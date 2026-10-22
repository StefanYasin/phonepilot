"""Workflow execution: order, confirmation, failures, transport routing."""

from __future__ import annotations

import pytest
from helpers import make_workflow

from phonepilot.engine import Runner
from phonepilot.errors import DeviceError
from phonepilot.templates import Step, Workflow
from phonepilot.transport import MockTransport


def run_with(
    registry,
    workflow,
    transport=None,
    confirm=None,
    device="iphone-01",
    dry_run=False,
    sleeps=None,
):
    sleeps = sleeps if sleeps is not None else []
    runner = Runner(
        registry,
        transport=transport,
        dry_run=dry_run,
        confirm=confirm or (lambda _q: True),
        sleep=sleeps.append,
        echo=lambda _m: None,
    )
    return runner.run(workflow, device_name=device)


def test_happy_path_executes_in_order(registry, fleet):
    transport = MockTransport()
    workflow = make_workflow(
        [
            {"id": "type_hello", "type": "send_keys", "text": "hi", "enter": True},
            {"id": "pause", "type": "wait", "seconds": 1},
            {"id": "open", "type": "trigger_shortcut", "shortcut": "Morning"},
        ]
    )
    sleeps: list[float] = []
    result = run_with(registry, workflow, transport=transport, sleeps=sleeps)

    assert result.status == "success"
    assert [c[0] for c in transport.calls] == ["send_keys", "trigger_shortcut"]
    assert transport.calls[0][1]["text"] == "hi"
    assert transport.calls[0][1]["enter"] is True
    assert sleeps == [1.0]
    assert [s.status for s in result.steps] == ["ok", "ok", "ok"]
    assert registry.get_run(result.run_id)["status"] == "success"
    assert len(registry.get_run_steps(result.run_id)) == 3


def test_social_post_needs_confirmation_and_is_declined(registry, fleet, tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg-bytes")
    transport = MockTransport()
    workflow = make_workflow(
        [
            {"id": "type_hello", "type": "send_keys", "text": "hi"},
            {
                "id": "post",
                "type": "social_post",
                "platform": "instagram",
                "media": str(photo),
                "caption": "hello world",
            },
            {"id": "after", "type": "wait", "seconds": 1},
        ]
    )
    result = run_with(registry, workflow, transport=transport, confirm=lambda _q: False)

    assert result.status == "aborted"
    assert [s.status for s in result.steps] == ["ok", "declined", "skipped"]
    assert [c[0] for c in transport.calls] == ["send_keys"]
    assert registry.get_run(result.run_id)["status"] == "aborted"
    assert "declined" in result.error


def test_social_post_passes_media_and_caption(registry, fleet, tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg-bytes")
    transport = MockTransport()
    workflow = make_workflow(
        [
            {
                "id": "post",
                "type": "social_post",
                "platform": "instagram",
                "media": str(photo),
                "caption": "new drop",
            }
        ]
    )
    result = run_with(registry, workflow, transport=transport)

    assert result.status == "success"
    action, payload = transport.calls[0]
    assert action == "social_post"
    assert payload["media"] == str(photo)
    assert payload["caption"] == "new drop"
    assert payload["platform"] == "instagram"


def test_social_post_missing_media_fails(registry, fleet, tmp_path):
    workflow = make_workflow(
        [
            {
                "id": "post",
                "type": "social_post",
                "platform": "instagram",
                "media": str(tmp_path / "ghost.jpg"),
                "caption": "nope",
            }
        ]
    )
    result = run_with(registry, workflow, transport=MockTransport())
    assert result.status == "failed"
    assert "media file not found" in result.steps[0].detail


def test_engine_refuses_unconfirmed_social_step_even_if_parsed_object_is_forged(
    registry, fleet, tmp_path
):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"x")
    workflow = Workflow(
        name="forged",
        version=1,
        description="",
        device="iphone-01",
        defaults={},
        steps=[
            Step(
                id="post",
                type="social_post",
                params={
                    "platform": "instagram",
                    "media": str(photo),
                    "caption": "hi",
                    "confirm_required": False,
                },
            )
        ],
    )
    transport = MockTransport()
    result = run_with(registry, workflow, transport=transport, confirm=lambda _q: True)
    assert result.status == "failed"
    assert "confirm_required" in result.steps[0].detail
    assert transport.calls == []


def test_failure_stops_the_run(registry, fleet):
    transport = MockTransport(fail_on={"send_keys"})
    workflow = make_workflow(
        [
            {"id": "type", "type": "send_keys", "text": "hi"},
            {"id": "after", "type": "wait", "seconds": 1},
        ]
    )
    result = run_with(registry, workflow, transport=transport)
    assert result.status == "failed"
    assert [s.status for s in result.steps] == ["failed", "skipped"]


def test_continue_on_error_produces_partial(registry, fleet):
    transport = MockTransport(fail_on={"send_keys"})
    workflow = make_workflow(
        [
            {
                "id": "type",
                "type": "send_keys",
                "text": "hi",
                "continue_on_error": True,
            },
            {"id": "open", "type": "trigger_shortcut", "shortcut": "Next"},
        ]
    )
    result = run_with(registry, workflow, transport=transport)
    assert result.status == "partial"
    assert [s.status for s in result.steps] == ["failed", "ok"]
    assert [c[0] for c in transport.calls] == ["send_keys", "trigger_shortcut"]


def test_api_call_and_response_interpolation(registry, fleet, board):
    transport = MockTransport()
    workflow = make_workflow(
        [
            {"id": "fetch", "type": "api_call", "url": board.base + "/api/v1/data"},
            {"id": "use", "type": "send_keys", "text": "{{ steps.fetch.json.title }}"},
        ]
    )
    result = run_with(registry, workflow, transport=transport)

    assert result.status == "success", result.error
    assert transport.calls[-1][1]["text"] == "Hello from the board"
    assert result.steps[0].output["status"] == 200
    assert result.steps[0].output["json"]["count"] == 3


def test_api_call_status_mismatch_fails(registry, fleet, board):
    workflow = make_workflow(
        [
            {
                "id": "fetch",
                "type": "api_call",
                "url": board.base + "/api/v1/data",
                "expect_status": 204,
            }
        ]
    )
    result = run_with(registry, workflow, transport=MockTransport())
    assert result.status == "failed"
    assert "expected [204]" in result.steps[0].detail


def test_missing_variable_fails_clearly(registry, fleet):
    workflow = make_workflow(
        [{"id": "type", "type": "send_keys", "text": "{{ vars.nope }}"}]
    )
    result = run_with(registry, workflow, transport=MockTransport())
    assert result.status == "failed"
    assert "was not provided" in result.steps[0].detail


def test_variables_and_env_interpolation(registry, fleet, monkeypatch):
    monkeypatch.setenv("PHONEPILOT_TEST_GREETING", "hi there")
    transport = MockTransport()
    workflow = make_workflow(
        [
            {
                "id": "type",
                "type": "send_keys",
                "text": "{{ env.PHONEPILOT_TEST_GREETING }} {{ vars.name }}",
            }
        ]
    )
    runner = Runner(
        registry,
        transport=transport,
        confirm=lambda _q: True,
        sleep=lambda _s: None,
        variables={"name": "Stefan"},
    )
    result = runner.run(workflow, device_name="iphone-01")
    assert result.status == "success"
    assert transport.calls[0][1]["text"] == "hi there Stefan"


def test_trigger_shortcut_honours_wait_after(registry, fleet):
    transport = MockTransport()
    workflow = make_workflow(
        [
            {
                "id": "open",
                "type": "trigger_shortcut",
                "shortcut": "Slow",
                "wait_after": 4,
            }
        ]
    )
    sleeps: list[float] = []
    result = run_with(registry, workflow, transport=transport, sleeps=sleeps)
    assert result.status == "success"
    assert sleeps == [4.0]
    assert transport.calls[0][1]["wait_after"] == 4.0


def test_dry_run_sends_nothing_and_skips_network(registry, board):
    registry.add_device("board-01", kind="esp32", host=board.base)
    workflow = make_workflow(
        [
            {"id": "type", "type": "send_keys", "text": "hi"},
            {"id": "fetch", "type": "api_call", "url": board.base + "/api/v1/data"},
        ],
        device="board-01",
    )
    before = len(board.requests)
    runner = Runner(
        registry, dry_run=True, confirm=lambda _q: True, sleep=lambda _s: None
    )
    result = runner.run(workflow, device_name="board-01")

    assert result.status == "success"
    assert len(board.requests) == before
    assert "dry run" in result.steps[1].detail


def test_iphone_commands_go_through_the_bridge_board(registry, board):
    registry.add_device("board-01", kind="esp32", host=board.base, token="tok")
    registry.add_device("iphone-01", kind="iphone", bridge="board-01")
    workflow = make_workflow(
        [{"id": "type", "type": "send_keys", "text": "hello"}], device="iphone-01"
    )

    runner = Runner(registry, confirm=lambda _q: True, sleep=lambda _s: None)
    result = runner.run(workflow)

    assert result.status == "success", result.error
    assert board.paths() == ["/api/v1/keys"]
    assert board.requests[0]["headers"]["x-phonepilot-token"] == "tok"
    assert board.requests[0]["body"]["text"] == "hello"


def test_unknown_device_is_a_clear_error(registry):
    workflow = make_workflow(
        [{"id": "type", "type": "send_keys", "text": "hi"}], device="ghost"
    )
    runner = Runner(registry)
    with pytest.raises(DeviceError) as caught:
        runner.run(workflow)
    assert "fleet add ghost" in str(caught.value)


def test_run_requires_a_device(registry):
    workflow = make_workflow([{"id": "type", "type": "send_keys", "text": "hi"}])
    workflow.device = None
    with pytest.raises(DeviceError):
        Runner(registry).run(workflow)


def test_failed_run_records_error(registry, fleet):
    workflow = make_workflow([{"id": "type", "type": "send_keys", "text": "hi"}])
    transport = MockTransport(fail_on={"send_keys"})
    result = run_with(registry, workflow, transport=transport)
    stored = registry.get_run(result.run_id)
    assert stored["status"] == "failed"
    assert "mock failure" in stored["error"]
