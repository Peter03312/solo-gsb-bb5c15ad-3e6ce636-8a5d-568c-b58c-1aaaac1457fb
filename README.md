# 工业二进制报文解析 API

基于 FastAPI 的工业二进制报文解析服务。用户注册**带版本的报文定义**（位宽、端序、条件分支、计数驱动的重复字段、校验字段），提交指定版本的十六进制报文后，服务按位解析、验证校验和，并返回字段值、位偏移及实际分支路径；反向地，也可以提交字段值**编码**出十六进制报文（校验和自动计算写入）。对于串口/TCP 式的**分块字节流**，还可以提交十六进制分块数组，由同步头与长度字段自动分帧、跨块重组、剔除噪声并逐帧解析。此外支持保存**解析回归样例**：把报文与基线字段快照存入 SQLite，版本升级后对新版本重跑并逐字段比对差异。需要快速掌握一批报文的字段画像时，可用**批量报文统计画像**接口一次提交 1～500 条报文，按版本给出接收/拒绝情况与各字段的出现次数、最小/最大/均值。服务还内置**请求耗时追踪**：除 `/health` 外的每个 API 请求都会把 request_id、方法、路径、状态码、起止时间与耗时写入 SQLite，可通过可观测性接口按时间范围和路径前缀查询总数、错误数、p50/p95 耗时及按状态码分组统计。SQLite 持久化，启动时自动建表。

## 快速启动

```bash
docker compose up --build
```

单条命令即可启动，服务监听 **8000** 端口：

- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 配置

通过环境变量配置（见 `docker-compose.yml`）：

| 变量      | 默认值             | 说明                 |
| --------- | ------------------ | -------------------- |
| `DB_PATH` | `/data/parser.db`  | SQLite 数据库文件路径（容器内），已挂载 named volume `parser-data` 持久化 |

端口映射在 `docker-compose.yml` 中为 `8000:8000`，如需修改主机端口，改左侧即可（如 `"9000:8000"`）。

## 报文定义格式

```json
{
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "description": "温湿度传感器报文",
  "fields": [
    {"name": "header",      "type": "uint", "bit_length": 8},
    {"name": "msg_type",    "type": "uint", "bit_length": 4},
    {"name": "flags",       "type": "uint", "bit_length": 4},
    {"name": "temperature", "type": "int",  "bit_length": 16, "endianness": "little",
     "scale": 0.1, "condition": {"field": "msg_type", "op": "eq", "value": 1}},
    {"name": "humidity",    "type": "uint", "bit_length": 8,
     "condition": {"field": "msg_type", "op": "eq", "value": 2}},
    {"name": "crc",         "type": "uint", "bit_length": 16}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt"}
}
```

**字段属性**

| 属性         | 说明                                                                 |
| ------------ | -------------------------------------------------------------------- |
| `name`       | 字段名，唯一定位                                                     |
| `type`       | `uint` / `int`（二进制补码）/ `bool`（1 位）/ `hex` / `ascii`        |
| `bit_length` | 位宽，支持非字节对齐（如 4 位）；`hex`/`ascii`/小端字段需为 8 的倍数 |
| `endianness` | `big`（默认）/ `little`，仅对多字节 `uint`/`int` 有效                |
| `scale`/`offset` | 可选，仅数值字段：`value = raw * scale + offset`                 |
| `condition`  | 可选，条件不满足时跳过该字段（条件比较的是**原始值 raw**，非缩放值） |
| `repeat`     | 可选，形如 `{"field": "count", "max": 8}`：字段按前置计数字段的原始值重复，详见下文 |

**条件操作符**：`eq` `ne` `gt` `ge` `lt` `le` `in`（`in` 的 `value` 为整数数组）。条件只能引用**之前定义**的标量数值/布尔字段。

**重复字段（repeat）**

- `repeat.field` 必须引用**之前定义的标量 `uint` 字段**（计数字段）；解析/编码时取该字段的**原始值 raw**作为重复次数。
- `repeat.max` 取值范围 **1～64**；运行时计数超过 `max` 返回 422。
- 重复字段可以是任意类型（含 `ascii`/`hex`/`bool`、支持小端与 `scale`/`offset`），可以同时带 `condition`：**条件未命中时不展开**任何重复项；命中后重复段长度（`count × bit_length`）正确推动后续字段与校验字段的偏移。
- 计数字段本身被更早的条件跳过时，重复无法确定，返回 422。
- 重复字段是数组，**不能**再被后续条件/计数引用；校验字段本身不能声明为重复字段。
- 校验和算法的计算范围天然覆盖展开后的全部字节（校验字段位于重复段之后时偏移随之顺延）。

**校验和算法**：`sum8`、`xor8`、`crc16-ccitt`（8 位字段用前两者，16 位字段用后两者）。计算范围为校验字段之前的全部字节，校验字段需字节对齐。

## API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/v1/definitions` | 注册报文定义（重复 `device_type+version` 返回 409） |
| GET  | `/api/v1/definitions` | 列出定义，参数：`device_type`、`include_inactive` |
| GET  | `/api/v1/definitions/{device_type}/latest` | 查询该设备类型最新**启用**版本 |
| GET  | `/api/v1/definitions/{device_type}/{version}` | 查询指定版本 |
| POST | `/api/v1/definitions/{device_type}/{version}/deactivate` | 停用定义 |
| POST | `/api/v1/definitions/{device_type}/compatibility` | 两个已注册版本的报文布局兼容性分析（>256 个等价类组合或任一版本含 `repeat` 定义返回 422） |
| POST | `/api/v1/parse` | 解析报文（`version` 缺省时使用最新启用版本） |
| POST | `/api/v1/stream/parse` | 字节流分帧：按同步头 + 无符号长度字段从十六进制分块中重组并逐帧解析；支持 `version_mode=detect` 在同一批分块中自动识别混合固件版本 |
| POST | `/api/v1/detect` | 不传版本，用全部已注册版本（含停用）逐一解析以自动识别版本 |
| POST | `/api/v1/messages/profile` | 批量报文统计画像：一次提交 1～500 条报文（每条可带/不带 `version`），按版本给出 accepted/rejected 与各字段出现次数及数值 min/max/mean |
| POST | `/api/v1/encode` | 反向编码：由字段值生成报文（版本选择规则同 parse） |
| POST | `/api/v1/fixtures` | 保存带名称的解析回归样例（名称在**设备类型内**唯一，重复返回 409） |
| GET  | `/api/v1/fixtures` | 列出回归样例，参数：`device_type` |
| GET  | `/api/v1/fixtures/{name}` | 按名称查询样例（跨设备类型重名时用 `?device_type=` 消歧，否则返回 409） |
| DELETE | `/api/v1/fixtures/{name}` | 删除样例（消歧规则同上） |
| POST | `/api/v1/fixtures/{name}/run` | 运行回归样例：用现有解析器重新解析并与基线快照逐字段比较 |
| GET  | `/api/v1/observability/latency` | 请求耗时统计：按半开时间范围 `[start, end)` 与路径前缀查询总数、错误数、p50/p95 及状态码分组 |

> 停用后的定义不参与列表（默认）和 latest 查询，但仍可按显式版本解析/编码历史报文。

## curl 示例

### 1. 注册定义

```bash
curl -X POST http://localhost:8000/api/v1/definitions \
  -H 'Content-Type: application/json' \
  -d '{
    "device_type": "temp_sensor",
    "version": "1.0.0",
    "description": "温湿度传感器报文",
    "fields": [
      {"name": "header",      "type": "uint", "bit_length": 8},
      {"name": "msg_type",    "type": "uint", "bit_length": 4},
      {"name": "flags",       "type": "uint", "bit_length": 4},
      {"name": "temperature", "type": "int",  "bit_length": 16, "endianness": "little",
       "scale": 0.1, "condition": {"field": "msg_type", "op": "eq", "value": 1}},
      {"name": "humidity",    "type": "uint", "bit_length": 8,
       "condition": {"field": "msg_type", "op": "eq", "value": 2}},
      {"name": "crc",         "type": "uint", "bit_length": 16}
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"}
  }'
```

### 2. 解析报文（温度分支）

报文 `aa10eb008936`：`header=0xAA`，`msg_type=1`，温度原始值 `0x00EB=235`（小端）× 0.1 = 23.5 ℃，CRC-16/CCITT = `0x8936`。

```bash
curl -X POST http://localhost:8000/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "temp_sensor", "version": "1.0.0", "hex": "aa10eb008936"}'
```

响应（节选）：

```json
{
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "fields": [
    {"name": "header",      "bit_offset": 0,  "bit_length": 8,  "value": 170},
    {"name": "msg_type",    "bit_offset": 8,  "bit_length": 4,  "value": 1},
    {"name": "flags",       "bit_offset": 12, "bit_length": 4,  "value": 0},
    {"name": "temperature", "bit_offset": 16, "bit_length": 16, "value": 23.5, "raw_value": 235},
    {"name": "crc",         "bit_offset": 32, "bit_length": 16, "value": 35126}
  ],
  "branch_path": [
    {"field": "temperature", "condition": "msg_type == 1", "actual_value": 1, "matched": true,  "action": "parsed"},
    {"field": "humidity",    "condition": "msg_type == 2", "actual_value": 1, "matched": false, "action": "skipped"}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt", "valid": true, "expected_hex": "0x8936", "calculated_hex": "0x8936"},
  "message_bits": 48, "consumed_bits": 48, "trailing_bits": 0, "warnings": []
}
```

湿度分支报文（`msg_type=2`，湿度 0x55=85%）：`aa2055ba77`。校验和错误时仍返回 200，但 `checksum.valid` 为 `false`；报文过短或 hex 非法返回 422。

### 3. 编码报文（解析的逆操作）

提交字段值，服务按位宽连续写入（支持非字节对齐、大小端、`int` 补码），自动计算并写入校验字段，返回 `hex`、实际编码字段和分支路径：

```bash
curl -X POST http://localhost:8000/api/v1/encode \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "temp_sensor", "version": "1.0.0",
       "values": {"header": 170, "msg_type": 1, "flags": 0, "temperature": 23.5}}'
```

响应（节选）：

```json
{
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "hex": "aa10eb008936",
  "fields": [
    {"name": "header",      "bit_offset": 0,  "bit_length": 8,  "value": 170,  "raw_value": 170},
    {"name": "msg_type",    "bit_offset": 8,  "bit_length": 4,  "value": 1,    "raw_value": 1},
    {"name": "flags",       "bit_offset": 12, "bit_length": 4,  "value": 0,    "raw_value": 0},
    {"name": "temperature", "bit_offset": 16, "bit_length": 16, "value": 23.5, "raw_value": 235},
    {"name": "crc",         "bit_offset": 32, "bit_length": 16, "value": 35126, "raw_value": 35126}
  ],
  "branch_path": [
    {"field": "temperature", "condition": "msg_type == 1", "actual_value": 1, "matched": true,  "action": "encoded"},
    {"field": "humidity",    "condition": "msg_type == 2", "actual_value": 1, "matched": false, "action": "skipped"}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt", "range": "bytes [0, 4)", "calculated": 35126, "calculated_hex": "0x8936"},
  "message_bits": 48, "padding_bits": 0
}
```

**编码规则**

- `values` 中各值采用**解析响应的类型**：`uint`/`int` 为整数（带 `scale`/`offset` 时可为浮点数），`bool` 为布尔值，`hex` 为 `"0x..."` 字符串，`ascii` 为字符串（长度须等于 `bit_length/8`）。
- 重复字段的值为**数组**，长度必须与计数字段的 raw 值**完全一致**（计数为 0 时传 `[]`，传标量或长度不符均返回 422）；逐元素套用类型/范围/scale 反算规则，错误信息会标出下标（如 `samples[1]`）。
- 带 `scale`/`offset` 的数值按 `raw = (value - offset) / scale` 反算；无法精确还原或 raw 超出位宽范围时返回 422。
- 条件判断统一使用 **raw**（与 parse 一致）：条件未命中的字段不要求输入（提供了也返回 422）；命中字段缺失、出现未知字段均返回 422。
- 校验字段**不接受调用方赋值**，服务在其位置自动计算写入；校验位置未字节对齐时返回 422。

### 4. 重复字段（repeat）：变长数组

定义中 `count` 是普通 uint 字段，`samples` 通过 `repeat` 声明由它的原始值决定重复次数（上限 8）：

```json
{
  "device_type": "array_dev",
  "version": "1.0.0",
  "fields": [
    {"name": "header",  "type": "uint", "bit_length": 8},
    {"name": "count",   "type": "uint", "bit_length": 8},
    {"name": "samples", "type": "uint", "bit_length": 16, "endianness": "little",
     "scale": 0.1, "repeat": {"field": "count", "max": 8}},
    {"name": "crc",     "type": "uint", "bit_length": 16}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt"}
}
```

报文 `aa03e800d00710270236`：`header=0xAA`，`count=3`，后跟 3 个小端 int16 采样值 raw `232 / 2000 / 10000`（即 23.2、200.0、1000.0），最后 2 字节 CRC。

```bash
# 注册
curl -X POST http://localhost:8000/api/v1/definitions \
  -H 'Content-Type: application/json' \
  -d '{ "device_type": "array_dev", "version": "1.0.0",
        "fields": [
          {"name": "header",  "type": "uint", "bit_length": 8},
          {"name": "count",   "type": "uint", "bit_length": 8},
          {"name": "samples", "type": "uint", "bit_length": 16, "endianness": "little",
           "scale": 0.1, "repeat": {"field": "count", "max": 8}},
          {"name": "crc",     "type": "uint", "bit_length": 16}
        ],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"} }'

# 解析（value / raw_value 为数组，count 为实际重复次数）
curl -X POST http://localhost:8000/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "array_dev", "version": "1.0.0", "hex": "aa03e800d00710270236"}'
```

响应（节选）：

```json
{
  "fields": [
    {"name": "header",  "bit_offset": 0,  "bit_length": 8,  "value": 170},
    {"name": "count",   "bit_offset": 8,  "bit_length": 8,  "value": 3},
    {"name": "samples", "bit_offset": 16, "bit_length": 16, "count": 3,
     "value": [23.2, 200.0, 1000.0], "raw_value": [232, 2000, 10000]},
    {"name": "crc",     "bit_offset": 64, "bit_length": 16, "value": 566}
  ],
  "consumed_bits": 80, "trailing_bits": 0
}
```

- `bit_length` 仍是**单个元素**的位宽；重复段总位数为 `count × bit_length`，后续字段（含校验字段）的偏移随之顺延。
- 计数为 0 时该字段返回 `"value": []`（数值字段同时有 `"raw_value": []`），不占用报文位。
- 编码时数组长度必须与 `count` 完全一致：

```bash
curl -X POST http://localhost:8000/api/v1/encode \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "array_dev", "version": "1.0.0",
       "values": {"header": 170, "count": 3, "samples": [23.2, 200.0, 1000.0]}}'
# -> {"hex": "aa03e800d00710270236", ...}

# count=0 的报文：aa00 + crc16-ccitt(aa00) = aa00efba
curl -X POST http://localhost:8000/api/v1/encode \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "array_dev", "version": "1.0.0",
       "values": {"header": 170, "count": 0, "samples": []}}'
```

返回 422 的情形：运行时计数超过声明的 `max`、编码数组长度与计数不一致（或不是数组）、重复段超出报文长度、计数字段被更早的条件分支跳过。注册时若计数引用不是前置标量 `uint`、`max` 不在 1～64、重复字段被后续条件引用或校验字段声明为重复，同样返回 422。

### 5. 列表 / 最新版本 / 停用

```bash
# 列出全部启用中的定义（可加 ?device_type=temp_sensor 或 ?include_inactive=true）
curl http://localhost:8000/api/v1/definitions

# 查询设备类型的最新启用版本
curl http://localhost:8000/api/v1/definitions/temp_sensor/latest

# 停用指定版本
curl -X POST http://localhost:8000/api/v1/definitions/temp_sensor/1.0.0/deactivate
```

### 6. 版本兼容性分析

比较同一 `device_type` 的两个**已注册**版本（无需提供报文）。分析器：

1. 收集所有被条件引用的字段（控制字段），按条件中出现的整数常量把每个控制字段的原始值域划分为有限个**等价类**——同一等价类内的取值使该字段上的全部条件真假恒定（如 `eq 1` 分出 `[0] / [1] / [2..2ⁿ-1]`，`gt 5` 在 `6` 处切分）；
2. 枚举各控制字段等价类代表值的笛卡尔积，对每个可达组合**模拟布局**：按条件保留/跳过字段，计算每个字段的位偏移与布局总长度（条件引用的字段被更早分支跳过时该组合不可达，自动跳过）；
3. 对旧版每个可达布局，在新版的**相同条件代表值**下寻找对应布局：字段名称、类型、位宽、位偏移、总长度及校验算法必须全部一致。任一旧布局找不到对应布局即判为 `breaking`，逐分支返回差异；否则判为 `compatible`。

等价类组合总数超过 **256** 时返回 `422`（边界值 256 正常分析）。**任一版本含 `repeat` 定义时也直接返回 422**：重复长度由运行时计数值决定，无法在不提供报文的情况下给出静态布局结论，因此拒绝分析而不是给出可能误导的判定。

```bash
curl -X POST http://localhost:8000/api/v1/definitions/temp_sensor/compatibility \
  -H 'Content-Type: application/json' \
  -d '{"old_version": "1.0.0", "new_version": "2.0.0"}'
```

兼容时响应（节选）：

```json
{
  "device_type": "temp_sensor",
  "old_version": "1.0.0",
  "new_version": "2.0.0",
  "compatible": true,
  "status": "compatible",
  "equivalence_classes": [
    {"field": "msg_type", "classes": 4, "representatives": [0, 1, 2, 3]}
  ],
  "combination_count": 4,
  "reachable_branches": 4,
  "unreachable_combinations": 0,
  "breaking_branches": 0,
  "branches": [
    {
      "representative": {"msg_type": {"op": "==", "value": 1}},
      "compatible": true,
      "old_layout": {
        "fields": [
          {"name": "header",      "type": "uint", "bit_length": 8,  "bit_offset": 0},
          {"name": "msg_type",    "type": "uint", "bit_length": 4,  "bit_offset": 8},
          {"name": "flags",       "type": "uint", "bit_length": 4,  "bit_offset": 12},
          {"name": "temperature", "type": "int",  "bit_length": 16, "bit_offset": 16},
          {"name": "crc",         "type": "uint", "bit_length": 16, "bit_offset": 32}
        ],
        "total_bits": 48,
        "checksum_algorithm": "crc16-ccitt"
      },
      "new_layout": { "...": "结构相同..." }
    }
  ]
}
```

发生破坏时，顶层 `"status"` 为 `"breaking"`、`"compatible"` 为 `false`，不兼容分支的 `"differences"` 逐项列出差异，`kind` 取值：

| `kind` | 含义 |
| ------ | ---- |
| `field_added` / `field_removed` | 新版多出 / 删除了字段（含因新分支引入的字段） |
| `field_changed` | 同名字段的 `type` / `bit_length` / `bit_offset` 变化 |
| `total_length_changed` | 布局总位数变化 |
| `checksum_algorithm_changed` | 校验算法变化（如 `crc16-ccitt` → `crc16-modbus`） |
| `branch_unreachable` | 旧版可达分支在新版下条件无法满足（控制字段被跳过） |

两版本对同一控制字段的位宽不同时，按两者原始值域的**交集**划分（无交集返回 422）；新版新增控制字段或删除旧控制字段时，仅在受影响的等价类上产生差异，未受影响分支仍可判为兼容。

### 7. 自动识别版本（/detect）

收到报文但不知道版本时，调用 `/api/v1/detect`。请求体**只含** `device_type` 与 `hex`（不接受 `version`，多余字段返回 422）；服务会取出该设备类型下**全部已注册版本（含停用版本）**，按版本顺序逐一解析：

- **候选条件**：定义**完整消费**报文（无剩余尾部位）；若该版本配置了校验和，还要求校验通过。未配置校验和的版本只按完整消费判断。
- **唯一候选**：返回识别出的版本及与 `/parse` 完全一致的解析结果。
- **无候选**：返回 `422`，并在 `detail.failures` 中按版本给出失败原因（报文过短、存在尾部位、校验和不符、校验字段被条件分支跳过等）。
- **多个候选**：返回 `409` 并在 `detail.candidates` 中给出候选版本列表，**不会**静默选择最新版本——调用方需自行消歧（如改用带 `version` 的 `/parse`）。

```bash
# 示例设备注册了 1.0.0（2 字节，sum8）与 2.0.0（4 字节，crc16-ccitt）
curl -X POST http://localhost:8000/api/v1/detect \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "detect_sensor", "hex": "aa2a6a92"}'
```

唯一候选时响应（与 `/parse` 相同）：

```json
{
  "device_type": "detect_sensor",
  "version": "2.0.0",
  "definition_active": true,
  "fields": [ "...同 /parse..." ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt", "valid": true, "...": "..."},
  "message_bits": 32, "consumed_bits": 32, "trailing_bits": 0, "warnings": []
}
```

无候选时 `422` 响应示例（4 字节报文对 1.0.0 多出尾部位，对 2.0.0 则校验和不符）：

```json
{
  "detail": {
    "message": "no registered version matches the message",
    "device_type": "detect_sensor",
    "failures": [
      {"version": "1.0.0", "active": false, "reason": "definition does not fully consume the message: 16 trailing bit(s)"},
      {"version": "2.0.0", "active": true,  "reason": "checksum mismatch: expected 0x0000, calculated 0x6a92"}
    ]
  }
}
```

多候选时 `409` 响应示例（按版本顺序列出，含停用状态）：

```json
{
  "detail": {
    "message": "message matches multiple registered versions; an explicit version is required",
    "device_type": "detect_dup",
    "candidates": [
      {"version": "1.0.0", "active": true},
      {"version": "2.0.0", "active": false}
    ]
  }
}
```

> 设备类型下没有任何注册版本（含停用）时返回 `404`；hex 非法/为空返回 `422`，清洗规则与 `/parse` 一致（忽略空白、`_`、`-` 与 `0x` 前缀）。

### 8. 字节流分帧解析（/stream/parse）

串口 / TCP 收到的原始字节通常是**任意分块**的，还可能夹杂线路噪声、损坏帧和被分块边界切断的半帧。`/api/v1/stream/parse` 在按位解析器之上叠加一层**字节流分帧器**：按 `chunks` 顺序维护跨块缓冲，用同步字节定位帧头，按帧内无符号长度字段切出整帧后再交给现有解析器。

**请求参数**

| 参数 | 说明 |
| ---- | ---- |
| `device_type` / `version` | 固定模式（`version_mode="fixed"`，默认）下的版本选择规则与 `/parse` 一致（`version` 缺省取最新启用版本） |
| `version_mode` | `"fixed"`（默认）：全部帧按固定版本解析；`"detect"`：每帧用该设备类型的**全部已注册版本（含停用）**自动识别，同一批 `chunks` 允许混合固件版本。detect 模式下不允许传 `version`（返回 422） |
| `sync_hex` | 帧头同步字节（一或多字节），如 `"aa55"`；支持跨块匹配（`…aa` 与 `55…` 分两块到达也能识别） |
| `length_field_offset` | 长度字段相对同步头的字节偏移（≥0） |
| `length_field_bytes` | 无符号长度字段宽度（1～8 字节） |
| `length_endianness` | 长度字段端序：`big`（默认）/ `little` |
| `length_adjustment` | 有符号修正值（默认 0），用于长度值不直接等于帧长的协议 |
| `chunks` | 十六进制字符串数组，**按到达顺序**给出；一帧可以横跨多个分块 |

从同步头起算的**总帧长**（字节）为：

```
total = length_field_offset + length_field_bytes + length_value + length_adjustment
```

即长度值默认表示「长度字段之后的字节数」；若协议长度字段从其自身末尾起算（Netty 风格），传负的 `length_adjustment`（如长度字段在偏移 1、宽 2 且长度值含整个帧头时，`length_adjustment = -3`）。

**分帧与重同步行为**

- **跨块缓冲**：同步头、长度字段或帧体被分块切断时自动拼接，帧完整前不解析、不报错。
- **噪声**：同步头之前的字节作为 `noise` 丢弃（包括尾部恰好无法构成同步头的同步起始字节）。
- **非法长度**：长度值加修正后短于帧头本身（如为负）时丢弃该候选同步字节并继续寻找下一个同步头。
- **解析 / 校验失败**：整帧到达后交给现有解析器；报文过短、解析失败、校验字段被分支跳过、校验和不符或定义未完整消费帧（存在尾部位）时，**逐字节重同步**——只丢弃该候选同步字节，从下一个同步头重试，避免一个坏帧导致整流失联。
- **detect 模式的识别与拒绝规则**：每个完整候选帧都按**版本顺序**尝试全部已注册版本（含停用），判定标准与 `/detect` 完全一致（完整消费帧 + 配置了校验和时校验通过）。**恰好一个版本命中**才接收该帧，帧结果中携带该帧的 `version` 与 `definition_active`，因此同一批 `chunks` 中的帧可以来自不同固件版本（例如升级交替期的设备总线）。**零命中**时同样逐字节重同步，丢弃记录的 `reason` 为 `parse_error`（全部版本均仅因校验和不符时为 `checksum`），并在 `failures` 中逐版本列出失败原因；**多版本命中**时也丢弃候选帧，但 `reason` 为独立的 **`ambiguous_version`** 且在 `candidates` 中列出候选版本——**不会**静默选择最新版本。
- **末尾未完成帧不是错误**：最后一个分块处理完后仍滞留的字节（半帧或半个同步头）通过 `incomplete_hex` 返回，调用方可在后续请求中续传（本接口为无状态单次重组，续传需调用方自行拼接后重发）。
- **硬性上限**：滞留的未分帧缓冲超过 **1 MiB**，或长度字段声明的单帧超过 **64 KiB**，返回 `422`（这类情况无法安全缓冲，不做静默丢弃）。

> 分帧以**字节**为粒度：被选定义必须完整消费整帧（`trailing_bits = 0`），否则该帧会被判为坏帧触发重同步；非字节对齐（位级）的报文定义不适用于字节长度分帧。

**响应**

- 顶层 `version_mode` 回显识别模式；`fixed` 模式下顶层 `version` / `definition_active` 即固定使用的定义，`detect` 模式下两者为 `null`（版本是逐帧属性）。
- `frames[]`：每帧含相对拼接字节流的原始半开区间偏移 `start` / `end`、`length`、原始 `hex`，以及与 `/parse` 完全一致的 `result`（字段、分支、校验结果等）；detect 模式下每帧 `result` 还含 `version` / `definition_active`。
- `discarded_ranges[]`：每个被丢弃区间含 `start` / `end` / `length` / `hex`、`reason`（`noise` / `invalid_length` / `parse_error` / `checksum` / `ambiguous_version`）及 `detail`。detect 模式下零命中记录额外含 `failures`（逐版本的 `version` / `active` / `reason`），多版本命中记录额外含 `candidates`（`version` / `active` 列表，按版本顺序）；固定模式不包含这两个字段。
- `incomplete_hex` / `incomplete_bytes`：末尾滞留的未完成字节（为空串表示流恰好在帧边界结束）。
- 另有 `received_bytes`、`frame_count`、`discarded_count`、`discarded_bytes` 与 `limits` 汇总。

示例协议：`AA | len(1B) | seq(1B) | temperature(2B) | xor8(1B)`，长度字段计长度字段之后的字节（恒为 4），总帧长 6：

```bash
curl -X POST http://localhost:8000/api/v1/stream/parse \
  -H 'Content-Type: application/json' \
  -d '{
    "device_type": "stream_dev",
    "version": "1.0.0",
    "sync_hex": "aa",
    "length_field_offset": 1,
    "length_field_bytes": 1,
    "length_endianness": "big",
    "length_adjustment": 0,
    "chunks": ["1234aa040100", "19b6aa0402ffff"]
  }'
```

上例中一帧被分块切断（`aa040100 | 19b6`），前导 `1234` 是噪声；响应（节选）：

```json
{
  "device_type": "stream_dev",
  "version": "1.0.0",
  "received_bytes": 13,
  "frame_count": 1,
  "frames": [
    {"start": 2, "end": 8, "length": 6, "hex": "aa04010019b6",
     "result": {"fields": [{"name": "seq", "value": 1}, {"name": "temperature", "value": 25}],
                "checksum": {"valid": true}}}
  ],
  "discarded_count": 1,
  "discarded_bytes": 2,
  "discarded_ranges": [
    {"start": 0, "end": 2, "length": 2, "hex": "1234", "reason": "noise", "detail": null}
  ],
  "incomplete_bytes": 5,
  "incomplete_hex": "aa0402ffff",
  "limits": {"max_buffer_bytes": 1048576, "max_frame_bytes": 65536}
}
```

> 第二个分块尾部 `aa0402ffff` 是尚未收完（缺 1 字节校验位）的帧，计入 `incomplete_hex` 而**不**返回错误。`chunks` 为空列表是合法的空流（200，无帧无丢弃）；某个分块 hex 非法时返回 `422` 并在 `detail` 中标注下标（如 `chunks[1]: …`），整个请求不会产生部分分帧结果。

#### detect 模式：同一批分块解析混合固件版本

设备类型 `stream_mix` 注册了两个字节长度不同的版本（均为 `AA | len(1B) | seq(1B) | … | xor8(1B)`，长度字段在偏移 1、占 1 字节）：

- `1.0.0`：`seq + xor8`，长度值 2，整帧 4 字节；
- `2.0.0`：`seq + temperature(2B) + xor8`，长度值 4，整帧 6 字节。

一条总线在固件升级交替期可能混有两种帧。请求中传 `"version_mode": "detect"`（**不传** `version`），服务对每个完整候选帧尝试全部已注册版本（含停用），恰好一个版本命中才接收：

```bash
curl -X POST http://localhost:8000/api/v1/stream/parse \
  -H 'Content-Type: application/json' \
  -d '{
    "device_type": "stream_mix",
    "version_mode": "detect",
    "sync_hex": "aa",
    "length_field_offset": 1,
    "length_field_bytes": 1,
    "length_endianness": "big",
    "length_adjustment": 0,
    "chunks": ["aa040b01", "2c22aa020f"]
  }'
```

上例第一块含一个被切断的 2.0.0 帧（`aa040b01 2c22`，`seq=11`、温度小整数，合计 6 字节），第二块尾部紧跟一个 1.0.0 帧（`aa020f` + xor8，4 字节）。响应（节选）：

```json
{
  "device_type": "stream_mix",
  "version": null,
  "definition_active": null,
  "version_mode": "detect",
  "frame_count": 2,
  "frames": [
    {"start": 0, "end": 6, "length": 6, "hex": "aa040b012c22",
     "result": {"version": "2.0.0", "definition_active": true,
                "fields": [{"name": "seq", "value": 11}, {"name": "temperature", "value": 300}],
                "checksum": {"valid": true}}},
    {"start": 6, "end": 10, "length": 4, "hex": "aa020f...",
     "result": {"version": "1.0.0", "definition_active": true,
                "fields": [{"name": "seq", "value": 15}],
                "checksum": {"valid": true}}}
  ],
  "discarded_ranges": []
}
```

- **命中已停用版本**也照常接收，该帧 `result.definition_active` 为 `false`。
- **零命中**：候选同步字节被逐字节重同步丢弃，`reason` 为 `parse_error`（或全部版本仅校验和不符时的 `checksum`），`failures` 逐版本给出原因：

```json
{"start": 0, "end": 1, "hex": "aa", "reason": "parse_error",
 "detail": "no registered version matches the frame (2 version(s) tried): 1.0.0: message too short: ...; 2.0.0: message too short: ...",
 "failures": [
   {"version": "1.0.0", "active": false, "reason": "message too short: need 8 bits at offset 24, only 0 bits left"},
   {"version": "2.0.0", "active": true,  "reason": "message too short: ..."}
 ]}
```

- **多版本命中**：候选帧同样被丢弃，但 `reason` 是 `ambiguous_version`，并在 `candidates` 中按版本顺序列出命中的版本（含停用状态），服务**不会**静默选择最新版：

```json
{"start": 0, "end": 1, "hex": "aa", "reason": "ambiguous_version",
 "detail": "frame matches 2 registered versions (1.0.0, 2.0.0); cannot pick a version without an explicit hint",
 "candidates": [
   {"version": "1.0.0", "active": true},
   {"version": "2.0.0", "active": false}
 ]}
```

> detect 模式沿用相同的 **64 KiB 单帧**与 **1 MiB 缓冲**硬性上限；设备类型下没有任何注册版本（含停用）时返回 `404`；同时传 `version` 与 `"version_mode": "detect"` 返回 `422`。不传 `version_mode`（或传 `"fixed"`）时行为与旧版完全一致，顶层仍返回固定的 `version` / `definition_active`。

### 9. 解析回归样例（/fixtures）

报文定义升级后，需要确认同一批历史报文在新版本上的解析表现。回归样例把一条报文与它在某个版本上的**字段快照**绑定存储（SQLite），随后可对任意目标版本（或默认的基线版本）重跑，逐字段比较：

1. **字段名 + `raw_value`**：数值字段比较原始整数（`uint`/`int`；重复字段为整数数组；`bool` 归一化为 0/1；`hex`/`ascii` 无 raw 值，只按字段名比较存在性）。
2. **分支路径 `branch_path`**：逐条件字段比较 `matched` 与 `action`（`parsed`/`skipped`）。
3. **校验结果**：比较 `checksum.valid`（目标定义没有校验字段时为 `null`）。

**保存样例**

```bash
curl -X POST http://localhost:8000/api/v1/fixtures \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "temp-branch",
    "device_type": "temp_sensor",
    "version": "1.0.0",
    "hex": "aa10eb008936",
    "expect": {
      "fields": [
        {"name": "header", "raw_value": 170},
        {"name": "temperature", "raw_value": 235}
      ],
      "branch_path": [{"field": "temperature", "matched": true, "action": "parsed"}],
      "checksum_valid": true
    }
  }'
```

- 创建时服务先按 `version` 解析一次 `hex`，把完整快照（全部字段 / 全部分支 / 校验结果）作为**基线**持久化；基线解析失败返回 422，引用的定义不存在返回 404。
- `expect` 各部分均可选，只做**子集断言**：`fields[].raw_value` 省略时仅要求该字段在基线中出现；与实际解析不符返回 422 并在 `detail.problems` 中逐项列出原因。无论是否提供 `expect`，持久化的始终是完整快照。
- `name` 在**同一 `device_type` 内唯一**（允许字母数字及 `_.:-`）；重复保存返回 409。不同设备类型可以重名，查询/删除/运行时用 `device_type` 消歧。

返回示例（节选）：

```json
{
  "name": "temp-branch",
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "hex": "aa10eb008936",
  "snapshot": {
    "fields": [
      {"name": "header", "raw_value": 170},
      {"name": "msg_type", "raw_value": 1},
      {"name": "flags", "raw_value": 0},
      {"name": "temperature", "raw_value": 235},
      {"name": "crc", "raw_value": 35126}
    ],
    "branch_path": [
      {"field": "temperature", "matched": true, "action": "parsed"},
      {"field": "humidity", "matched": false, "action": "skipped"}
    ],
    "checksum_valid": true
  },
  "created_at": "2026-09-21T..."
}
```

**运行样例**

```bash
# 对基线版本自身回归（body 也可省略 device_type 以外的字段）
curl -X POST http://localhost:8000/api/v1/fixtures/temp-branch/run \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "temp_sensor"}'

# 对新版本回归
curl -X POST http://localhost:8000/api/v1/fixtures/temp-branch/run \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "temp_sensor", "target_version": "2.0.0"}'
```

- 请求体必须包含 `device_type`（名称只在设备类型内唯一）；`target_version` 缺省时使用样例的基线版本。目标版本不存在返回 404。
- 目标版本解析**失败**（报文过短等）不返回 5xx，而是 HTTP 200 + `parse_error` 字段，并把每个基线字段/分支/校验项都保留为失败项，便于一次看到全部问题。

响应示例（版本变化，字段差异）：

```json
{
  "name": "temp-branch",
  "device_type": "temp_sensor",
  "baseline_version": "1.0.0",
  "target_version": "2.0.0",
  "version_changed": true,
  "hex": "aa10eb008936",
  "baseline": { "...": "保存时的快照..." },
  "actual": { "...": "目标版本的实际快照..." },
  "parse_error": null,
  "passed": false,
  "comparison": {
    "fields": {
      "added":    [{"name": "status", "raw_value": 0}],
      "removed":  [{"name": "flags", "raw_value": 0}],
      "changed":  [{"name": "temperature", "baseline_raw_value": 235, "actual_raw_value": 2350}],
      "unchanged": [{"name": "header", "raw_value": 170}],
      "passed": false
    },
    "branch_path": {
      "added": [],
      "removed": [],
      "changed": [
        {"field": "humidity",
         "baseline": {"field": "humidity", "matched": false, "action": "skipped"},
         "actual":   {"field": "humidity", "matched": true,  "action": "parsed"}}
      ],
      "unchanged": [],
      "passed": false
    },
    "checksum": {"baseline_valid": true, "actual_valid": true, "passed": true},
    "total_checks": 9,
    "passed_checks": 6,
    "pass_rate": 0.6667,
    "errors": [
      "field 'status' newly appears in target parse (raw_value 0)",
      "field 'flags' missing in target parse (baseline raw_value 0)",
      "field 'temperature' raw_value changed: baseline 235 -> actual 2350",
      "branch on 'humidity' changed: baseline matched false (skipped) -> actual matched true (parsed)"
    ]
  }
}
```

- `comparison.fields` 与 `comparison.branch_path` 各自给出 `added` / `removed` / `changed` / `unchanged` 明细；`errors` 是所有未通过项的逐条说明（含目标解析失败时的 `target parse failed: ...`）。
- `total_checks = 字段项数 + 分支项数 + 1（校验）`，`pass_rate = passed_checks / total_checks`；全部一致且无解析错误时顶层 `passed` 为 `true`、`pass_rate` 为 `1.0`。
- 列表 `GET /api/v1/fixtures?device_type=temp_sensor`；按名查询/删除支持 `?device_type=` 消歧，名称跨设备类型重复且未消歧时返回 409（`detail.candidates` 列出候选）。

### 10. 批量报文统计画像（/messages/profile）

一次提交 **1～500 条**报文，逐条解析后按**版本分组**给出字段统计画像，适合对总线上抓下的一批混合报文快速摸底。请求体包含 `device_type` 与 `samples[]`；每条 sample 为 `{"hex": "...", "version": "可选"}`：

- 每条 sample **带 `version`** 时按该版本解析（停用版本也可显式指定）；**省略 `version`** 时复用 `/detect` 的自动识别规则——按版本顺序尝试全部已注册版本（含停用），**恰好一个版本**完整消费且校验通过才归属该版本。
- **逐条独立处理，单条失败不中止整批**。接收门槛与 `/detect` 一致：定义必须完整消费报文（无尾部位），配置了校验和时还必须校验通过；校验和不符、校验字段被分支跳过、报文过短、尾部位等均判为该条 rejected。
- **拒绝条目不进入统计**。能归属到版本的拒绝（显式版本解析/校验失败）列入该版本组的 `rejected`；无法归属版本的拒绝（hex 非法、显式版本不存在、省略版本时零候选/多候选）列入**顶层** `rejected`。每条拒绝只含 `index`（请求数组下标，从 0 起）与 `reason`。

**统计口径**

- 每个版本组给出 `accepted_count` 与 `rejected`，并按该版本的**字段定义顺序**逐字段输出 `present_count`（该字段在多少条被接收报文中实际出现）。
- 条件分支**未命中而跳过**的字段既不计入 `present_count`，也不计入任何 min/max/mean 的分母——即分母只包含字段实际出现的样本。
- 数值标量（`uint`/`int`，含 scale/offset 换算后的显示值）汇总 `min`/`max`/`mean`；`bool`、`hex`、`ascii` 只有 `present_count`。
- 数值 **repeat 数组按元素**汇总：每一个展开元素都是一个观测值，`element_count` 给出元素总数（即均值分母），`present_count` 仍是包含该字段的**报文条数**（计数为 0 的空数组报文计为出现，但不贡献元素）；元素为空时 `min/max/mean` 为 `null`。
- **不同版本的同名字段严格隔离**，各自在自己的版本组内统计（响应中同时回显 `type`/`bit_length` 便于区分）。版本组按版本号数值顺序（`1.9.0` 在 `1.10.0` 之前）稳定输出，组内拒绝按下标升序。

以下示例使用第 1、2 节注册的 `temp_sensor` 1.0.0（温度/湿度条件分支 + crc16-ccitt）：

```bash
curl -X POST http://localhost:8000/api/v1/messages/profile \
  -H 'Content-Type: application/json' \
  -d '{
    "device_type": "temp_sensor",
    "samples": [
      {"hex": "aa10eb008936"},
      {"hex": "aa10d200363b"},
      {"hex": "aa2055ba77"},
      {"version": "1.0.0", "hex": "aa10eb00deadbeef"},
      {"hex": "zz"}
    ]
  }'
```

前三条分别是温度 23.5 ℃、温度 21.0 ℃ 与湿度 85%（版本省略，自动识别为 1.0.0，hex 均为含正确 CRC 的完整报文）；第 4 条带显式版本但多出尾部位 → 归入 1.0.0 组的 `rejected`；第 5 条 hex 非法 → 顶层 `rejected`。

响应（示例）：

```json
{
  "device_type": "temp_sensor",
  "sample_count": 5,
  "accepted_count": 3,
  "rejected_count": 2,
  "rejected": [
    {"index": 4, "reason": "invalid hex payload: non-hex characters or odd number of digits"}
  ],
  "versions": [
    {
      "version": "1.0.0",
      "definition_active": true,
      "accepted_count": 3,
      "rejected": [
        {"index": 3, "reason": "definition does not fully consume the message: 16 trailing bit(s)"}
      ],
      "fields": [
        {"name": "header", "type": "uint", "bit_length": 8,
         "present_count": 3, "min": 170, "max": 170, "mean": 170.0},
        {"name": "msg_type", "type": "uint", "bit_length": 4,
         "present_count": 3, "min": 1, "max": 2, "mean": 1.333333},
        {"name": "flags", "type": "uint", "bit_length": 4,
         "present_count": 3, "min": 0, "max": 0, "mean": 0.0},
        {"name": "temperature", "type": "int", "bit_length": 16,
         "present_count": 2, "min": 21.0, "max": 23.5, "mean": 22.25},
        {"name": "humidity", "type": "uint", "bit_length": 8,
         "present_count": 1, "min": 85, "max": 85, "mean": 85.0},
        {"name": "crc", "type": "uint", "bit_length": 16,
         "present_count": 3, "min": 13883, "max": 47735, "mean": 32248.0}
      ]
    }
  ]
}
```

注意 `temperature` 的 `present_count=2`（湿度分支那条未出现，分母不含它），`humidity` 为 1；若同一设备还注册了字段同名但位宽/语义不同的 2.0.0，两个版本会分别出现在 `versions[]` 中互不影响。省略版本的报文若零候选或多候选（歧义），按 `/detect` 同样的措辞进入顶层 `rejected`，服务不会静默挑选最新版本。

> 设备类型下没有任何注册版本（含停用）时返回 `404`；`samples` 为空、超过 500 条或含未知字段返回 `422`。hex 清洗规则与其他接口一致（忽略空白、`_`、`-` 与 `0x` 前缀）。

**启动后可访问性**

该路由与其他路由同在 `app/main.py` 中以 `@app.post` 注册，并由 `app/profiler.py` 的批量画像处理器提供实现，随 `uvicorn app.main:app`（即 `docker compose up --build`）的应用模块导入一并加载，**无需额外开关或步骤**。服务启动时（lifespan）会断言该路由已挂到应用上——若处理器未随应用加载，容器会在启动阶段直接报错退出，而不是在启动后对该路径返回静默 `404`。

启动后可用以下方式确认路由在线（在容器/服务所在主机执行）：

```bash
# 1) 健康检查，确认应用已完成启动
curl http://localhost:8000/health
# -> {"status":"ok"}

# 2) 路由已随应用加载（出现在 OpenAPI 路径列表中）
curl http://localhost:8000/openapi.json | python3 -c \
  "import json,sys; print('/api/v1/messages/profile' in json.load(sys.stdin)['paths'])"
# -> True

# 3) 对一个尚未注册任何版本的设备类型 POST，路由命中处理器并返回业务 404
#    （"no definitions found ..."），而非路由层面的 {"detail":"Not Found"}，
#    以此区分“路由未注册”与“该设备暂无定义”
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  http://localhost:8000/api/v1/messages/profile \
  -H 'Content-Type: application/json' \
  -d '{"device_type":"nonexistent","samples":[{"hex":"aa"}]}'
# -> 404（业务响应，detail 提示该设备无定义；先注册定义再提交样本即返回 200 画像）
```

> 注意区分两种 `404`：路径完全未注册时 FastAPI 返回 `{"detail":"Not Found"}`；本路由在线但 `device_type` 下没有任何已注册版本（含停用）时返回 `{"detail":"no definitions found for device type '...'"}`。正常使用流程是先 `POST /api/v1/definitions` 注册至少一个版本，再批量提交样本，此时返回 `200`。

### 11. 请求耗时追踪（/observability/latency）

服务内置请求耗时追踪中间件：**除 `/health` 外的每个 HTTP 请求**（含 4xx/5xx、路由未命中的 404）都会在响应完成后写入一条记录到 SQLite 表 `request_latency`，字段为：

| 字段 | 说明 |
| ---- | ---- |
| `request_id` | 每个请求生成的 UUID（同时通过响应头 `X-Request-ID` 返回，便于关联日志） |
| `method` / `path` | HTTP 方法与**实际请求路径**（如 `/api/v1/definitions/temp_sensor/1.0.0`，非路由模板） |
| `status_code` | 响应状态码 |
| `started_at` / `ended_at` | 请求开始 / 结束时间，UTC ISO-8601（微秒精度） |
| `duration_ms` | 耗时，毫秒（单调时钟 `perf_counter` 计时，保留 3 位小数） |

> 追踪写入是**尽力而为**的：持久化失败只记录日志，绝不改变原接口的状态码与响应体；即使 SQLite 不可用，业务接口仍正常工作。`/health` 不追踪、也不返回 `X-Request-ID`，保证存活探针始终轻量可用。

**查询接口**

```
GET /api/v1/observability/latency?start=...&end=...&path_prefix=...
```

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `start` | 否 | 时间范围起点（**包含**），ISO-8601，如 `2026-09-22T00:00:00Z`；不带时区的朴素时间按 UTC 处理 |
| `end` | 否 | 时间范围终点（**不包含**），格式同 `start` |
| `path_prefix` | 否 | 仅统计路径以该前缀开头的请求（纯字符串前缀匹配，如 `/api/v1/parse`） |

- 时间范围是**半开区间 `[start, end)`**：以**请求开始时间** `started_at` 过滤，恰好在 `start` 时刻的请求计入，恰好在 `end` 时刻的请求不计入。两个参数都可省略（省略端表示不限制该侧）。
- 时间戳不是合法 ISO-8601（如 `not-a-time`、Unix 秒 `1700000000`、`2026-13-99`）返回 **422**；`start >= end`（含两者相等，半开区间为空无意义）同样返回 **422**。
- 错误数口径为 `status_code >= 400`；p50/p95 采用**最近秩（nearest-rank）**算法，结果为某个实际样本值，不做插值；窗口内无请求时 p50/p95 为 `null`。

```bash
# 查询某一天 /api/v1 下全部请求的耗时统计
curl -sG 'http://localhost:8000/api/v1/observability/latency' \
  --data-urlencode 'start=2026-09-22T00:00:00Z' \
  --data-urlencode 'end=2026-09-23T00:00:00Z' \
  --data-urlencode 'path_prefix=/api/v1'
```

响应示例：

```json
{
  "filters": {
    "start": "2026-09-22T00:00:00.000000+00:00",
    "end": "2026-09-23T00:00:00.000000+00:00",
    "path_prefix": "/api/v1"
  },
  "total_requests": 4,
  "error_requests": 2,
  "latency_ms": {"p50": 2.15, "p95": 8.42},
  "status_codes": [
    {"status_code": 200, "count": 2},
    {"status_code": 404, "count": 1},
    {"status_code": 422, "count": 1}
  ]
}
```

非法时间戳的 422 响应示例：

```json
{"detail": "start must be an ISO-8601 timestamp (e.g. 2026-09-22T00:00:00Z), got 'not-a-time'"}
```

```json
{"detail": "start must be earlier than end (time range is half-open [start, end))"}
```

耗时记录与业务数据存放在同一个 SQLite 数据库（`request_latency` 表，`started_at` 上有索引），随 named volume `parser-data` 一起持久化；首次启动自动建表，无需手动迁移。

## 本地开发（不用 Docker）

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # http://localhost:8000
pytest                               # 运行测试
```

## 项目结构

```
app/
  main.py       # FastAPI 路由与生命周期
  schemas.py    # 请求模型与定义语义校验
  parser.py     # 按位解析器（位读取、条件分支、repeat 数组展开、校验和验证）
  encoder.py    # 按位编码器（位写入、scale/offset 反算、repeat 数组、校验和自动写入）
  framing.py    # 字节流分帧（同步头 + 长度字段、跨块缓冲、逐字节重同步、1 MiB/64 KiB 上限）
  analyzer.py   # 版本兼容性分析（条件等价类划分、可达分支枚举、布局比对；遇 repeat 拒绝分析）
  fixtures.py   # 回归样例：基线快照捕获、expect 断言、逐字段 added/removed/changed 比较
  profiler.py   # 批量报文统计画像（按版本隔离、字段出现次数、标量/重复元素 min/max/mean）
  observability.py # 请求耗时追踪中间件 + GET /api/v1/observability/latency 聚合统计
  checksum.py   # sum8 / xor8 / crc16-ccitt / crc16-modbus
  database.py   # SQLite 持久化（definitions / fixtures / request_latency 三表，启动自动建表）
tests/test_api.py
Dockerfile
docker-compose.yml
```

## Docker 镜像与持久化

`Dockerfile` 基于 `python:3.12-slim`，仅安装运行依赖（`requirements.txt`），启动命令为
`uvicorn app.main:app --host 0.0.0.0 --port 8000`。`docker-compose.yml` 将容器内数据库
`/data/parser.db` 挂载到 named volume `parser-data`，因此注册的报文定义、回归样例与**请求耗时记录**在容器
重建后仍然保留；首次启动时三张表（`definitions`、`fixtures`、`request_latency`）自动创建，无需手动迁移。

```bash
# 启动（首次自动构建镜像）
docker compose up --build

# 后台运行 / 查看日志 / 停止
docker compose up -d --build
docker compose logs -f
docker compose down            # 不删除数据卷；加 -v 才会清空 parser-data

# 不用 compose 直接运行
docker build -t msg-parser .
docker run -p 8000:8000 -e DB_PATH=/data/parser.db \
  -v parser-data:/data msg-parser
```

启动后可产生并查询请求耗时记录（`/health` 不会被记录）：

```bash
# 触发若干业务请求（响应头 X-Request-ID 即该请求的 request_id）
curl -s http://localhost:8000/api/v1/definitions -D - -o /dev/null | grep -i x-request-id

# 查询最近一天 /api/v1 前缀请求的总数、错误数、p50/p95 与状态码分组
curl -sG 'http://localhost:8000/api/v1/observability/latency' \
  --data-urlencode 'start=2026-09-22T00:00:00Z' \
  --data-urlencode 'end=2026-09-23T00:00:00Z' \
  --data-urlencode 'path_prefix=/api/v1'
```
