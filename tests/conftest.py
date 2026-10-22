"""Fixtures: a SQLite registry, a fake fleet, and a real local HTTP board."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from phonepilot.db import Registry


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path / "phonepilot.db")
    yield reg
    reg.close()


@pytest.fixture
def fleet(registry):
    """One ESP32 board plus one iPhone bridged through it."""
    registry.add_device("board-01", kind="esp32", host="192.0.2.10", token="secret")
    registry.add_device("iphone-01", kind="iphone", bridge="board-01")
    return registry


class FakeBoard:
    """A tiny stand in for the firmware HTTP API."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = raw.decode("utf-8", errors="replace")
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "body": body,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                status = 200
                if self.path == "/api/v1/status":
                    payload: dict[str, Any] = {
                        "ok": True,
                        "device": "board-01",
                        "firmware": "test-0.1.0",
                        "ble_connected": True,
                        "queue": 2,
                        "ip": "127.0.0.1",
                    }
                elif self.path == "/api/v1/data":
                    payload = {"title": "Hello from the board", "count": 3}
                elif self.path == "/api/v1/fail" or self.path == "/api/v1/refuse":
                    payload = {"ok": False, "error": "button not armed"}
                elif self.path.endswith("/chat/completions"):
                    draft = {
                        "name": "drafted_post",
                        "version": 1,
                        "description": "A drafted workflow",
                        "steps": [
                            {"type": "send_keys", "text": "hello", "enter": True},
                            {
                                "type": "social_post",
                                "platform": "instagram",
                                "media": "photo.jpg",
                                "caption": "drafted caption",
                            },
                        ],
                    }
                    payload = {"choices": [{"message": {"content": json.dumps(draft)}}]}
                elif self.path == "/missing":
                    status, payload = 404, {"error": "no such endpoint"}
                elif self.path.startswith("/api/v1/keys"):
                    text = (
                        (body or {}).get("text", "") if isinstance(body, dict) else ""
                    )
                    payload = {"ok": True, "sent": len(text)}
                elif self.path.startswith("/api/v1/"):
                    payload = {"ok": True, "queued": True, "job_id": 7}
                else:
                    status, payload = 404, {"error": "unknown"}
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = _handle
            do_POST = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeBoard:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)

    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]


@pytest.fixture
def board() -> Iterator[FakeBoard]:
    server = FakeBoard().start()
    yield server
    server.stop()
