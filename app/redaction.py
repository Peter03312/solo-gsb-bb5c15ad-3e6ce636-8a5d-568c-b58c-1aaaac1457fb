"""Deterministic HMAC-SHA256 field redaction.

Selected ``uint``/``int`` occurrences are replaced in place: every occurrence
gets a fresh bit string derived from an HMAC-SHA256 bit stream keyed by
``REDACTION_KEY``. The authenticated message for every occurrence uniquely
identifies *which* occurrence it is -- field name, repeat index and the
original raw value -- so the same key and the same input always produce the
same redacted bits, and two occurrences of one repeated field never collide
unless their original raw values happen to be equal.

Occurrence message (NUL-separated, unambiguous)::

    <field-name UTF-8> \\x00 <repeat-index or empty> \\x00 <signed decimal raw>

The bit stream starts with the plain ``HMAC-SHA256(key, message)`` digest and,
when a field is wider than one digest (256 bits), extends with a feedback
chain ``HMAC(key, previous_block)``; the leading ``bit_length`` bits (MSB
first) become the replacement raw value.

Protected fields are never redacted: fields referenced by conditions, repeat
counter fields and checksum fields. Branches and repeat counts therefore stay
valid, the message length is unchanged, and the checksum is recomputed over
the patched bytes so the returned hex parses again cleanly.
"""

import hashlib
import hmac
from typing import Any, Dict, List, Optional

from .checksum import compute
from .parser import ParseError, parse_message

_DIGEST_BITS = 256


class RedactionError(Exception):
    """The redaction request cannot be satisfied (maps to HTTP 422).

    ``problems`` optionally carries several field-level reasons at once so a
    caller learns every rejected selection in one round trip.
    """

    def __init__(self, message: str, problems: Optional[List[str]] = None):
        super().__init__(message)
        self.problems = problems


def _write_bits_msb(buf: bytearray, bit_offset: int, value: int, nbits: int) -> None:
    """Overwrite ``nbits`` at ``bit_offset`` with ``value`` (MSB first)."""
    for shift in range(nbits - 1, -1, -1):
        pos = bit_offset + (nbits - 1 - shift)
        byte = buf[pos // 8]
        if (value >> shift) & 1:
            byte |= 1 << (7 - (pos % 8))
        else:
            byte &= ~(1 << (7 - (pos % 8)))
        buf[pos // 8] = byte


def _patch_numeric(buf: bytearray, bit_offset: int, field: Dict[str, Any], raw_bits: int) -> None:
    """Write an unsigned numeric bit pattern the same way the parser read it."""
    n = field["bit_length"]
    bits = raw_bits & ((1 << n) - 1)  # two's complement mask for int fields
    if field.get("endianness", "big") == "little":
        # Registration guarantees little-endian numeric fields are byte aligned.
        for i in range(n // 8):
            _write_bits_msb(buf, bit_offset + 8 * i, (bits >> (8 * i)) & 0xFF, 8)
    else:
        _write_bits_msb(buf, bit_offset, bits, n)


def _hmac_stream_value(key: bytes, message: bytes, nbits: int) -> int:
    """Return the leading ``nbits`` bits of the HMAC-SHA256 feedback stream.

    Block 0 is the plain HMAC over ``message`` (so widths up to 256 bits are
    exactly HMAC-SHA256); wider fields extend with HMAC(key, previous block),
    chaining as many 256-bit blocks as needed.
    """
    blocks_needed = (nbits + _DIGEST_BITS - 1) // _DIGEST_BITS
    chunks: List[bytes] = []
    block = hmac.new(key, message, hashlib.sha256).digest()
    while len(chunks) < blocks_needed:
        chunks.append(block)
        block = hmac.new(key, block, hashlib.sha256).digest()
    stream = b"".join(chunks)
    return int.from_bytes(stream, "big") >> (len(stream) * 8 - nbits)


def _occurrence_message(name: str, index: Optional[int], raw: int) -> bytes:
    index_part = b"" if index is None else str(index).encode("ascii")
    return (
        name.encode("utf-8")
        + b"\x00"
        + index_part
        + b"\x00"
        + str(raw).encode("ascii")
    )


def _as_signed(raw_bits: int, nbits: int, ftype: str) -> int:
    if ftype == "int" and raw_bits >= 1 << (nbits - 1):
        return raw_bits - (1 << nbits)
    return raw_bits


def redact_message(
    record: Dict[str, Any],
    data: bytes,
    field_names: List[str],
    key: bytes,
) -> Dict[str, Any]:
    """Redact the selected numeric occurrences of one parsed message.

    The input must fully match the definition (complete consumption, original
    checksum valid when configured). Eligibility of every requested name is
    validated before any bit is touched; all replacement bits derive from the
    *original* parse result, so the outcome depends only on key and input.
    """
    fields_def: List[Dict[str, Any]] = record["fields"]
    checksum_cfg = record.get("checksum")

    result = parse_message(fields_def, checksum_cfg, data)

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

    by_name = {f["name"]: f for f in fields_def}
    parsed_by_name = {f["name"]: f for f in result["fields"]}
    condition_refs = {
        f["condition"]["field"] for f in fields_def if f.get("condition")
    }
    counter_refs = {f["repeat"]["field"] for f in fields_def if f.get("repeat")}
    checksum_name = checksum_cfg["field"] if checksum_cfg else None

    # Duplicate selections add no work and must not change the result.
    selected = list(dict.fromkeys(field_names))

    problems: List[str] = []
    for name in selected:
        field = by_name.get(name)
        if field is None:
            problems.append(f"unknown field '{name}': not defined in the message definition")
            continue
        if field["type"] not in ("uint", "int"):
            problems.append(
                f"field '{name}': only uint/int fields can be redacted "
                f"(got {field['type']})"
            )
        if name in condition_refs:
            problems.append(
                f"field '{name}': referenced by a condition and cannot be redacted"
            )
        if name in counter_refs:
            problems.append(
                f"field '{name}': drives a repeat count and cannot be redacted"
            )
        if name == checksum_name:
            problems.append(f"field '{name}': checksum field cannot be redacted")
        parsed = parsed_by_name.get(name)
        if parsed is None:
            problems.append(
                f"field '{name}': not present in this message (skipped by its branch condition)"
            )
        elif field.get("repeat") is not None and parsed.get("count", 0) == 0:
            problems.append(
                f"field '{name}': not present in this message (repeat count is 0)"
            )
    if problems:
        raise RedactionError("redaction request contains invalid field selections", problems)

    buf = bytearray(data)
    redacted_fields: List[Dict[str, Any]] = []

    for name in selected:
        field = by_name[name]
        parsed = parsed_by_name[name]
        n = field["bit_length"]
        if field.get("repeat") is not None:
            base = parsed["bit_offset"]
            for index, raw in enumerate(parsed["raw_value"]):
                new_bits = _hmac_stream_value(key, _occurrence_message(name, index, raw), n)
                _patch_numeric(buf, base + index * n, field, new_bits)
                redacted_fields.append(
                    {
                        "name": name,
                        "index": index,
                        "bit_offset": base + index * n,
                        "bit_length": n,
                        "original_raw_value": raw,
                        "raw_value": _as_signed(new_bits, n, field["type"]),
                    }
                )
        else:
            raw = parsed["raw_value"]
            offset = parsed["bit_offset"]
            new_bits = _hmac_stream_value(key, _occurrence_message(name, None, raw), n)
            _patch_numeric(buf, offset, field, new_bits)
            redacted_fields.append(
                {
                    "name": name,
                    "index": None,
                    "bit_offset": offset,
                    "bit_length": n,
                    "original_raw_value": raw,
                    "raw_value": _as_signed(new_bits, n, field["type"]),
                }
            )

    checksum_info: Optional[Dict[str, Any]] = None
    if checksum_cfg:
        cs_field = by_name[checksum_name]
        cs_bit_offset = parsed_by_name[checksum_name]["bit_offset"]
        cs_byte_offset = cs_bit_offset // 8
        calculated = compute(checksum_cfg["algorithm"], bytes(buf[:cs_byte_offset]))
        _patch_numeric(buf, cs_bit_offset, cs_field, calculated)
        checksum_info = {
            "field": checksum_name,
            "algorithm": checksum_cfg["algorithm"],
            "range": f"bytes [0, {cs_byte_offset})",
            "calculated": calculated,
            "calculated_hex": f"0x{calculated:0{cs_field['bit_length'] // 4}x}",
        }

    patched = bytes(buf)

    # The output must be a valid message under the same definition: it parses
    # fully, follows the same branch path and carries a valid checksum.
    verified = parse_message(fields_def, checksum_cfg, patched)
    if verified["trailing_bits"]:
        raise RedactionError("internal error: redacted message leaves trailing bits")
    if checksum_cfg and (verified["checksum"] is None or not verified["checksum"]["valid"]):
        raise RedactionError("internal error: redacted message failed checksum verification")

    if checksum_info is not None:
        checksum_info["valid"] = True

    return {
        "hex": patched.hex(),
        "fields": verified["fields"],
        "branch_path": verified["branch_path"],
        "checksum": (
            {**verified["checksum"], "range": checksum_info["range"]}
            if checksum_cfg
            else None
        ),
        "message_bits": verified["message_bits"],
        "consumed_bits": verified["consumed_bits"],
        "trailing_bits": 0,
        "warnings": [],
        "redacted_fields": redacted_fields,
    }
