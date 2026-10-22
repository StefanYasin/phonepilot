"""Autorun schedules: parsing, next run calculation, due execution."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from phonepilot.db import iso
from phonepilot.engine import Runner
from phonepilot.errors import DeviceError, TemplateError
from phonepilot.scheduler import (
    Scheduler,
    compute_next_run,
    next_daily,
    parse_at,
    parse_duration,
)
from phonepilot.transport import MockTransport

NOW = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def test_parse_duration_units():
    assert parse_duration("90s") == 90
    assert parse_duration("15m") == 900
    assert parse_duration("6h") == 21600
    assert parse_duration("1d") == 86400
    assert parse_duration(" 2H ") == 7200


def test_parse_duration_rejects_junk_and_tiny_intervals():
    with pytest.raises(DeviceError):
        parse_duration("soon")
    with pytest.raises(DeviceError):
        parse_duration("30s")


def test_parse_at():
    assert parse_at("07:30") == (7, 30)
    assert parse_at("23:59") == (23, 59)
    for bad in ("24:00", "7:5", "noon"):
        with pytest.raises(DeviceError):
            parse_at(bad)


def test_next_daily_is_in_the_future_and_keeps_local_time():
    result = next_daily("07:30", NOW)
    assert result > NOW
    assert result.astimezone().strftime("%H:%M") == "07:30"


def test_compute_next_run_interval_and_daily():
    assert compute_next_run("interval", NOW, None, 3600) == NOW + timedelta(hours=1)
    daily = compute_next_run("daily", NOW, "07:30", None)
    assert daily.astimezone().strftime("%H:%M") == "07:30"
    with pytest.raises(DeviceError):
        compute_next_run("monthly", NOW, None, None)


def test_scheduler_add_interval(fleet):
    now = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
    scheduler = Scheduler(
        fleet, Runner(fleet, transport=MockTransport()), now=lambda: now
    )
    schedule = scheduler.add("photo_post_queue", "iphone-01", every="6h")

    assert schedule["kind"] == "interval"
    assert schedule["interval_s"] == 21600
    assert schedule["next_run"] == iso(now + timedelta(hours=6))
    assert fleet.list_schedules()[0]["name"] == schedule["name"]


def test_scheduler_add_daily(fleet):
    scheduler = Scheduler(
        fleet, Runner(fleet, transport=MockTransport()), now=lambda: NOW
    )
    schedule = scheduler.add(
        "daily_briefing", "iphone-01", daily="07:30", name="morning"
    )
    assert schedule["kind"] == "daily"
    assert schedule["at_time"] == "07:30"
    assert (
        datetime.fromisoformat(schedule["next_run"]).astimezone().strftime("%H:%M")
        == "07:30"
    )


def test_scheduler_add_requires_exactly_one_kind(fleet):
    scheduler = Scheduler(
        fleet, Runner(fleet, transport=MockTransport()), now=lambda: NOW
    )
    with pytest.raises(DeviceError):
        scheduler.add("daily_briefing", "iphone-01")
    with pytest.raises(DeviceError):
        scheduler.add("daily_briefing", "iphone-01", every="1h", daily="07:30")


def test_scheduler_add_rejects_unknown_workflow_or_device(fleet):
    scheduler = Scheduler(
        fleet, Runner(fleet, transport=MockTransport()), now=lambda: NOW
    )
    with pytest.raises(TemplateError):
        scheduler.add("no_such_workflow", "iphone-01", every="1h")
    with pytest.raises(DeviceError):
        scheduler.add("daily_briefing", "ghost-phone", every="1h")


def test_run_due_executes_and_advances(fleet):
    transport = MockTransport()
    runner = Runner(
        fleet, transport=transport, confirm=lambda _q: True, echo=lambda _m: None
    )
    scheduler = Scheduler(fleet, runner, now=lambda: NOW)
    schedule = scheduler.add("photo_post_queue", "iphone-01", every="6h")

    outcomes = scheduler.run_due(
        at=datetime.fromisoformat(schedule["next_run"]) + timedelta(seconds=1)
    )

    assert len(outcomes) == 1
    _, result, error = outcomes[0]
    assert result is not None
    assert result.status == "success", error
    assert [c[0] for c in transport.calls] == ["trigger_shortcut", "social_post"]
    stored = fleet.list_schedules()[0]
    assert stored["last_run"] is not None
    skipped = scheduler.run_due(at=datetime.fromisoformat(stored["last_run"]))
    assert skipped == []


def test_run_due_surfaces_a_broken_workflow_and_advances(fleet):
    now = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
    fleet.add_schedule(
        "broken", "missing_workflow", "iphone-01", "interval", iso(now), interval_s=3600
    )
    scheduler = Scheduler(
        fleet, Runner(fleet, transport=MockTransport()), now=lambda: now
    )

    outcomes = scheduler.run_due(at=now)

    assert len(outcomes) == 1
    _, result, error = outcomes[0]
    assert result is None
    assert "not found" in error
    assert fleet.list_schedules()[0]["next_run"] == iso(now + timedelta(hours=1))


def test_run_due_ignores_disabled(fleet):
    now = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
    fleet.add_schedule(
        "off", "daily_briefing", "iphone-01", "interval", iso(now), interval_s=3600
    )
    fleet.set_schedule_enabled("off", False)
    scheduler = Scheduler(
        fleet, Runner(fleet, transport=MockTransport()), now=lambda: now
    )
    assert scheduler.run_due(at=now) == []
