"""Bit-level message parser driven by a versioned message definition."""

from typing import Any, Dict, List, Optional

from .checksum import compute


class ParseError(Exception):
    """Raised when a message cannot be parsed against its definition."""


class BitReader:
    """Reads bits MSB-first from a byte string."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0  # current bit position

    @property
    def remaining(self) -> int:
        return len(self.data) * 8 - self.pos

    def read(self, nbits: int) -> int:
        if nbits <= 0:
            raise ParseError(f"bit_length must be positive, got {nbits}")
        if nbits > self.remaining:
            raise ParseError(
                f"message too short: need {nbits} bits at offset {self.pos}, "
                f"only {self.remaining} bits left"
            )
        value = 0
        for _ in range(nbits):
            byte = self.data[self.pos // 8]
            bit = (byte >> (7 - (self.pos % 8))) & 1
            value = (value << 1) | bit
            self.pos += 1
        return value


_OP_SYMBOLS = {
    "eq": "==",
    "ne": "!=",
    "gt": ">",
    "ge": ">=",
    "lt": "<",
    "le": "<=",
    "in": "in",
}


def _eval_condition(condition: Dict[str, Any], values: Dict[str, Any]) -> tuple[bool, Any]:
    field = condition["field"]
    if field not in values:
        raise ParseError(
            f"cannot evaluate condition: referenced field '{field}' was not parsed "
            f"(skipped by an earlier branch)"
        )
    actual = values[field]
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
    raise ParseError(f"unsupported condition operator: {op}")


def _read_numeric(reader: BitReader, field: Dict[str, Any]) -> int:
    n = field["bit_length"]
    if field.get("endianness", "big") == "little":
        # validated at registration: n % 8 == 0
        raw = 0
        for i in range(n // 8):
            raw |= reader.read(8) << (8 * i)
    else:
        raw = reader.read(n)
    if field["type"] == "int" and raw >= (1 << (n - 1)):
        raw -= 1 << n  # two's complement
    return raw


def _read_field(reader: BitReader, field: Dict[str, Any]) -> tuple[Any, Optional[int]]:
    """Returns (display_value, raw_int_for_conditions)."""
    ftype = field["type"]
    n = field["bit_length"]
    if ftype == "bool":
        raw = reader.read(1)
        return bool(raw), raw
    if ftype in ("uint", "int"):
        raw = _read_numeric(reader, field)
        value: Any = raw
        scale = field.get("scale")
        offset = field.get("offset")
        if scale is not None or offset is not None:
            value = round(raw * (scale if scale is not None else 1.0) + (offset or 0.0), 6)
        return value, raw
    if ftype == "hex":
        raw = reader.read(n)
        return f"0x{raw:0{n // 4}x}", None
    if ftype == "ascii":
        raw_bytes = bytes(reader.read(8) for _ in range(n // 8))
        return raw_bytes.decode("ascii", errors="replace"), None
    raise ParseError(f"unsupported field type: {ftype}")


def resolve_repeat_count(
    field_name: str,
    repeat: Dict[str, Any],
    raw_values: Dict[str, Any],
    error_cls: type = ParseError,
) -> int:
    """Resolve the runtime repetition count from a preceding scalar uint field.

    The referenced field must have been present on this branch and its raw
    value must not exceed the registration-time ``max``.
    """
    counter = repeat["field"]
    if counter not in raw_values:
        raise error_cls(
            f"field '{field_name}': repeat counter '{counter}' was not parsed "
            f"(skipped by an earlier branch)"
        )
    count = raw_values[counter]
    maximum = repeat["max"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise error_cls(
            f"field '{field_name}': repeat counter '{counter}' has an invalid raw value {count!r}"
        )
    if count > maximum:
        raise error_cls(
            f"field '{field_name}': repeat count {count} from '{counter}' exceeds the "
            f"declared maximum of {maximum}"
        )
    return count


def parse_message(
    fields: List[Dict[str, Any]],
    checksum_cfg: Optional[Dict[str, Any]],
    data: bytes,
) -> Dict[str, Any]:
    reader = BitReader(data)
    values: Dict[str, Any] = {}  # raw values, used by conditions
    out_fields: List[Dict[str, Any]] = []
    branch_path: List[Dict[str, Any]] = []
    checksum_info: Optional[Dict[str, Any]] = None

    for field in fields:
        name = field["name"]
        condition = field.get("condition")
        if condition:
            matched, actual = _eval_condition(condition, values)
            symbol = _OP_SYMBOLS[condition["op"]]
            branch_path.append(
                {
                    "field": name,
                    "condition": f"{condition['field']} {symbol} {condition['value']}",
                    "actual_value": actual,
                    "matched": matched,
                    "action": "parsed" if matched else "skipped",
                }
            )
            if not matched:
                continue

        bit_offset = reader.pos
        repeat = field.get("repeat")

        if repeat is not None:
            count = resolve_repeat_count(name, repeat, values)
            values_list: List[Any] = []
            raws_list: List[Any] = []
            for _ in range(count):
                value, raw = _read_field(reader, field)
                values_list.append(value)
                if raw is not None:
                    raws_list.append(raw)
            entry: Dict[str, Any] = {
                "name": name,
                "bit_offset": bit_offset,
                "bit_length": field["bit_length"],
                "count": count,
                "value": values_list,
            }
            if field["type"] in ("uint", "int"):
                entry["raw_value"] = raws_list
            out_fields.append(entry)
            # repeated fields are arrays: never used as condition/counter sources
            continue

        value, raw = _read_field(reader, field)
        entry = {
            "name": name,
            "bit_offset": bit_offset,
            "bit_length": field["bit_length"],
            "value": value,
        }
        if raw is not None and field["type"] in ("uint", "int"):
            entry["raw_value"] = raw
        out_fields.append(entry)
        if raw is not None:
            values[name] = raw

        if checksum_cfg and name == checksum_cfg["field"]:
            if bit_offset % 8 != 0:
                raise ParseError(
                    f"checksum field '{name}' does not start on a byte boundary "
                    f"(bit offset {bit_offset})"
                )
            calculated = compute(checksum_cfg["algorithm"], data[: bit_offset // 8])
            checksum_info = {
                "field": name,
                "algorithm": checksum_cfg["algorithm"],
                "range": f"bytes [0, {bit_offset // 8})",
                "expected": raw,
                "calculated": calculated,
                "expected_hex": f"0x{raw:0{field['bit_length'] // 4}x}",
                "calculated_hex": f"0x{calculated:0{field['bit_length'] // 4}x}",
                "valid": raw == calculated,
            }

    trailing = reader.remaining
    warnings: List[str] = []
    if trailing:
        warnings.append(f"{trailing} trailing bit(s) not covered by the definition")

    return {
        "fields": out_fields,
        "branch_path": branch_path,
        "checksum": checksum_info,
        "message_bits": len(data) * 8,
        "consumed_bits": reader.pos,
        "trailing_bits": trailing,
        "warnings": warnings,
    }
