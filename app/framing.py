"""Byte-stream framing on top of the bit-level message parser.

Raw serial/TCP input arrives in arbitrary chunks and may contain leading
noise, corrupted frames and partial frames split across chunk boundaries.
This module reassembles the stream and extracts self-delimited frames using
a Netty-style length field:

* the frame header is located by a configurable **sync byte sequence**;
* an **unsigned length field** sits at a fixed absolute offset from the
  sync bytes (``length_field_offset``) and is ``length_field_bytes`` wide;
* the total frame length, measured from the sync bytes, is::

      length_field_offset + length_field_bytes + length_value
      + length_adjustment

  (``length_adjustment`` handles trailing payloads not counted in the
  length value and lengths counted relative to the end of the length field).

Whenever the bytes at a sync position do not form a valid frame -- the
length field yields an illegal total length, the resulting frame fails to
parse, its checksum does not verify, or the parser leaves trailing bits --
the framer discards the sync byte *one byte at a time* and retries at the
next sync occurrence. A frame that is simply not fully received yet is
kept in a cross-chunk buffer and is never reported as an error.
"""

from dataclasses import dataclass
from typing import Callable, List, Literal, Optional

# Hard safety limits shared with the API layer.
MAX_BUFFER_SIZE = 1024 * 1024  # 1 MiB of retained (unframed) bytes
MAX_FRAME_SIZE = 64 * 1024  # 64 KiB per single frame

# Why a byte range was discarded.
REASON_NOISE = "noise"  # bytes outside any frame (before a sync)
REASON_INVALID_LENGTH = "invalid_length"  # sync found, length value unusable
REASON_PARSE_ERROR = "parse_error"  # frame rejected by the parse/verify callback
REASON_CHECKSUM = "checksum"  # kept distinct for callers that want it
# detect mode: a complete frame matched more than one registered version
REASON_AMBIGUOUS_VERSION = "ambiguous_version"


class FramingError(Exception):
    """Hard framing failure (buffer/frame size limit exceeded).

    Such failures are reported to the client (HTTP 422) instead of being
    recovered by resynchronisation, because the stream can no longer be
    held in memory safely.
    """


class RejectFrame(Exception):
    """Raised by a frame-validation callback to force one-byte resync.

    ``reason`` should be one of the ``REASON_*`` constants (defaults to a
    generic parse failure); ``detail`` is a human-readable explanation.
    ``failures`` carries structured per-version failure records and
    ``candidates`` the versions that matched, used by detect mode so the
    rejection stays machine-readable instead of a single free-form string.
    """

    def __init__(
        self,
        detail: str,
        reason: str = REASON_PARSE_ERROR,
        failures: Optional[list] = None,
        candidates: Optional[list] = None,
    ):
        super().__init__(detail)
        self.detail = detail
        self.reason = reason
        self.failures = failures
        self.candidates = candidates


Endianness = Literal["big", "little"]
ValidateFrame = Callable[[bytes], object]


@dataclass(frozen=True)
class FrameSpec:
    """Static description of the wire framing."""

    sync: bytes
    length_field_offset: int
    length_field_bytes: int
    length_endianness: Endianness
    length_adjustment: int
    max_frame_size: int = MAX_FRAME_SIZE


@dataclass
class DiscardedRange:
    """A half-open byte interval ``[start, end)`` thrown away during resync."""

    start: int  # absolute offset in the concatenated stream
    end: int
    reason: str
    detail: Optional[str] = None
    hex: str = ""
    # detect mode only: per-version failure records (zero candidates) or the
    # matching version list (ambiguous_version); None in fixed mode.
    failures: Optional[list] = None
    candidates: Optional[list] = None


@dataclass
class RawFrame:
    """One extracted frame: where it came from and what the callback returned."""

    start: int
    end: int
    hex: str
    result: object


def _trailing_sync_prefix_len(data: bytes, sync: bytes) -> int:
    """Length of the longest prefix of ``sync`` matching the tail of ``data``.

    A sync marker may straddle two chunks (e.g. ``...AB`` then ``CD...`` for
    marker ``ABCD``); such a trailing partial match must stay buffered.
    """
    upper = min(len(sync) - 1, len(data))
    for take in range(upper, 0, -1):
        if data[-take:] == sync[:take]:
            return take
    return 0


class StreamFramer:
    """Incrementally frame a byte stream fed in arbitrary chunks."""

    def __init__(self, spec: FrameSpec, validate: ValidateFrame):
        self.spec = spec
        self._validate = validate
        self._buffer = bytearray()
        self._base = 0  # stream offset of buffer[0]
        self.frames: List[RawFrame] = []
        self.discarded: List[DiscardedRange] = []

    # ------------------------------------------------------------- feeding --

    def feed(self, chunk: bytes) -> None:
        """Append one chunk and extract every frame now fully available."""
        self._buffer.extend(chunk)
        self._pump()
        if len(self._buffer) > MAX_BUFFER_SIZE:
            raise FramingError(
                f"unframed stream buffer exceeded {MAX_BUFFER_SIZE} bytes "
                f"(have {len(self._buffer)}): no frame delimiter found within the limit"
            )

    def finish(self) -> bytes:
        """Return the bytes still buffered after the final chunk.

        Anything retained here is a potentially incomplete frame (or a
        trailing partial sync marker) and must be reported, never treated as
        an error.
        """
        return bytes(self._buffer)

    # -------------------------------------------------------------- engine --

    def _pump(self) -> None:
        spec = self.spec
        sync = spec.sync
        offset = spec.length_field_offset
        width = spec.length_field_bytes
        length_end = offset + width

        while True:
            idx = self._buffer.find(sync)
            if idx < 0:
                # No complete marker anywhere. A candidate marker can only
                # live in the final len(sync)-1 bytes (it must start at a
                # real sync-start byte); discard everything else, then keep
                # a matching suffix across this chunk boundary.
                tail_start = max(len(self._buffer) - (len(sync) - 1), 0)
                last_start = self._buffer.rfind(sync[:1], tail_start)
                if last_start < 0:
                    self._discard(len(self._buffer), REASON_NOISE)
                    return
                if last_start > 0:
                    self._discard(last_start, REASON_NOISE)
                keep = _trailing_sync_prefix_len(bytes(self._buffer), sync)
                if len(self._buffer) > keep:
                    self._discard(len(self._buffer) - keep, REASON_NOISE)
                return

            # Noise preceding the (candidate) sync is gone for good.
            if idx > 0:
                self._discard(idx, REASON_NOISE)

            available = len(self._buffer)
            if available < length_end:
                # Header (or sync + length field) not fully received yet.
                return

            raw_len = int.from_bytes(
                self._buffer[offset:length_end], spec.length_endianness, signed=False
            )
            frame_len = length_end + raw_len + spec.length_adjustment

            if frame_len > spec.max_frame_size:
                # A declared frame beyond the hard cap is a stream-level
                # failure, not noise: the next bytes are very likely the
                # body of an oversized frame that must not be buffered.
                raise FramingError(
                    f"frame length {frame_len} at offset {self._base} exceeds the "
                    f"{spec.max_frame_size}-byte limit (length field value {raw_len}"
                    f"{f', adjustment {spec.length_adjustment}' if spec.length_adjustment else ''})"
                )
            if frame_len < length_end:
                self._reject_one_byte(
                    REASON_INVALID_LENGTH,
                    f"length field value {raw_len} with adjustment "
                    f"{spec.length_adjustment} yields an illegal frame length "
                    f"{frame_len} (shorter than its {length_end}-byte header)",
                )
                continue
            if available < frame_len:
                # Wait for the rest of the frame; the length field stays
                # re-readable when more bytes arrive.
                return

            candidate = bytes(self._buffer[:frame_len])
            try:
                result = self._validate(candidate)
            except RejectFrame as exc:
                self._reject_one_byte(
                    exc.reason,
                    exc.detail,
                    failures=exc.failures,
                    candidates=exc.candidates,
                )
                continue

            start = self._base
            self.frames.append(
                RawFrame(
                    start=start,
                    end=start + frame_len,
                    hex=candidate.hex(),
                    result=result,
                )
            )
            del self._buffer[:frame_len]
            self._base += frame_len

    def _reject_one_byte(
        self,
        reason: str,
        detail: str,
        failures: Optional[list] = None,
        candidates: Optional[list] = None,
    ) -> None:
        """Discard exactly the candidate sync byte and resync past it."""
        self._discard(1, reason, detail, failures, candidates)

    def _discard(
        self,
        count: int,
        reason: str,
        detail: Optional[str] = None,
        failures: Optional[list] = None,
        candidates: Optional[list] = None,
    ) -> None:
        if count <= 0:
            return
        start = self._base
        dropped = bytes(self._buffer[:count])
        self.discarded.append(
            DiscardedRange(
                start=start,
                end=start + count,
                reason=reason,
                detail=detail,
                hex=dropped.hex(),
                failures=failures,
                candidates=candidates,
            )
        )
        del self._buffer[:count]
        self._base += count
