"""Request-latency tracing and observability query API.

Every API request except ``/health`` is wrapped by
:class:`LatencyTrackingMiddleware`, which records ``request_id``, HTTP method,
path, status code, start/end timestamps and duration (milliseconds) into the
``request_latency`` SQLite table. Recording failures are swallowed (and logged)
so that a persistence problem never changes the original response.

``GET /api/v1/observability/latency`` aggregates the recorded requests over a
half-open ``[start, end)`` time window and an optional path prefix, returning
the total/error counts, p50/p95 latency and a per-status-code breakdown.
"""

import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.middleware.base import BaseHTTPMiddleware

from . import database

logger = logging.getLogger("app.observability")

router = APIRouter()

# /health is liveness plumbing rather than business traffic and must stay
# answerable even when the latency store is unavailable, so it is never traced.
EXCLUDED_PATHS = frozenset({"/health"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(dt: datetime) -> str:
    """Render a datetime as fixed-width, microsecond-precision UTC ISO-8601.
    Fixed width makes lexicographic string comparison (used by the SQLite time
    filters) identical to chronological order."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_ts(value: str, field: str) -> datetime:
    """Parse a query timestamp. Accepts ISO-8601 (a trailing ``Z``/``z`` is
    allowed as a UTC alias); a naive timestamp is assumed to be UTC. Anything
    else -- including unix numbers and non-ISO strings -- is a 422."""
    try:
        dt = datetime.fromisoformat(value.replace("z", "+00:00").replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field} must be an ISO-8601 timestamp"
                f" (e.g. 2026-09-22T00:00:00Z), got {value!r}"
            ),
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def percentile(sorted_values: list[float], pct: int) -> Optional[float]:
    """Nearest-rank percentile (deterministic, no interpolation). Rank k is the
    smallest rank with k/N >= pct/100; returns None for an empty sample."""
    if not sorted_values:
        return None
    rank = math.ceil(pct / 100 * len(sorted_values))
    return sorted_values[rank - 1]


class LatencyTrackingMiddleware(BaseHTTPMiddleware):
    """Trace every API request except ``EXCLUDED_PATHS``.

    Each request gets a UUID ``request_id`` (echoed back in the
    ``X-Request-ID`` response header); timing uses ``perf_counter`` for the
    duration and wall-clock UTC timestamps for start/end. Any failure while
    persisting the record is logged and swallowed -- tracing must never alter
    the status code or body the endpoint produced.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)

        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        started_dt = utc_now()
        started_perf = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            ended_dt = utc_now()
            duration_ms = round((time.perf_counter() - started_perf) * 1000, 3)
            started_at = format_ts(started_dt)
            ended_at = format_ts(ended_dt)
            # Best-effort persistence: never let a write failure change the
            # outcome of the traced request.
            try:
                database.record_request_latency(
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_ms=duration_ms,
                )
            except Exception:  # noqa: BLE001 - tracing must never break requests
                logger.exception(
                    "failed to record latency for request %s %s %s",
                    request_id,
                    request.method,
                    request.url.path,
                )


@router.get("/api/v1/observability/latency")
def latency_report(
    start: Optional[str] = Query(
        default=None,
        description="Window start (inclusive), ISO-8601; naive values are UTC.",
    ),
    end: Optional[str] = Query(
        default=None,
        description="Window end (exclusive), ISO-8601; naive values are UTC.",
    ),
    path_prefix: Optional[str] = Query(
        default=None, description="Only include requests whose path starts with this prefix."
    ),
):
    """Aggregate request latency over a half-open ``[start, end)`` window.

    Returns the total request count, error count (status code >= 400),
    p50/p95 duration in milliseconds (nearest-rank) and a per-status-code
    breakdown. Malformed timestamps or ``start >= end`` yield 422.
    """
    start_dt = parse_ts(start, "start") if start is not None else None
    end_dt = parse_ts(end, "end") if end is not None else None
    if start_dt is not None and end_dt is not None and start_dt >= end_dt:
        raise HTTPException(
            status_code=422,
            detail="start must be earlier than end (time range is half-open [start, end))",
        )

    prefix = path_prefix.strip() if path_prefix else None
    rows = database.list_request_latency(
        start=format_ts(start_dt) if start_dt is not None else None,
        end=format_ts(end_dt) if end_dt is not None else None,
        path_prefix=prefix,
    )

    durations = sorted(row["duration_ms"] for row in rows)
    status_counts: dict[str, int] = {}
    error_count = 0
    for row in rows:
        code = str(row["status_code"])
        status_counts[code] = status_counts.get(code, 0) + 1
        if row["status_code"] >= 400:
            error_count += 1

    return {
        "filters": {
            "start": format_ts(start_dt) if start_dt is not None else None,
            "end": format_ts(end_dt) if end_dt is not None else None,
            "path_prefix": prefix,
        },
        "total_requests": len(rows),
        "error_requests": error_count,
        "latency_ms": {
            "p50": percentile(durations, 50),
            "p95": percentile(durations, 95),
        },
        "status_codes": [
            {"status_code": int(code), "count": count}
            for code, count in sorted(status_counts.items())
        ],
    }
