"""Bit-level message encoder: the reverse of parser.parse_message."""

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .checksum import compute
from .parser import _OP_SYMBOLS, resolve_repeat_count


class EncodeError(Exception):
    """Raised when caller-provided values cannot be encoded against a definition."""


class BitWriter:
    """Writes bits MSB-first into a growing byte buffer."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.pos = 0  # current bit position

    def write(self, value: int, nbits: int) -> None:
        if nbits <= 0:
            raise EncodeError(f"bit_length must be positive, got {nbits}")
        if value < 0 or value >= (1 << nbits):
            raise EncodeError(f"value {value} does not fit in {nbits} bits")
        for shift in range(nbits - 1, -1, -1):
            if self.pos % 8 == 0:
                self._buf.append(0)
            if (value >> shift) & 1:
                self._buf[self.pos // 8] |= 1 << (7 - (self.pos % 8))
            self.pos += 1

    def to_bytes(self) -> bytes:
        return bytes(self._buf)


def _eval_condition(condition: Dict[str, Any], raw_values: Dict[str, Any]) -> Tuple[bool, Any]:
    """Same semantics as the parser: conditions always compare raw values."""
    field = condition["field"]
    if field not in raw_values:
        raise EncodeError(
            f"cannot evaluate condition: referenced field '{field}' was not encoded "
            f"(skipped by an earlier branch)"
        )
    actual = raw_values[field]
    op = condition["op"]
    expected = condition["value"]
    if op == "eq":
        return actual == expected, actual
    if op == "ne":
        return actual != expected, actual
    if op == "gt":
        return actual > expected, actual
    if op == "ge":
        return actual >= expected, actual
    if op == "lt":
        return actual < expected, actual
    if op == "le":
        return actual <= expected, actual
    if op == "in":
        return actual in expected, actual
    raise EncodeError(f"unsupported condition operator: {op}")


def _numeric_to_raw(field: Dict[str, Any], value: Any) -> Tuple[Any, int]:
    """Validates a numeric value and returns (display_value, raw_int)."""
    name = field["name"]
    ftype = field["type"]
    n = field["bit_length"]
    scale = field.get("scale")
    offset = field.get("offset")
    if ftype == "uint":
        lo, hi = 0, (1 << n) - 1
    else:  # int, two's complement
        lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1

    if scale is None and offset is None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise EncodeError(f"field '{name}': expected an integer value")
        raw = value
        display: Any = raw
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EncodeError(f"field '{name}': expected a numeric value")
        if isinstance(value, float) and not math.isfinite(value):
            raise EncodeError(f"field '{name}': value must be finite")
        sc = scale if scale is not None else 1.0
        off = offset if offset is not None else 0.0
        if sc == 0:
            raise EncodeError(f"field '{name}': scale is zero, cannot reverse-compute raw")
        try:
            raw = int(round((value - off) / sc))
        except OverflowError:
            raise EncodeError(
                f"field '{name}': value {value} is out of range for {n}-bit {ftype}"
            )
        display = round(raw * sc + off, 6)
        if display != value:
            raise EncodeError(
                f"field '{name}': value {value} cannot be exactly represented with "
                f"scale={sc} offset={off} (nearest encodable value is {display})"
            )
    if not (lo <= raw <= hi):
        raise EncodeError(
            f"field '{name}': raw value {raw} out of range [{lo}, {hi}] for {n}-bit {ftype}"
        )
    return display, raw


_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _to_raw(field: Dict[str, Any], value: Any) -> Tuple[Any, int]:
    """Converts a caller-provided value (parse-response type) to (display, raw_int)."""
    ftype = field["type"]
    name = field["name"]
    n = field["bit_length"]
    if ftype == "bool":
        if not isinstance(value, bool):
            raise EncodeError(f"field '{name}': expected a boolean value")
        return value, int(value)
    if ftype in ("uint", "int"):
        return _numeric_to_raw(field, value)
    if ftype == "hex":
        if not isinstance(value, str):
            raise EncodeError(f"field '{name}': expected a hex string like '0x1af'")
        s = value.strip()
        if s.lower().startswith("0x"):
            s = s[2:]
        if not s or not _HEX_RE.match(s):
            raise EncodeError(f"field '{name}': invalid hex string: {value!r}")
        raw = int(s, 16)
        if raw >= (1 << n):
            raise EncodeError(f"field '{name}': value {value!r} does not fit in {n} bits")
        return f"0x{raw:0{n // 4}x}", raw
    if ftype == "ascii":
        if not isinstance(value, str):
            raise EncodeError(f"field '{name}': expected a string value")
        try:
            data = value.encode("ascii")
        except UnicodeEncodeError:
            raise EncodeError(f"field '{name}': value is not ASCII-encodable: {value!r}")
        if len(data) != n // 8:
            raise EncodeError(
                f"field '{name}': expected exactly {n // 8} ASCII character(s), got {len(data)}"
            )
        return value, int.from_bytes(data, "big")
    raise EncodeError(f"unsupported field type: {ftype}")


def _write_numeric(writer: BitWriter, field: Dict[str, Any], raw: int) -> None:
    n = field["bit_length"]
    bits = raw & ((1 << n) - 1)  # two's complement for negative int raws
    if field.get("endianness", "big") == "little":
        # validated at registration: n % 8 == 0; least significant byte first
        for i in range(n // 8):
            writer.write((bits >> (8 * i)) & 0xFF, 8)
    else:
        writer.write(bits, n)


def _write_field(writer: BitWriter, field: Dict[str, Any], raw: int) -> None:
    if field["type"] == "bool":
        writer.write(raw, 1)
    elif field["type"] in ("uint", "int"):
        _write_numeric(writer, field, raw)
    else:  # hex / ascii: byte-aligned big-endian by registration rules
        writer.write(raw, field["bit_length"])


def encode_message(
    fields: List[Dict[str, Any]],
    checksum_cfg: Optional[Dict[str, Any]],
    values: Dict[str, Any],
) -> Dict[str, Any]:
    writer = BitWriter()
    raw_values: Dict[str, Any] = {}  # raw values, used by conditions
    out_fields: List[Dict[str, Any]] = []
    branch_path: List[Dict[str, Any]] = []
    checksum_info: Optional[Dict[str, Any]] = None
    consumed: set[str] = set()

    for field in fields:
        name = field["name"]
        condition = field.get("condition")
        if condition:
            matched, actual = _eval_condition(condition, raw_values)
            symbol = _OP_SYMBOLS[condition["op"]]
            branch_path.append(
                {
                    "field": name,
                    "condition": f"{condition['field']} {symbol} {condition['value']}",
                    "actual_value": actual,
                    "matched": matched,
                    "action": "encoded" if matched else "skipped",
                }
            )
            if not matched:
                continue

        bit_offset = writer.pos
        repeat = field.get("repeat")

        if repeat is not None:
            if name not in values:
                raise EncodeError(f"missing value for field '{name}'")
            provided = values[name]
            if not isinstance(provided, list):
                raise EncodeError(
                    f"field '{name}': expected a list of {field['type']} values "
                    f"(repeat-driven by '{repeat['field']}'), got {type(provided).__name__}"
                )
            # resolve (and bound) the counter before comparing array lengths
            count = resolve_repeat_count(name, repeat, raw_values, error_cls=EncodeError)
            if len(provided) != count:
                raise EncodeError(
                    f"field '{name}': repeat counter '{repeat['field']}' requires exactly "
                    f"{count} value(s), got {len(provided)}"
                )
            consumed.add(name)
            displays: List[Any] = []
            raws: List[Any] = []
            for index, item in enumerate(provided):
                element_field = dict(field)
                element_field["name"] = f"{name}[{index}]"
                display, raw = _to_raw(element_field, item)
                _write_field(writer, field, raw)
                displays.append(display)
                if field["type"] in ("uint", "int"):
                    raws.append(raw)
            entry: Dict[str, Any] = {
                "name": name,
                "bit_offset": bit_offset,
                "bit_length": field["bit_length"],
                "count": count,
                "value": displays,
            }
            if field["type"] in ("uint", "int"):
                entry["raw_value"] = raws
            out_fields.append(entry)
            # repeated fields are arrays: never used as condition/counter sources
            continue

        if checksum_cfg and name == checksum_cfg["field"]:
            if name in values:
                raise EncodeError(
                    f"checksum field '{name}' is computed by the service; "
                    f"do not provide a value for it"
                )
            if bit_offset % 8 != 0:
                raise EncodeError(
                    f"checksum field '{name}' does not start on a byte boundary "
                    f"(bit offset {bit_offset})"
                )
            calculated = compute(checksum_cfg["algorithm"], writer.to_bytes())
            _write_numeric(writer, field, calculated)
            raw_values[name] = calculated
            out_fields.append(
                {
                    "name": name,
                    "bit_offset": bit_offset,
                    "bit_length": field["bit_length"],
                    "value": calculated,
                    "raw_value": calculated,
                }
            )
            checksum_info = {
                "field": name,
                "algorithm": checksum_cfg["algorithm"],
                "range": f"bytes [0, {bit_offset // 8})",
                "calculated": calculated,
                "calculated_hex": f"0x{calculated:0{field['bit_length'] // 4}x}",
            }
            continue

        if name not in values:
            raise EncodeError(f"missing value for field '{name}'")
        consumed.add(name)
        display, raw = _to_raw(field, values[name])
        _write_field(writer, field, raw)
        entry: Dict[str, Any] = {
            "name": name,
            "bit_offset": bit_offset,
            "bit_length": field["bit_length"],
            "value": display,
        }
        if field["type"] in ("uint", "int"):
            entry["raw_value"] = raw
        out_fields.append(entry)
        raw_values[name] = raw

    leftovers = [k for k in values if k not in consumed]
    if leftovers:
        known = {f["name"] for f in fields}
        checksum_name = checksum_cfg["field"] if checksum_cfg else None
        key = sorted(leftovers)[0]
        if key == checksum_name:
            raise EncodeError(
                f"checksum field '{key}' is computed by the service; "
                f"do not provide a value for it"
            )
        if key in known:
            raise EncodeError(
                f"field '{key}' was skipped by its branch condition; "
                f"do not provide a value for it"
            )
        raise EncodeError(f"unknown field '{key}': not defined in the message definition")

    data = writer.to_bytes()
    return {
        "hex": data.hex(),
        "fields": out_fields,
        "branch_path": branch_path,
        "checksum": checksum_info,
        "message_bits": writer.pos,
        "padding_bits": len(data) * 8 - writer.pos,
    }
