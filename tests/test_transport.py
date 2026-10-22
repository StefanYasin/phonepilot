"""Transport behaviour against a real local HTTP server and the fakes."""

from __future__ import annotations

import pytest

from phonepilot.errors import TransportError
from phonepilot.transport import (
    DryRunTransport,
    HttpTransport,
    MockTransport,
    base_url,
    http_request,
    looks_like_url,
)


def test_base_url_accepts_host_or_full_url():
    assert base_url({"host": "192.168.1.50", "port": 80}) == "http://192.168.1.50:80"
    assert base_url({"host": "http://board.local", "port": 80}) == "http://board.local"
    assert (
        base_url({"host": "https://board.local/", "port": 443}) == "https://board.local"
    )
    with pytest.raises(TransportError):
        base_url({"host": ""})


def test_status_and_commands_round_trip(board):
    transport = HttpTransport(
        {
            "name": "board-01",
            "kind": "esp32",
            "host": board.base,
            "port": 80,
            "token": "tok",
        }
    )
    status = transport.status()
    assert status["firmware"] == "test-0.1.0"
    assert status["ble_connected"] is True

    sent = transport.send_keys("hello", enter=True)
    assert sent["sent"] == 5
    request = board.requests[-1]
    assert request["path"] == "/api/v1/keys"
    assert request["headers"]["x-phonepilot-token"] == "tok"
    assert request["body"] == {"text": "hello", "enter": True, "delay_ms": 0}

    transport.trigger_shortcut("Morning", wait_after=2)
    assert board.requests[-1]["body"]["wait_after"] == 2

    transport.social_post("instagram", "/tmp/a.jpg", "caption", target="feed")
    assert board.requests[-1]["path"] == "/api/v1/post"
    assert board.requests[-1]["body"]["target"] == "feed"

    transport.arm_approval(45)
    assert board.requests[-1]["body"]["seconds"] == 45

    transport.panic()
    assert board.requests[-1]["path"] == "/api/v1/panic"


def test_board_refusal_becomes_transport_error(board):
    transport = HttpTransport({"name": "board-01", "host": board.base, "port": 80})
    with pytest.raises(TransportError) as caught:
        transport._call("POST", "/api/v1/refuse", {})
    assert "button not armed" in str(caught.value)


def test_http_request_reports_status_errors(board):
    with pytest.raises(TransportError) as caught:
        http_request("GET", board.base + "/missing")
    assert "404" in str(caught.value)


def test_connection_failure_is_wrapped():
    with pytest.raises(TransportError):
        http_request("GET", "http://127.0.0.1:9/nothing", timeout=2)


def test_mock_transport_records_calls():
    transport = MockTransport()
    transport.send_keys("hi", enter=False)
    transport.trigger_shortcut("Next")
    transport.social_post("x", "a.jpg", "caption")
    transport.arm_approval(10)
    assert [c[0] for c in transport.calls] == [
        "send_keys",
        "trigger_shortcut",
        "social_post",
        "arm_approval",
    ]


def test_mock_failure_mode():
    transport = MockTransport(fail_on={"social_post"})
    with pytest.raises(TransportError):
        transport.social_post("x", "a.jpg", "caption")


def test_dry_run_transport_never_raises():
    transport = DryRunTransport({"name": "board-01"})
    assert transport.send_keys("hi")["dry_run"] is True
    assert transport.trigger_shortcut("Next")["dry_run"] is True
    assert transport.social_post("x", "a.jpg", "caption")["dry_run"] is True
    assert len(transport.calls) == 3


def test_looks_like_url():
    assert looks_like_url("https://example.com/hook")
    assert not looks_like_url("example.com")
