"""Autorun scheduling for workflow templates.

Two schedule kinds cover most creator routines:

* ``interval``: every N seconds, stored as next_run in UTC
* ``daily``:    once a day at a local wall clock time (HH:MM)

The scheduler is a plain function called by ``phonepilot autorun``. There is
no daemon and no hidden thread; cron, Task Scheduler, or a terminal loop owns
the timing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Registry, iso, utcnow
from .engine import Runner, RunResult
from .errors import DeviceError, TemplateError
from .templates import Workflow, find_template, load_workflow

DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MIN_INTERVAL_S = 60


def parse_duration(value: str | int) -> int:
    """Parse ``90s``, ``15m``, ``6h`` or ``1d`` into seconds."""
    if isinstance(value, int) and not isinstance(value, bool):
        seconds = value
    else:
        match = DURATION_RE.match(str(value))
        if not match:
            raise DeviceError(f"duration {value!r} must look like 30m, 6h or 1d")
        seconds = int(match.group(1)) * UNIT_SECONDS[match.group(2).lower()]
    if seconds < MIN_INTERVAL_S:
        raise DeviceError(f"interval must be at least {MIN_INTERVAL_S} seconds")
    return seconds


def parse_at(value: str) -> tuple[int, int]:
    """Parse a local ``HH:MM`` time."""
    match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", str(value).strip())
    if not match:
        raise DeviceError(f"time {value!r} must be HH:MM in 24 hour local time")
    return int(match.group(1)), int(match.group(2))


def next_daily(at_time: str, now: datetime) -> datetime:
    """Next local occurrence of HH:MM, returned as UTC."""
    hour, minute = parse_at(at_time)
    local = now.astimezone()
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def compute_next_run(
    kind: str, now: datetime, at_time: str | None, interval_s: int | None
) -> datetime:
    if kind == "daily":
        if not at_time:
            raise DeviceError("daily schedule needs at_time")
        return next_daily(at_time, now)
    if kind == "interval":
        if not interval_s:
            raise DeviceError("interval schedule needs interval_s")
        return now + timedelta(seconds=int(interval_s))
    raise DeviceError(f"unknown schedule kind {kind!r}")


def load_scheduled_workflow(name: str) -> Workflow:
    path = find_template(name)
    return load_workflow(path)


class Scheduler:
    """Adds schedules and executes the ones that are due."""

    def __init__(
        self,
        registry: Registry,
        runner: Runner,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.registry = registry
        self.runner = runner
        self.now = now

    def add(
        self,
        workflow: str,
        device: str,
        name: str | None = None,
        every: str | None = None,
        daily: str | None = None,
    ) -> dict[str, Any]:
        if bool(every) == bool(daily):
            raise DeviceError("choose exactly one of --every or --daily")
        self.registry.require_device(device)
        workflow_path = find_template(workflow)
        parsed = load_workflow(workflow_path)
        schedule_name = (name or parsed.name).lower()
        moment = self.now()
        if every:
            interval_s = parse_duration(every)
            next_run = compute_next_run("interval", moment, None, interval_s)
            return self.registry.add_schedule(
                schedule_name,
                parsed.name,
                device,
                "interval",
                iso(next_run),
                interval_s=interval_s,
            )
        at_time = str(daily)
        parse_at(at_time)
        next_run = compute_next_run("daily", moment, at_time, None)
        return self.registry.add_schedule(
            schedule_name, parsed.name, device, "daily", iso(next_run), at_time=at_time
        )

    def run_due(
        self, at: datetime | None = None
    ) -> list[tuple[dict[str, Any], RunResult | None, str]]:
        """Run every schedule whose next_run is in the past.

        Returns ``(schedule, result, error)`` triples. A broken template or a
        missing device is reported, and the schedule still advances so one bad
        entry cannot spin forever.
        """
        moment = at or self.now()
        outcomes: list[tuple[dict[str, Any], RunResult | None, str]] = []
        for schedule in self.registry.due_schedules(moment):
            result: RunResult | None = None
            error = ""
            try:
                workflow = load_scheduled_workflow(schedule["workflow"])
                result = self.runner.run(
                    workflow,
                    device_name=schedule["device"],
                    trigger=f"schedule:{schedule['name']}",
                )
                if result.status not in ("success", "partial"):
                    error = result.error or result.status
            except (TemplateError, DeviceError) as exc:
                error = str(exc)
            next_run = compute_next_run(
                schedule["kind"], moment, schedule["at_time"], schedule["interval_s"]
            )
            self.registry.mark_schedule_ran(schedule["name"], moment, next_run)
            outcomes.append((schedule, result, error))
        return outcomes
