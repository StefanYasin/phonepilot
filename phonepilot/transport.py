"""Transport layer between the fleet manager and the ESP32 board.

Only the board is addressed directly. An iPhone is reached through its bridge
board: the phone Shortcut polls the board and executes the queued job.

Three transports exist:

* ``HttpTransport``    real board over Wi-Fi, token in ``X-PhonePilot-Token``
* ``MockTransport``    in memory stand in, used by demos and some tests
* ``DryRunTransport``  prints what would happen and sends nothing
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from .errors import TransportError

DEFAULT_TIMEOUT = 8.0
TOKEN_ENV = "PHONEPILOT_TOKEN"


def base_url(device: dict[str, Any]) -> str:
    """Build the board base URL from a registry row."""
    host = (device.get("host") or "").strip()
    if not host:
        raise TransportError(f"device {device.get('name')!r} has no host")
    if host.startswith(("http://", "https://")):
        return host.rstrip("/")
    port = int(device.get("port") or 80)
    return f"http://{host}:{port}"


def _decode(raw: bytes, content_type: str) -> Any:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if "json" in (content_type or "").lower() or text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
    return {"raw": text}


def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    body: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_status: set[int] | None = None,
) -> dict[str, Any]:
    """Perform one HTTP request and decode the response.

    Returns ``{"status": int, "json": Any|None, "text": str, "headers": dict}``.
    Raises TransportError on connection failure, timeout, or HTTP >= 400 unless
    the status is listed in ``allow_status``.
    """
    data: bytes | None = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    elif body is not None:
        data = body.encode("utf-8")

    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method.upper()
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if allow_status and exc.code in allow_status:
            decoded = _decode(raw, exc.headers.get("Content-Type", ""))
            return {
                "status": exc.code,
                "json": decoded,
                "text": raw.decode("utf-8", errors="replace"),
                "headers": dict(exc.headers.items()),
            }
        detail = raw.decode("utf-8", errors="replace")[:300]
        raise TransportError(
            f"{method.upper()} {url} returned HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise TransportError(f"cannot reach {url}: {reason}") from exc
    except TimeoutError as exc:
        raise TransportError(
            f"{method.upper()} {url} timed out after {timeout}s"
        ) from exc

    decoded = _decode(raw, content_type)
    text = raw.decode("utf-8", errors="replace")
    payload = decoded if isinstance(decoded, dict) else {"data": decoded}
    return {
        "status": status,
        "json": payload if isinstance(decoded, dict) else decoded,
        "text": text,
        "headers": response_headers,
    }


class HttpTransport:
    """Talks to a flashed PhonePilot board."""

    kind = "http"

    def __init__(
        self, device: dict[str, Any], timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.device = device
        self.base = base_url(device)
        self.timeout = timeout
        self.token = device.get("token") or os.environ.get(TOKEN_ENV, "")

    def _call(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"X-PhonePilot-Token": self.token} if self.token else {}
        try:
            response = http_request(
                method,
                self.base + path,
                headers=headers,
                json_body=payload,
                timeout=self.timeout,
            )
        except TransportError as exc:
            raise TransportError(f"{self.device.get('name', 'board')}: {exc}") from exc
        body = response.get("json")
        if isinstance(body, dict) and body.get("ok") is False:
            raise TransportError(f"board refused command: {body.get('error') or body}")
        return response

    def status(self) -> dict[str, Any]:
        body = self._call("GET", "/api/v1/status").get("json")
        return body if isinstance(body, dict) else {"raw": body}

    def send_keys(
        self, text: str, enter: bool = False, delay_ms: int = 0
    ) -> dict[str, Any]:
        return self._call(
            "POST", "/api/v1/keys", {"text": text, "enter": enter, "delay_ms": delay_ms}
        )["json"]

    def trigger_shortcut(self, name: str, wait_after: float = 0) -> dict[str, Any]:
        return self._call(
            "POST", "/api/v1/shortcut", {"name": name, "wait_after": wait_after}
        )["json"]

    def social_post(
        self,
        platform: str,
        media: str,
        caption: str,
        target: str | None = None,
    ) -> dict[str, Any]:
        payload = {"platform": platform, "media": media, "caption": caption}
        if target:
            payload["target"] = target
        return self._call("POST", "/api/v1/post", payload)["json"]

    def arm_approval(self, seconds: int = 90) -> dict[str, Any]:
        return self._call("POST", "/api/v1/approve", {"seconds": seconds})["json"]

    def panic(self) -> dict[str, Any]:
        return self._call("POST", "/api/v1/panic", {})["json"]


class MockTransport:
    """In memory board used for demos and tests.

    ``fail_on`` accepts step kinds ("send_keys", "social_post") to force a
    failure and exercise the error path.
    """

    kind = "mock"

    def __init__(
        self, device: dict[str, Any] | None = None, fail_on: set[str] | None = None
    ) -> None:
        self.device = device or {"name": "mock-board", "kind": "mock"}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_on = set(fail_on or ())

    def _record(self, action: str, payload: dict[str, Any]) -> None:
        self.calls.append((action, payload))
        if action in self.fail_on:
            raise TransportError(f"mock failure on {action}")

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "device": self.device.get("name", "mock-board"),
            "firmware": "mock",
            "ble_connected": True,
            "queue": 0,
            "ip": "127.0.0.1",
        }

    def send_keys(
        self, text: str, enter: bool = False, delay_ms: int = 0
    ) -> dict[str, Any]:
        self._record("send_keys", {"text": text, "enter": enter, "delay_ms": delay_ms})
        return {"ok": True, "sent": len(text), "mock": True}

    def trigger_shortcut(self, name: str, wait_after: float = 0) -> dict[str, Any]:
        self._record("trigger_shortcut", {"name": name, "wait_after": wait_after})
        return {"ok": True, "queued": True, "job_id": len(self.calls), "mock": True}

    def social_post(
        self,
        platform: str,
        media: str,
        caption: str,
        target: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "social_post",
            {
                "platform": platform,
                "media": media,
                "caption": caption,
                "target": target,
            },
        )
        return {"ok": True, "queued": True, "requires_phone": True, "mock": True}

    def arm_approval(self, seconds: int = 90) -> dict[str, Any]:
        self._record("arm_approval", {"seconds": seconds})
        return {"ok": True, "armed_for": seconds, "mock": True}

    def panic(self) -> dict[str, Any]:
        self._record("panic", {})
        return {"ok": True, "mock": True}


class DryRunTransport:
    """Records commands without sending them."""

    kind = "dry-run"

    def __init__(self, device: dict[str, Any] | None = None) -> None:
        self.device = device or {"name": "dry-run", "kind": "dry-run"}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, payload))
        return {"ok": True, "dry_run": True, "action": action}

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "dry_run": True,
            "device": self.device.get("name", "dry-run"),
        }

    def send_keys(
        self, text: str, enter: bool = False, delay_ms: int = 0
    ) -> dict[str, Any]:
        return self._record(
            "send_keys", {"text": text, "enter": enter, "delay_ms": delay_ms}
        )

    def trigger_shortcut(self, name: str, wait_after: float = 0) -> dict[str, Any]:
        return self._record(
            "trigger_shortcut", {"name": name, "wait_after": wait_after}
        )

    def social_post(
        self,
        platform: str,
        media: str,
        caption: str,
        target: str | None = None,
    ) -> dict[str, Any]:
        return self._record(
            "social_post",
            {
                "platform": platform,
                "media": media,
                "caption": caption,
                "target": target,
            },
        )

    def arm_approval(self, seconds: int = 90) -> dict[str, Any]:
        return self._record("arm_approval", {"seconds": seconds})

    def panic(self) -> dict[str, Any]:
        return self._record("panic", {})


def make_transport(
    device: dict[str, Any] | None,
    dry_run: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """Pick a transport for a registry device row."""
    if dry_run:
        return DryRunTransport(device)
    if device is None:
        raise TransportError("no device to talk to")
    if device.get("kind") == "mock":
        return MockTransport(device)
    return HttpTransport(device, timeout=timeout)


def looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
