"""Registry behaviour: devices, bridge routing, runs, schedules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from phonepilot.db import iso, parse_iso
from phonepilot.errors import DeviceError


def test_add_and_get_device(registry):
    device = registry.add_device(
        "board-01", kind="esp32", host="192.168.1.50", token="abc"
    )
    assert device["kind"] == "esp32"
    assert registry.get_device("BOARD-01")["host"] == "192.168.1.50"
    assert device["status"] == "unknown"


def test_duplicate_device_is_rejected(registry):
    registry.add_device("board-01", kind="esp32", host="192.168.1.50")
    with pytest.raises(DeviceError) as caught:
        registry.add_device("board-01", kind="esp32", host="192.168.1.51")
    assert "already exists" in str(caught.value)


def test_invalid_device_names_are_rejected(registry):
    for name in ("A", "has space", "-leading", "x"):
        with pytest.raises(DeviceError):
            registry.add_device(name, kind="esp32", host="192.168.1.50")


def test_esp32_needs_a_host(registry):
    with pytest.raises(DeviceError) as caught:
        registry.add_device("board-01", kind="esp32")
    assert "--host" in str(caught.value)


def test_iphone_needs_an_existing_bridge(registry):
    with pytest.raises(DeviceError) as caught:
        registry.add_device("iphone-01", kind="iphone", bridge="ghost")
    assert "not registered" in str(caught.value)

    registry.add_device("board-01", kind="esp32", host="192.168.1.50")
    with pytest.raises(DeviceError):
        registry.add_device("iphone-02", kind="iphone")


def test_iphone_commands_route_through_bridge(fleet):
    iphone = fleet.get_device("iphone-01")
    control = fleet.control_device(iphone)
    assert control["name"] == "board-01"
    assert control["kind"] == "esp32"


def test_list_and_remove(registry):
    registry.add_device("board-01", kind="esp32", host="192.168.1.50")
    registry.add_device("mock-01", kind="mock")
    assert [d["name"] for d in registry.list_devices()] == ["board-01", "mock-01"]
    assert registry.remove_device("mock-01") is True
    assert registry.remove_device("mock-01") is False


def test_update_device_status(registry):
    registry.add_device("board-01", kind="esp32", host="192.168.1.50")
    registry.update_device_status("board-01", "online")
    device = registry.get_device("board-01")
    assert device["status"] == "online"
    assert device["last_seen"]


def test_run_lifecycle(registry):
    registry.start_run("run1", "flow", "board-01", trigger="cli")
    registry.record_step("run1", 0, "first", "wait", "ok", detail="waited")
    registry.finish_run("run1", "success")
    run = registry.get_run("run1")
    assert run["status"] == "success"
    assert run["finished_at"]
    steps = registry.get_run_steps("run1")
    assert len(steps) == 1
    assert steps[0]["step_id"] == "first"


def test_list_runs_is_newest_first(registry):
    registry.start_run("a", "flow", "board-01")
    registry.finish_run("a", "success")
    registry.start_run("b", "flow", "board-01")
    registry.finish_run("b", "failed", "boom")
    ids = [r["id"] for r in registry.list_runs()]
    assert ids == ["b", "a"]


def test_schedule_due_and_mark(registry):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    registry.add_schedule(
        "hourly", "flow", "board-01", "interval", iso(now), interval_s=3600
    )
    assert registry.due_schedules(now - timedelta(minutes=1)) == []
    assert [s["name"] for s in registry.due_schedules(now)] == ["hourly"]
    registry.mark_schedule_ran("hourly", now, now + timedelta(hours=1))
    assert registry.due_schedules(now) == []
    assert [s["name"] for s in registry.due_schedules(now + timedelta(hours=1))] == [
        "hourly"
    ]


def test_schedule_disable(registry):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    registry.add_schedule(
        "hourly", "flow", "board-01", "interval", iso(now), interval_s=3600
    )
    assert registry.set_schedule_enabled("hourly", False) is True
    assert registry.due_schedules(now) == []


def test_schedule_validation(registry):
    now = iso(datetime(2026, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(DeviceError):
        registry.add_schedule("x", "flow", "board-01", "interval", now)
    with pytest.raises(DeviceError):
        registry.add_schedule("x", "flow", "board-01", "daily", now)
    with pytest.raises(DeviceError):
        registry.add_schedule("x", "flow", "board-01", "sometimes", now)


def test_iso_round_trip():
    moment = datetime(2026, 5, 4, 3, 2, 1, tzinfo=timezone.utc)
    assert parse_iso(iso(moment)) == moment
    naive = parse_iso("2026-05-04T03:02:01")
    assert naive.tzinfo is not None
