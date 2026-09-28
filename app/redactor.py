"""Deterministic field redaction for parsed binary messages.

Selected ``uint``/``int`` field occurrences are replaced in place by an
HMAC-SHA256 bit stream keyed by ``REDACTION_KEY``: the same key, field name,
repeat index and original raw value always yield the same replacement bits.
The message length and the active branch never change; the checksum is
recomputed over the redacted bytes afterwards.
"""

import hmac
from hashlib import sha256
from typing import Any, Dict, List, Optional

from .checksum import compute
from .parser import parse_message

# HMAC-SHA256 emits 256 bits per block; fields may be up to 4096 bits wide at
# registration time, so the keyed stream is extended in counter blocks.
_BLOCK_BITS = 256
_MAX_BLOCKS = 16  # 16 * 256 = 4096 bits, the largest supported field width


class RedactionError(Exception):
    """Raised when a redaction request is invalid for the definition/message."""


def _put_bits(buf: bytearray, bit_offset: int, nbits: int, value: int) -> None:
    """Overwrite ``nbits`` at ``bit_offset`` MSB-first without touching the
    surrounding bits (supports non-byte-aligned fields)."""
    for i in range(nbits):
        pos = bit_offset + i
        bit = (value >> (nbits - 1 - i)) & 1
        byte_index = pos // 8
        mask = 1 << (7 - (pos % 8))
        if bit:
            buf[byte_index] |= mask
        else:
            buf[byte_index] &= ~mask


def _put_numeric(
    buf: bytearray, bit_offset: int, field: Dict[str, Any], raw: int
) -> None:
    """Write a numeric raw value in the field's wire layout (two's complement
    for negative ints; little-endian fields are stored least-significant byte
    first, exactly as the parser/encoder lay them out)."""
    n = field["bit_length"]
    bits = raw & ((1 << n) - 1)
    if field.get("endianness", "big") == "little":
        # validated at registration: n % 8 == 0
        for i in range(n // 8):
            _put_bits(buf, bit_offset + 8 * i, 8, (bits >> (8 * i)) & 0xFF)
    else:
        _put_bits(buf, bit_offset, n, bits)


def _occurrence_message(name: str, index: Optional[int], raw: int) -> bytes:
    """Canonical bytes hashed for one field occurrence.

    NUL-separated ``name`` / optional repeat index / original raw value
    (decimal, including the sign for signed ints) so two different
    occurrences can never share a message.
    """
    parts = [name.encode("utf-8")]
    if index is not None:
        parts.append(str(index).encode("ascii"))
    parts.append(str(raw).encode("ascii"))
    return b"\x00".join(parts)


def _keyed_bits(key: bytes, message: bytes, nbits: int) -> int:
    """Return the leading ``nbits`` bits of the HMAC-SHA256 keyed stream.

    The first block is ``HMAC(key, message)``; should the field be wider than
    one digest, extra blocks are ``HMAC(key, message || 0x00 || counter_be32)``
    (counter starting at 1), appended until enough bits are available.
    """
    needed_blocks = (nbits + _BLOCK_BITS - 1) // _BLOCK_BITS
    if needed_blocks > _MAX_BLOCKS:
        raise RedactionError(
            f"field bit length {nbits} exceeds the redaction stream limit of "
            f"{_MAX_BLOCKS * _BLOCK_BITS} bits"
        )
    stream = hmac.new(key, message, sha256).digest()
    for counter in range(1, needed_blocks):
        stream += hmac.new(
            key, message + b"\x00" + counter.to_bytes(4, "big"), sha256
        ).digest()
    value = int.from_bytes(stream, "big")
    return value >> (len(stream) * 8 - nbits)


def _occurrences(entry: Dict[str, Any]) -> List[tuple[Optional[int], int, int]]:
    """(repeat index, raw value, wire bit offset) for one parsed field entry."""
    n = entry["bit_length"]
    if "count" in entry:
        return [
            (i, raw, entry["bit_offset"] + i * n)
            for i, raw in enumerate(entry.get("raw_value", []))
        ]
    return [(None, entry["raw_value"], entry["bit_offset"])]


def redact_message(
    fields: List[Dict[str, Any]],
    checksum_cfg: Optional[Dict[str, Any]],
    data: bytes,
    selected: List[str],
    key: bytes,
) -> Dict[str, Any]:
    """Redact the selected scalar/repeated numeric fields of one message.

    The message must fully match its definition (complete consumption and,
    when configured, a valid checksum); callers normally gate with
    ``main._match_definition`` before invoking this.
    """
    result = parse_message(fields, checksum_cfg, data)
    if result["trailing_bits"]:
        raise RedactionError(
            f"definition does not fully consume the message: "
            f"{result['trailing_bits']} trailing bit(s)"
        )
    if checksum_cfg:
        checksum = result["checksum"]
        if checksum is None:
            raise RedactionError(
                f"checksum field '{checksum_cfg['field']}' was not parsed "
                f"(skipped by an earlier branch)"
            )
        if not checksum["valid"]:
            raise RedactionError(
                f"checksum mismatch: expected {checksum['expected_hex']}, "
                f"calculated {checksum['calculated_hex']}"
            )

    defs_by_name = {f["name"]: f for f in fields}
    condition_sources = {
        f["condition"]["field"] for f in fields if f.get("condition")
    }
    counter_fields = {f["repeat"]["field"] for f in fields if f.get("repeat")}
    checksum_name = checksum_cfg["field"] if checksum_cfg else None
    parsed_by_name = {f["name"]: f for f in result["fields"]}

    chosen: List[str] = []
    for name in selected:
        if name in chosen:
            raise RedactionError(
                f"field '{name}' appears more than once in the redaction list"
            )
        field = defs_by_name.get(name)
        if field is None:
            raise RedactionError(
                f"unknown field '{name}': not defined in the message definition"
            )
        ftype = field["type"]
        if ftype not in ("uint", "int"):
            raise RedactionError(
                f"field '{name}' is of type '{ftype}': only uint/int fields "
                f"can be redacted"
            )
        if name in condition_sources:
            raise RedactionError(
                f"field '{name}' is referenced by a condition and cannot be "
                f"redacted (changing it could switch the message branch)"
            )
        if name in counter_fields:
            raise RedactionError(
                f"field '{name}' drives a repeat count and cannot be redacted "
                f"(changing it would change the message length)"
            )
        if name == checksum_name:
            raise RedactionError(
                f"field '{name}' is the checksum field and cannot be redacted; "
                f"the checksum is recomputed automatically"
            )
        if name not in parsed_by_name:
            raise RedactionError(
                f"field '{name}' was not present in the message (skipped by "
                f"its branch condition)"
            )
        chosen.append(name)

    buf = bytearray(data)
    redactions: List[Dict[str, Any]] = []
    for name in chosen:
        field = defs_by_name[name]
        for index, original_raw, bit_offset in _occurrences(parsed_by_name[name]):
            message = _occurrence_message(name, index, original_raw)
            new_raw = _keyed_bits(key, message, field["bit_length"])
            # Keep the replacement inside the signed range semantics of the
            # wire: stored bits are identical either way, but the reported
            # redacted_raw mirrors what a re-parse yields for int fields.
            _put_numeric(buf, bit_offset, field, new_raw)
            if field["type"] == "int" and new_raw >= 1 << (field["bit_length"] - 1):
                reported_raw = new_raw - (1 << field["bit_length"])
            else:
                reported_raw = new_raw
            redactions.append(
                {
                    "name": name,
                    "index": index,
                    "bit_offset": bit_offset,
                    "bit_length": field["bit_length"],
                    "original_raw": original_raw,
                    "redacted_raw": reported_raw,
                }
            )

    checksum_info: Optional[Dict[str, Any]] = None
    if checksum_cfg:
        checksum_field = defs_by_name[checksum_name]
        checksum_entry = parsed_by_name[checksum_name]
        checksum_offset = checksum_entry["bit_offset"]
        if checksum_offset % 8 != 0:
            raise RedactionError(
                f"checksum field '{checksum_name}' does not start on a byte "
                f"boundary (bit offset {checksum_offset})"
            )
        calculated = compute(
            checksum_cfg["algorithm"], bytes(buf[: checksum_offset // 8])
        )
        _put_numeric(buf, checksum_offset, checksum_field, calculated)
        width = checksum_field["bit_length"]
        checksum_info = {
            "field": checksum_name,
            "algorithm": checksum_cfg["algorithm"],
            "range": f"bytes [0, {checksum_offset // 8})",
            "calculated": calculated,
            "calculated_hex": f"0x{calculated:0{width // 4}x}",
        }

    return {
        "hex": bytes(buf).hex(),
        "redactions": redactions,
        "checksum": checksum_info,
    }
