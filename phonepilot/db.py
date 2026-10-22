"""SQLite registry for devices, runs, and autorun schedules.

The database is deliberately boring: one file, plain SQL, no migrations
framework. ``init`` is idempotent and every write commits immediately.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import DeviceError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
KINDS = ("esp32", "iphone", "mock")

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    host        TEXT,
    port        INTEGER NOT NULL DEFAULT 80,
    token       TEXT,
    bridge      TEXT,
    status      TEXT NOT NULL DEFAULT 'unknown',
    last_seen   TEXT,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    workflow    TEXT NOT NULL,
    device      TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT 'cli',
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS run_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    step_id     TEXT NOT NULL,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    name        TEXT PRIMARY KEY,
    workflow    TEXT NOT NULL,
    device      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    at_time     TEXT,
    interval_s  INTEGER,
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run    TEXT,
    next_run    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_schedules_next ON schedules(next_run);
"""


def utcnow() -> datetime:
    """Timezone aware UTC timestamp."""
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """Serialize a datetime without microseconds."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp, assuming UTC when no offset is present."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class Registry:
    """Thin wrapper around a SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- lifecycle -----------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Registry:  # noqa: PYI034
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- devices -------------------------------------------------------
    def add_device(
        self,
        name: str,
        kind: str = "esp32",
        host: str | None = None,
        port: int = 80,
        token: str | None = None,
        bridge: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """Register a device. Names are stable, lowercase and unique."""
        name = (name or "").strip().lower()
        if not NAME_RE.match(name):
            raise DeviceError(
                "device name must be 2-32 characters of lowercase letters, digits, hyphens or underscores"
            )
        if kind not in KINDS:
            raise DeviceError(
                f"unknown device kind {kind!r}; use one of {', '.join(KINDS)}"
            )
        if kind == "esp32" and not host:
            raise DeviceError("an esp32 device needs --host (the board IP or hostname)")
        if kind == "iphone":
            if not bridge:
                raise DeviceError(
                    "an iphone device needs --bridge pointing at an esp32 device"
                )
            parent = self.get_device(bridge)
            if parent is None:
                raise DeviceError(f"bridge {bridge!r} is not registered")
            if parent["kind"] not in ("esp32", "mock"):
                raise DeviceError(f"bridge {bridge!r} must be an esp32 or mock device")
        if not (1 <= int(port) <= 65535):
            raise DeviceError("port must be between 1 and 65535")
        try:
            cur = self._conn.execute(
                """INSERT INTO devices (name, kind, host, port, token, bridge, notes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    kind,
                    host.rstrip("/") if host else None,
                    int(port),
                    token,
                    bridge,
                    notes,
                    iso(utcnow()),
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DeviceError(f"device {name!r} already exists") from exc
        row = self._conn.execute(
            "SELECT * FROM devices WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)

    def get_device(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM devices WHERE name = ?", ((name or "").strip().lower(),)
        ).fetchone()
        return dict(row) if row else None

    def require_device(self, name: str) -> dict[str, Any]:
        device = self.get_device(name)
        if device is None:
            raise DeviceError(
                f"device {name!r} is not registered; run: phonepilot fleet add {name}"
            )
        return device

    def list_devices(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM devices ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def remove_device(self, name: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM devices WHERE name = ?", ((name or "").strip().lower(),)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_device_status(self, name: str, status: str, seen: bool = True) -> None:
        now = iso(utcnow()) if seen else None
        if now:
            self._conn.execute(
                "UPDATE devices SET status = ?, last_seen = ? WHERE name = ?",
                (status, now, name),
            )
        else:
            self._conn.execute(
                "UPDATE devices SET status = ? WHERE name = ?", (status, name)
            )
        self._conn.commit()

    def control_device(self, device: dict[str, Any]) -> dict[str, Any]:
        """Return the device that actually accepts HTTP commands.

        An iPhone has no server of its own. Its Shortcut polls the bridge
        board, so every command for the phone is sent to the ESP32.
        """
        if device["kind"] == "iphone":
            bridge = device.get("bridge")
            if not bridge:
                raise DeviceError(f"iphone {device['name']!r} has no bridge")
            parent = self.get_device(bridge)
            if parent is None:
                raise DeviceError(
                    f"bridge {bridge!r} for {device['name']!r} is missing"
                )
            return parent
        return device

    # -- runs ----------------------------------------------------------
    def start_run(
        self, run_id: str, workflow: str, device: str, trigger: str = "cli"
    ) -> None:
        self._conn.execute(
            """INSERT INTO runs (id, workflow, device, trigger, status, started_at)
               VALUES (?, ?, ?, ?, 'running', ?)""",
            (run_id, workflow, device, trigger, iso(utcnow())),
        )
        self._conn.commit()

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, error, iso(utcnow()), run_id),
        )
        self._conn.commit()

    def record_step(
        self,
        run_id: str,
        idx: int,
        step_id: str,
        step_type: str,
        status: str,
        detail: str = "",
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO run_steps
               (run_id, idx, step_id, type, status, started_at, finished_at, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                idx,
                step_id,
                step_type,
                status,
                started_at or iso(utcnow()),
                finished_at or iso(utcnow()),
                detail[:2000],
            ),
        )
        self._conn.commit()

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM run_steps WHERE run_id = ? ORDER BY idx", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- schedules -----------------------------------------------------
    def add_schedule(
        self,
        name: str,
        workflow: str,
        device: str,
        kind: str,
        next_run: str,
        at_time: str | None = None,
        interval_s: int | None = None,
    ) -> dict[str, Any]:
        name = (name or "").strip().lower()
        if not NAME_RE.match(name):
            raise DeviceError(
                "schedule name must be lowercase letters, digits, hyphens or underscores"
            )
        if kind not in ("interval", "daily"):
            raise DeviceError("schedule kind must be interval or daily")
        if kind == "interval" and not interval_s:
            raise DeviceError("interval schedules need interval_s")
        if kind == "daily" and not at_time:
            raise DeviceError("daily schedules need at_time (HH:MM)")
        try:
            self._conn.execute(
                """INSERT INTO schedules
                   (name, workflow, device, kind, at_time, interval_s, next_run, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    workflow,
                    device,
                    kind,
                    at_time,
                    interval_s,
                    next_run,
                    iso(utcnow()),
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DeviceError(f"schedule {name!r} already exists") from exc
        row = self._conn.execute(
            "SELECT * FROM schedules WHERE name = ?", (name,)
        ).fetchone()
        return dict(row)

    def list_schedules(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM schedules ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def remove_schedule(self, name: str) -> bool:
        cur = self._conn.execute("DELETE FROM schedules WHERE name = ?", (name,))
        self._conn.commit()
        return cur.rowcount > 0

    def set_schedule_enabled(self, name: str, enabled: bool) -> bool:
        cur = self._conn.execute(
            "UPDATE schedules SET enabled = ? WHERE name = ?",
            (1 if enabled else 0, name),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def due_schedules(self, now: datetime) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ? ORDER BY next_run",
            (iso(now),),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_schedule_ran(
        self, name: str, ran_at: datetime, next_run: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE schedules SET last_run = ?, next_run = ? WHERE name = ?",
            (iso(ran_at), iso(next_run), name),
        )
        self._conn.commit()


def rows_to_table(rows: Iterable[dict[str, Any]], columns: list[str]) -> str:
    """Render rows as a plain text table for CLI output."""
    rows = list(rows)
    if not rows:
        return "(nothing yet)"
    widths = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            widths[c] = max(widths[c], len(str(row.get(c, ""))))
    header = "  ".join(c.ljust(widths[c]) for c in columns).rstrip()
    rule = "  ".join("-" * widths[c] for c in columns).rstrip()
    lines = [header, rule]
    for row in rows:
        lines.append(
            "  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns).rstrip()
        )
    return "\n".join(lines)
