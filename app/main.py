"""Industrial binary message parsing API."""

import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response

from . import database
from .analyzer import CompatibilityError, analyze_compatibility
from .encoder import EncodeError, encode_message
from .fixtures import capture_snapshot, compare_snapshots, verify_expectation
from .observability import LatencyTrackingMiddleware, router as observability_router
from .framing import (
    MAX_BUFFER_SIZE,
    MAX_FRAME_SIZE,
    FrameSpec,
    FramingError,
    REASON_AMBIGUOUS_VERSION,
    REASON_CHECKSUM,
    REASON_PARSE_ERROR,
    RejectFrame,
    StreamFramer,
)
from .parser import ParseError, parse_message
from .profiler import VersionProfile
from .schemas import (
    CompatibilityRequest,
    DefinitionCreate,
    DetectRequest,
    EncodeRequest,
    FixtureCreate,
    FixtureRunRequest,
    MessageProfileRequest,
    ParseRequest,
    StreamParseRequest,
    validate_definition,
)


# Route served by the batch profiling processor (app/profiler.py). It is
# registered as an ordinary @app.post below; ``_ensure_profiler_route`` runs at
# application startup so that, if the handler is ever not wired onto the app,
# the service fails loudly at boot instead of answering a silent 404 after
# startup.
BATCH_PROFILE_ROUTE = ("POST", "/api/v1/messages/profile")


def _ensure_profiler_route() -> None:
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path == BATCH_PROFILE_ROUTE[1] and methods and BATCH_PROFILE_ROUTE[0] in methods:
            return
    raise RuntimeError(
        f"batch profiler route {BATCH_PROFILE_ROUTE[0]} {BATCH_PROFILE_ROUTE[1]} "
        "is not registered: the batch profiling processor did not load with the "
        "application"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    # Confirm the batch profiling handler shipped with the running app before
    # the service starts accepting traffic.
    _ensure_profiler_route()
    yield


app = FastAPI(
    title="Industrial Binary Message Parser",
    description=(
        "Register versioned binary message definitions (bit widths, endianness, "
        "conditional branches, checksums) and parse hex payloads against them."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Make the service usable even if the lifespan hook is bypassed (e.g. tests).
database.init_db()

# Record request latency for every API request except /health. Failures while
# writing a record are contained inside the middleware and never alter the
# traced response.
app.add_middleware(LatencyTrackingMiddleware)
app.include_router(observability_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/definitions", status_code=201)
def register_definition(defn: DefinitionCreate):
    """Register a new versioned message definition."""
    try:
        validate_definition(defn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    record = database.create_definition(
        device_type=defn.device_type,
        version=defn.version,
        description=defn.description,
        fields=[f.model_dump(exclude_none=True) for f in defn.fields],
        checksum=defn.checksum.model_dump() if defn.checksum else None,
    )
    if record is None:
        raise HTTPException(
            status_code=409,
            detail=f"definition '{defn.device_type}' version '{defn.version}' already exists",
        )
    return record


@app.get("/api/v1/definitions")
def list_definitions(
    device_type: Optional[str] = Query(default=None),
    include_inactive: bool = Query(default=False),
):
    """List registered definitions (active ones by default)."""
    return {
        "count": len(
            items := database.list_definitions(device_type, include_inactive)
        ),
        "items": items,
    }


@app.get("/api/v1/definitions/{device_type}/latest")
def get_latest_definition(device_type: str):
    """Get the latest active version for a device type."""
    record = database.get_latest(device_type, active_only=True)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"no active definition found for device type '{device_type}'",
        )
    return record


@app.get("/api/v1/definitions/{device_type}/{version}")
def get_definition(device_type: str, version: str):
    record = database.get_definition(device_type, version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{version}' not found",
        )
    return record


@app.post("/api/v1/definitions/{device_type}/{version}/deactivate")
def deactivate_definition(device_type: str, version: str):
    """Deactivate a definition. It stays parseable by explicit version but is
    excluded from listing (by default) and from latest-version lookups."""
    record = database.deactivate(device_type, version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{version}' not found",
        )
    return record


@app.post("/api/v1/definitions/{device_type}/compatibility")
def compatibility(device_type: str, req: CompatibilityRequest):
    """Analyze wire compatibility between two registered versions of the same
    device type. Conditions are partitioned into finite equivalence classes,
    reachable layouts (field bit offsets + total length) are enumerated, and
    each old-version layout must find a new-version layout with identical
    field names/types/bit widths/offsets, total length and checksum algorithm.
    Returns ``breaking`` with per-branch differences otherwise; more than 256
    equivalence-class combinations yields 422."""
    if req.old_version == req.new_version:
        raise HTTPException(
            status_code=422,
            detail="old_version and new_version must differ",
        )
    old_record = database.get_definition(device_type, req.old_version)
    if old_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{req.old_version}' not found",
        )
    new_record = database.get_definition(device_type, req.new_version)
    if new_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{req.new_version}' not found",
        )
    try:
        return analyze_compatibility(old_record, new_record)
    except CompatibilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


_HEX_CLEANER = re.compile(r"(0[xX])|[\s_-]")


def _clean_hex(hex_payload: str):
    """Strip whitespace/separators/0x prefix and decode. Returns
    ``(data, None)`` or ``(None, reason)`` on an empty or non-hex payload."""
    cleaned = _HEX_CLEANER.sub("", hex_payload)
    if not cleaned:
        return None, "hex payload is empty"
    try:
        return bytes.fromhex(cleaned), None
    except ValueError:
        return None, (
            "invalid hex payload: non-hex characters or odd number of digits"
        )


def _decode_hex(hex_payload: str) -> bytes:
    """Shared by /parse and /detect. Raises HTTP 422 on a bad payload."""
    data, reason = _clean_hex(hex_payload)
    if reason is not None:
        raise HTTPException(status_code=422, detail=reason)
    return data


def _detect_versions(records, data: bytes, unit: str = "message"):
    """Shared by /detect and /messages/profile: try every registered version
    of a device type (including inactive ones, in version order) against one
    complete byte payload, using the same acceptance rule as
    :func:`_match_definition` (full consumption + checksum verification).

    Returns ``(matched_records, matched_result, failures)`` where
    ``matched_records`` is the ordered list of accepting records,
    ``matched_result`` is the parse result of the last (only meaningful when
    exactly one matches), and ``failures`` lists per-version failure reasons.
    """
    matched_records = []
    matched_result = None
    failures = []
    for record in records:
        result, rejection = _match_definition(record, data, unit=unit)
        if rejection is None:
            matched_records.append(record)
            matched_result = result
        else:
            failures.append(
                {
                    "version": record["version"],
                    "active": record["active"],
                    "reason": rejection[1],
                }
            )
    return matched_records, matched_result, failures


def _no_detection_reason(failures, unit: str = "message") -> str:
    """Umbrella detail text when detection finds zero matching versions."""
    reasons = "; ".join(f"{f['version']}: {f['reason']}" for f in failures)
    return (
        f"no registered version matches the {unit} ({len(failures)} "
        f"version(s) tried): {reasons}"
    )


def _ambiguous_detection_reason(versions) -> str:
    """Detail text when detection matches several versions."""
    return (
        f"message matches {len(versions)} registered versions "
        f"({', '.join(versions)}); cannot pick a version without an explicit hint"
    )


def _select_definition(device_type: str, version: Optional[str]) -> dict:
    """Shared by /parse and /encode: an explicit version selects that definition
    (even if inactive); otherwise the latest active version is used."""
    if version:
        record = database.get_definition(device_type, version)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"definition '{device_type}' version '{version}' not found",
            )
    else:
        record = database.get_latest(device_type, active_only=True)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"no active definition found for device type '{device_type}'",
            )
    return record


def _match_definition(record: dict, data: bytes, unit: str = "message"):
    """Shared by /detect and detect-mode /stream/parse: decide whether one
    registered version accepts a complete byte payload.

    A version accepts when the definition fully consumes the payload (no
    trailing bits) and, when a checksum is configured, its verification
    passes (a skipped checksum field counts as a failure); definitions
    without a checksum are judged on full consumption alone.

    Returns ``(result, None)`` on acceptance, otherwise
    ``(None, (reason, detail))`` where ``reason`` is ``REASON_CHECKSUM`` for
    a mismatch and ``REASON_PARSE_ERROR`` for every other failure.
    """
    try:
        result = parse_message(record["fields"], record["checksum"], data)
    except ParseError as exc:
        return None, (REASON_PARSE_ERROR, str(exc))
    if result["trailing_bits"]:
        return None, (
            REASON_PARSE_ERROR,
            f"definition does not fully consume the {unit}: "
            f"{result['trailing_bits']} trailing bit(s)",
        )
    if record["checksum"]:
        checksum = result["checksum"]
        if checksum is None:
            return None, (
                REASON_PARSE_ERROR,
                f"checksum field '{record['checksum']['field']}' was not parsed "
                f"(skipped by an earlier branch)",
            )
        if not checksum["valid"]:
            return None, (
                REASON_CHECKSUM,
                f"checksum mismatch: expected {checksum['expected_hex']}, "
                f"calculated {checksum['calculated_hex']}",
            )
    return result, None


def _parse_result_for_record(record: dict, result: dict) -> dict:
    """Shape the parser output exactly like /parse for a matched record."""
    return {
        "device_type": record["device_type"],
        "version": record["version"],
        "definition_active": record["active"],
        **result,
    }


@app.post("/api/v1/parse")
def parse(req: ParseRequest):
    """Parse a hex payload against a definition (latest active version unless
    an explicit version is given)."""
    record = _select_definition(req.device_type, req.version)

    data = _decode_hex(req.hex)

    try:
        result = parse_message(record["fields"], record["checksum"], data)
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "device_type": record["device_type"],
        "version": record["version"],
        "definition_active": record["active"],
        **result,
    }


@app.post("/api/v1/detect")
def detect(req: DetectRequest):
    """Identify the message version without a version hint: try every registered
    version of the device type (including inactive ones) in version order.

    A version is a candidate only when the definition fully consumes the message
    (no trailing bits) and, when a checksum is configured, its verification
    passes; definitions without a checksum are judged on full consumption alone.
    Exactly one candidate returns the parse result; zero candidates returns 422
    with the per-version failure reason; several candidates return 409 with the
    candidate list — the latest version is never picked silently.
    """
    records = database.list_definitions(req.device_type, include_inactive=True)
    if not records:
        raise HTTPException(
            status_code=404,
            detail=f"no definitions found for device type '{req.device_type}'",
        )
    records.sort(key=lambda r: database.version_key(r["version"]))

    data = _decode_hex(req.hex)

    matched_records, matched_result, failures = _detect_versions(records, data)

    if len(matched_records) == 1:
        return _parse_result_for_record(matched_records[0], matched_result)

    if not matched_records:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "no registered version matches the message",
                "device_type": req.device_type,
                "failures": failures,
            },
        )

    raise HTTPException(
        status_code=409,
        detail={
            "message": "message matches multiple registered versions; "
            "an explicit version is required",
            "device_type": req.device_type,
            "candidates": [
                {"version": r["version"], "active": r["active"]}
                for r in matched_records
            ],
        },
    )


@app.post("/api/v1/messages/profile")
def messages_profile(req: MessageProfileRequest):
    """Parse 1-500 hex samples and return a statistical field profile grouped
    by message version.

    Each sample is processed independently, so one bad sample never aborts the
    batch. An explicit ``version`` selects that definition (including inactive
    ones); an omitted version is resolved per sample with the same rules as
    ``/detect`` -- every registered version is tried in version order and a
    sample counts only when exactly one version fully consumes it and passes
    its checksum. Explicitly-versioned samples are accepted under the same
    gate (full consumption + checksum verification): a checksum mismatch,
    trailing bits or a checksum field skipped by a branch rejects the sample.

    Rejected samples never enter the statistics. A rejection attributable to a
    known version (explicit version given, parse/checksum failure) is listed in
    that version group; failures without an attributable version -- bad hex, an
    unknown explicit version, or a versionless sample with zero / several
    detect candidates -- go to the top-level ``rejected`` list.

    Each version group reports ``accepted_count`` and, per field in definition
    order, ``present_count`` plus min/max/mean for numeric scalar values and
    for every element of numeric repeat arrays (the element count is the
    denominator, returned as ``element_count``). Fields skipped by a missed
    condition are simply absent, so they count neither as present nor into any
    denominator. Same-named fields of different versions are always isolated in
    their own groups. Version groups come out in version order and their
    rejected entries in request-index order.
    """
    records = database.list_definitions(req.device_type, include_inactive=True)
    if not records:
        raise HTTPException(
            status_code=404,
            detail=f"no definitions found for device type '{req.device_type}'",
        )
    records.sort(key=lambda r: database.version_key(r["version"]))
    records_by_version = {r["version"]: r for r in records}

    profiles: dict[str, VersionProfile] = {}
    top_rejected: list[dict] = []

    def profile_for(version: str) -> VersionProfile:
        profile = profiles.get(version)
        if profile is None:
            profile = VersionProfile(records_by_version[version])
            profiles[version] = profile
        return profile

    for index, sample in enumerate(req.samples):
        data, reason = _clean_hex(sample.hex)
        if reason is not None:
            top_rejected.append({"index": index, "reason": reason})
            continue

        if sample.version is not None:
            record = records_by_version.get(sample.version)
            if record is None:
                top_rejected.append(
                    {
                        "index": index,
                        "reason": (
                            f"definition '{req.device_type}' version "
                            f"'{sample.version}' not found"
                        ),
                    }
                )
                continue
            result, rejection = _match_definition(record, data, unit="message")
            if rejection is not None:
                profile_for(record["version"]).rejected.append(
                    {"index": index, "reason": rejection[1]}
                )
                continue
            profile_for(record["version"]).add_accepted(result)
            continue

        matched_records, matched_result, failures = _detect_versions(
            records, data, unit="message"
        )
        if len(matched_records) == 1:
            record = matched_records[0]
            profile_for(record["version"]).add_accepted(matched_result)
        elif not matched_records:
            top_rejected.append(
                {"index": index, "reason": _no_detection_reason(failures)}
            )
        else:
            versions = [r["version"] for r in matched_records]
            top_rejected.append(
                {"index": index, "reason": _ambiguous_detection_reason(versions)}
            )

    ordered_versions = sorted(profiles, key=database.version_key)
    groups = [profiles[v].build() for v in ordered_versions]
    accepted_total = sum(group["accepted_count"] for group in groups)
    rejected_total = len(top_rejected) + sum(
        len(group["rejected"]) for group in groups
    )
    return {
        "device_type": req.device_type,
        "sample_count": len(req.samples),
        "accepted_count": accepted_total,
        "rejected_count": rejected_total,
        "rejected": top_rejected,
        "versions": groups,
    }


@app.post("/api/v1/stream/parse")
def stream_parse(req: StreamParseRequest):
    """Reassemble a chunked byte stream, frame it by a sync marker plus an
    unsigned length field, and parse every complete frame with the existing
    parser.

    ``chunks`` are concatenated in order while a cross-chunk buffer is
    maintained: a frame split over several chunks is joined transparently.
    Bytes that cannot start a frame (noise), illegal length values and
    frames that fail to parse or verify are discarded one byte at a time,
    resynchronising at the next sync marker. Bytes left over after the last
    chunk -- a partial frame or a partial sync marker -- come back as
    ``incomplete_hex`` and are never an error. A retained buffer above 1 MiB
    or a single declared frame above 64 KiB returns 422.

    ``version_mode`` selects how each complete frame is validated:

    * ``fixed`` (the default): every frame is parsed against the explicit
      ``version`` or, when omitted, the latest active version -- the original
      behaviour, unchanged.
    * ``detect``: no ``version`` hint is allowed; every complete candidate
      frame is tried against *all* registered versions of the device type,
      including inactive ones. A frame is accepted only when exactly one
      version fully consumes it and verifies its checksum, so one batch of
      chunks may contain frames from mixed firmware versions; each frame
      result then reports its own ``version`` and ``definition_active``.
      Zero matches triggers the same one-byte resynchronisation as fixed
      mode and records every version's failure reason; frames matching
      several versions are discarded as well, but with the distinct
      ``ambiguous_version`` reason and a ``candidates`` list -- the newest
      version is never picked silently.
    """
    detect_mode = req.version_mode == "detect"

    if detect_mode:
        records = database.list_definitions(req.device_type, include_inactive=True)
        if not records:
            raise HTTPException(
                status_code=404,
                detail=f"no definitions found for device type '{req.device_type}'",
            )
        records.sort(key=lambda r: database.version_key(r["version"]))
        selected_version = None
        definition_active = None
    else:
        record = _select_definition(req.device_type, req.version)
        selected_version = record["version"]
        definition_active = record["active"]

    sync = _decode_hex(req.sync_hex)

    header_size = req.length_field_offset + req.length_field_bytes
    if header_size > MAX_FRAME_SIZE:
        raise HTTPException(
            status_code=422,
            detail=(
                f"length field ends at byte {header_size}, beyond the "
                f"{MAX_FRAME_SIZE}-byte maximum frame size"
            ),
        )

    # Decode every chunk up front so malformed hex fails the whole request
    # before any framing state is built.
    pieces: list[bytes] = []
    for index, chunk in enumerate(req.chunks):
        try:
            pieces.append(_decode_hex(chunk))
        except HTTPException as exc:
            raise HTTPException(
                status_code=422,
                detail=f"chunks[{index}]: {exc.detail}",
            )

    spec = FrameSpec(
        sync=sync,
        length_field_offset=req.length_field_offset,
        length_field_bytes=req.length_field_bytes,
        length_endianness=req.length_endianness,
        length_adjustment=req.length_adjustment,
        max_frame_size=MAX_FRAME_SIZE,
    )

    def validate(frame: bytes) -> dict:
        if detect_mode:
            return _validate_frame_detect(frame)
        try:
            result = parse_message(record["fields"], record["checksum"], frame)
        except ParseError as exc:
            raise RejectFrame(str(exc))
        # Frames are byte-granular: a definition leaving bits unconsumed
        # cannot be self-delimited by a byte length field, so resync.
        if result["trailing_bits"]:
            raise RejectFrame(
                f"definition does not fully consume the frame: "
                f"{result['trailing_bits']} trailing bit(s)"
            )
        if record["checksum"]:
            checksum = result["checksum"]
            if checksum is None:
                raise RejectFrame(
                    f"checksum field '{record['checksum']['field']}' was not parsed "
                    f"(skipped by an earlier branch)"
                )
            if not checksum["valid"]:
                raise RejectFrame(
                    f"checksum mismatch: expected {checksum['expected_hex']}, "
                    f"calculated {checksum['calculated_hex']}",
                    reason=REASON_CHECKSUM,
                )
        return {
            "device_type": record["device_type"],
            "version": record["version"],
            "definition_active": record["active"],
            **result,
        }

    def _validate_frame_detect(frame: bytes) -> dict:
        """Try every registered version (including inactive ones), in version
        order. Exactly one match accepts the frame; zero matches resyncs with
        per-version failure reasons; several matches resync with an explicit
        ``ambiguous_version`` rejection instead of guessing the newest one."""
        candidates = []
        failures = []
        for record in records:
            result, rejection = _match_definition(record, frame, unit="frame")
            if rejection is None:
                candidates.append(
                    {"version": record["version"], "active": record["active"]}
                )
                matched_result = _parse_result_for_record(record, result)
            else:
                kind, detail = rejection
                failures.append(
                    {
                        "version": record["version"],
                        "active": record["active"],
                        "reason": detail,
                        "category": kind,
                    }
                )

        if len(candidates) == 1:
            return matched_result

        if not candidates:
            # Mirror the fixed-mode checksum reason only when every version
            # failed at checksum verification; a mix of failures (too short,
            # trailing bits, ...) stays a generic parse rejection.
            all_checksum = bool(failures) and all(
                f["category"] == REASON_CHECKSUM for f in failures
            )
            umbrella = REASON_CHECKSUM if all_checksum else REASON_PARSE_ERROR
            public_failures = [
                {"version": f["version"], "active": f["active"], "reason": f["reason"]}
                for f in failures
            ]
            reasons = "; ".join(
                f"{f['version']}: {f['reason']}" for f in failures
            )
            raise RejectFrame(
                f"no registered version matches the frame ({len(failures)} "
                f"version(s) tried): {reasons}",
                reason=umbrella,
                failures=public_failures,
            )

        raise RejectFrame(
            f"frame matches {len(candidates)} registered versions "
            f"({', '.join(c['version'] for c in candidates)}); "
            f"cannot pick a version without an explicit hint",
            reason=REASON_AMBIGUOUS_VERSION,
            candidates=candidates,
        )

    framer = StreamFramer(spec, validate)
    try:
        for piece in pieces:
            framer.feed(piece)
    except FramingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    incomplete = framer.finish()
    response = {
        "device_type": req.device_type,
        # In detect mode the version is a per-frame attribute; the top-level
        # fields report null rather than silently naming one version.
        "version": selected_version,
        "definition_active": definition_active,
        "version_mode": req.version_mode,
        "sync_hex": sync.hex(),
        "received_bytes": sum(len(p) for p in pieces),
        "frame_count": len(framer.frames),
        "frames": [
            {
                "start": frame.start,
                "end": frame.end,
                "length": frame.end - frame.start,
                "hex": frame.hex,
                "result": frame.result,
            }
            for frame in framer.frames
        ],
        "discarded_count": len(framer.discarded),
        "discarded_bytes": sum(r.end - r.start for r in framer.discarded),
        "discarded_ranges": [],
        "incomplete_bytes": len(incomplete),
        "incomplete_hex": incomplete.hex(),
        "limits": {
            "max_buffer_bytes": MAX_BUFFER_SIZE,
            "max_frame_bytes": MAX_FRAME_SIZE,
        },
    }
    for rng in framer.discarded:
        entry = {
            "start": rng.start,
            "end": rng.end,
            "length": rng.end - rng.start,
            "hex": rng.hex,
            "reason": rng.reason,
            "detail": rng.detail,
        }
        # failures/candidates only appear on detect-mode rejections, keeping
        # the fixed-mode payload key-for-key compatible.
        if rng.failures is not None:
            entry["failures"] = rng.failures
        if rng.candidates is not None:
            entry["candidates"] = rng.candidates
        response["discarded_ranges"].append(entry)
    return response


@app.post("/api/v1/encode")
def encode(req: EncodeRequest):
    """Encode field values into a hex payload (latest active version unless an
    explicit version is given). Conditions are evaluated on raw values; the
    checksum field is computed automatically and must not be provided."""
    record = _select_definition(req.device_type, req.version)

    try:
        result = encode_message(record["fields"], record["checksum"], req.values)
    except EncodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "device_type": record["device_type"],
        "version": record["version"],
        "definition_active": record["active"],
        **result,
    }


# ------------------------------------------------------------ fixtures (回归) --


@app.post("/api/v1/fixtures", status_code=201)
def create_fixture(req: FixtureCreate):
    """Save a named parse-regression fixture.

    The fixture name is unique within a device type. The hex payload is parsed
    once against the referenced version and the resulting field snapshot
    (field names + raw_value, branch path, checksum result) is stored as the
    baseline. An optional ``expect`` block asserts fields/branches/checksum of
    that baseline at creation time; the full captured snapshot is persisted
    regardless and used by ``/run``.
    """
    record = database.get_definition(req.device_type, req.version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{req.device_type}' version '{req.version}' not found",
        )

    data = _decode_hex(req.hex)
    try:
        result = parse_message(record["fields"], record["checksum"], data)
    except ParseError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"baseline parse failed for fixture '{req.name}': {exc}",
        )

    snapshot = capture_snapshot(result)
    if req.expect is not None:
        problems = verify_expectation(
            req.expect.model_dump(exclude_none=True), snapshot
        )
        if problems:
            raise HTTPException(
                status_code=422,
                detail={"message": "fixture expectation does not match baseline", "problems": problems},
            )

    saved = database.create_fixture(
        device_type=req.device_type,
        name=req.name,
        version=req.version,
        hex_payload=req.hex,
        snapshot=snapshot,
    )
    if saved is None:
        raise HTTPException(
            status_code=409,
            detail=f"fixture '{req.name}' already exists for device type '{req.device_type}'",
        )
    return saved


@app.get("/api/v1/fixtures")
def list_fixtures(device_type: Optional[str] = Query(default=None)):
    """List saved regression fixtures (optionally filtered by device type)."""
    items = database.list_fixtures(device_type)
    return {"count": len(items), "items": items}


@app.get("/api/v1/fixtures/{name}")
def get_fixture(name: str, device_type: Optional[str] = Query(default=None)):
    """Get a fixture by name. Names are unique only within a device type:
    pass ``?device_type=...`` to disambiguate, otherwise a name shared by
    several device types returns 409 with the candidates."""
    if device_type is not None:
        fixture = database.get_fixture(device_type, name)
        if fixture is None:
            raise HTTPException(status_code=404, detail=f"fixture '{name}' not found")
        return fixture

    matches = database.get_fixture_by_name(name)
    if not matches:
        raise HTTPException(status_code=404, detail=f"fixture '{name}' not found")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"fixture name '{name}' matches multiple device types",
                "candidates": [
                    {"device_type": m["device_type"], "version": m["version"]} for m in matches
                ],
            },
        )
    return matches[0]


@app.delete("/api/v1/fixtures/{name}", status_code=204)
def delete_fixture(name: str, device_type: Optional[str] = Query(default=None)):
    """Delete a fixture (same disambiguation rule as GET)."""
    if device_type is not None:
        if not database.delete_fixture(device_type, name):
            raise HTTPException(status_code=404, detail=f"fixture '{name}' not found")
        return Response(status_code=204)

    matches = database.get_fixture_by_name(name)
    if not matches:
        raise HTTPException(status_code=404, detail=f"fixture '{name}' not found")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"fixture name '{name}' matches multiple device types",
                "candidates": [
                    {"device_type": m["device_type"], "version": m["version"]} for m in matches
                ],
            },
        )
    database.delete_fixture(matches[0]["device_type"], name)
    return Response(status_code=204)


@app.post("/api/v1/fixtures/{name}/run")
def run_fixture(name: str, req: FixtureRunRequest):
    """Re-run a saved fixture against a parser version and compare with its
    baseline snapshot.

    The body carries the fixture's ``device_type`` (names are unique only
    within a device type) and an optional ``target_version`` (defaults to the
    fixture's baseline version). The payload is parsed against the target
    definition and compared field-by-field: field names with ``raw_value``,
    the conditional branch path, and the checksum validation result. Version
    changes surface as ``added``/``removed``/``changed`` sets plus a pass
    rate; a target version that fails to parse is reported per item (HTTP 200
    with ``parse_error``), never as a 5xx crash.
    """
    fixture = database.get_fixture(req.device_type, name)
    if fixture is None:
        raise HTTPException(
            status_code=404,
            detail=f"fixture '{name}' not found for device type '{req.device_type}'",
        )

    target_version = req.target_version or fixture["version"]
    target = database.get_definition(fixture["device_type"], target_version)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{fixture['device_type']}' version '{target_version}' not found",
        )

    data = _decode_hex(fixture["hex"])

    actual_snapshot = None
    parse_error: Optional[str] = None
    target_checksum_cfg = target["checksum"]
    try:
        result = parse_message(target["fields"], target_checksum_cfg, data)
    except ParseError as exc:
        # Keep the regression report: every baseline item becomes a failure.
        parse_error = str(exc)
        actual_snapshot = {
            "fields": [],
            "branch_path": [],
            "checksum_valid": None,
        }
    else:
        actual_snapshot = capture_snapshot(result)

    comparison = compare_snapshots(fixture["snapshot"], actual_snapshot)
    if parse_error is not None:
        comparison["errors"].insert(0, f"target parse failed: {parse_error}")
        # A failed parse cannot validate even a checksum-free definition.
        if comparison["checksum"]["passed"]:
            comparison["checksum"]["passed"] = False
            comparison["passed_checks"] -= 1
            comparison["pass_rate"] = round(
                comparison["passed_checks"] / comparison["total_checks"], 4
            )

    return {
        "name": name,
        "device_type": fixture["device_type"],
        "baseline_version": fixture["version"],
        "target_version": target_version,
        "version_changed": target_version != fixture["version"],
        "hex": fixture["hex"],
        "baseline": fixture["snapshot"],
        "actual": actual_snapshot,
        "comparison": comparison,
        "parse_error": parse_error,
        "passed": parse_error is None and comparison["passed"],
    }
