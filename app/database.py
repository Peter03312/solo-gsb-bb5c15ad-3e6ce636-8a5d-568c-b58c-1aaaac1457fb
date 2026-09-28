"""SQLite persistence, auto-initialized on startup."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DB_PATH = os.environ.get("DB_PATH", "parser.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS definitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_type TEXT    NOT NULL,
    version     TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    spec        TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    UNIQUE (device_type, version)
);

CREATE TABLE IF NOT EXISTS fixtures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_type TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    version     TEXT    NOT NULL,
    hex         TEXT    NOT NULL,
    snapshot    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE (device_type, name)
);

CREATE TABLE IF NOT EXISTS request_latency (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    TEXT    NOT NULL,
    method        TEXT    NOT NULL,
    path          TEXT    NOT NULL,
    status_code   INTEGER NOT NULL,
    started_at    TEXT    NOT NULL,
    ended_at      TEXT    NOT NULL,
    duration_ms   REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_request_latency_started_at
    ON request_latency (started_at);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    spec = json.loads(row["spec"])
    return {
        "device_type": row["device_type"],
        "version": row["version"],
        "description": row["description"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "fields": spec["fields"],
        "checksum": spec["checksum"],
    }


def version_key(version: str):
    """Sort key so that 1.10.0 > 1.9.0; non-numeric parts fall back to string order."""
    parts = []
    for part in version.split("."):
        if part.isdigit():
            parts.append((0, int(part), ""))
        else:
            parts.append((1, 0, part))
    return parts


def create_definition(
    device_type: str,
    version: str,
    description: str,
    fields: List[Dict[str, Any]],
    checksum: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Returns the created record, or None if (device_type, version) already exists."""
    spec = json.dumps({"fields": fields, "checksum": checksum})
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO definitions (device_type, version, description, spec, active, created_at)"
                " VALUES (?, ?, ?, ?, 1, ?)",
                (device_type, version, description, spec, created_at),
            )
    except sqlite3.IntegrityError:
        return None
    return get_definition(device_type, version)


def get_definition(device_type: str, version: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM definitions WHERE device_type = ? AND version = ?",
            (device_type, version),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_latest(device_type: str, active_only: bool = True) -> Optional[Dict[str, Any]]:
    query = "SELECT * FROM definitions WHERE device_type = ?"
    if active_only:
        query += " AND active = 1"
    with _connect() as conn:
        rows = conn.execute(query, (device_type,)).fetchall()
    if not rows:
        return None
    return _row_to_dict(max(rows, key=lambda r: version_key(r["version"])))


def list_definitions(
    device_type: Optional[str] = None, include_inactive: bool = False
) -> List[Dict[str, Any]]:
    query = "SELECT * FROM definitions"
    clauses, params = [], []
    if device_type:
        clauses.append("device_type = ?")
        params.append(device_type)
    if not include_inactive:
        clauses.append("active = 1")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY device_type, created_at"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def deactivate(device_type: str, version: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE definitions SET active = 0 WHERE device_type = ? AND version = ?",
            (device_type, version),
        )
    if cursor.rowcount == 0:
        return None
    return get_definition(device_type, version)


# ------------------------------------------------------------------ fixtures --


def _fixture_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "device_type": row["device_type"],
        "name": row["name"],
        "version": row["version"],
        "hex": row["hex"],
        "snapshot": json.loads(row["snapshot"]),
        "created_at": row["created_at"],
    }


def create_fixture(
    device_type: str,
    name: str,
    version: str,
    hex_payload: str,
    snapshot: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Returns the created fixture, or None if (device_type, name) already exists."""
    snapshot_json = json.dumps(snapshot)
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO fixtures (device_type, name, version, hex, snapshot, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (device_type, name, version, hex_payload, snapshot_json, created_at),
            )
    except sqlite3.IntegrityError:
        return None
    return get_fixture(device_type, name)


def get_fixture(device_type: str, name: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM fixtures WHERE device_type = ? AND name = ?",
            (device_type, name),
        ).fetchone()
    return _fixture_row_to_dict(row) if row else None


def get_fixture_by_name(name: str) -> List[Dict[str, Any]]:
    """Look up a fixture by name across device types (names are only unique
    *within* a device type)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM fixtures WHERE name = ? ORDER BY device_type, created_at",
            (name,),
        ).fetchall()
    return [_fixture_row_to_dict(r) for r in rows]


def list_fixtures(device_type: Optional[str] = None) -> List[Dict[str, Any]]:
    query = "SELECT * FROM fixtures"
    params: List[Any] = []
    if device_type:
        query += " WHERE device_type = ?"
        params.append(device_type)
    query += " ORDER BY device_type, created_at"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_fixture_row_to_dict(r) for r in rows]


def delete_fixture(device_type: str, name: str) -> bool:
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM fixtures WHERE device_type = ? AND name = ?",
            (device_type, name),
        )
    return cursor.rowcount > 0


# --------------------------------------------------------- request latency --


def record_request_latency(
    request_id: str,
    method: str,
    path: str,
    status_code: int,
    started_at: str,
    ended_at: str,
    duration_ms: float,
) -> None:
    """Persist one API request timing record. Called by the latency-tracking
    middleware; failures propagate to the caller, which must never let them
    affect the original response."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO request_latency"
            " (request_id, method, path, status_code, started_at, ended_at, duration_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request_id, method, path, status_code, started_at, ended_at, duration_ms),
        )


def list_request_latency(
    start: Optional[str] = None,
    end: Optional[str] = None,
    path_prefix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return latency rows matching the filters. Time filtering uses the
    half-open interval [start, end). Timestamps are normalized UTC ISO-8601
    strings of fixed width, so lexicographic comparison equals chronological
    order; ``path`` is filtered by a plain string-prefix match."""
    clauses, params = [], []
    if start is not None:
        clauses.append("started_at >= ?")
        params.append(start)
    if end is not None:
        clauses.append("started_at < ?")
        params.append(end)
    if path_prefix:
        clauses.append("substr(path, 1, length(?)) = ?")
        params.extend((path_prefix, path_prefix))
    query = "SELECT * FROM request_latency"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY started_at, id"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "request_id": row["request_id"],
            "method": row["method"],
            "path": row["path"],
            "status_code": row["status_code"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "duration_ms": row["duration_ms"],
        }
        for row in rows
    ]
