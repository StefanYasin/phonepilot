"""Workflow execution engine.

The runner turns a validated template into board commands, records every step
in SQLite, and stops at the first failure unless the step opts into
``continue_on_error``. Social posts never run without an explicit yes.
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Registry
from .errors import DeviceError, PhonePilotError, StepError, TransportError
from .templates import Step, Workflow, resolve_media
from .transport import DryRunTransport, http_request, make_transport

VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StepResult:
    """Outcome of one step."""

    idx: int
    step_id: str
    type: str
    status: str
    detail: str = ""
    output: Any = None
    started_at: str = ""
    finished_at: str = ""


@dataclass
class RunResult:
    """Outcome of one workflow run."""

    run_id: str
    workflow: str
    device: str
    status: str = "running"
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    steps: list[StepResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "device": self.device,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "steps": [s.__dict__ for s in self.steps],
        }


class _Declined(StepError):
    """Internal marker: the operator said no."""


def _resolve_ref(ref: str, context: dict[str, Any]) -> Any:
    parts = ref.split(".")
    head = parts[0]
    if head == "env":
        import os

        key = ".".join(parts[1:])
        if key not in os.environ:
            raise StepError(f"environment variable {key!r} is not set")
        return os.environ[key]
    if head == "vars":
        key = ".".join(parts[1:])
        if key not in context.get("vars", {}):
            raise StepError(
                f"variable {key!r} was not provided; pass --var {key}=value"
            )
        return context["vars"][key]
    if head == "run":
        key = ".".join(parts[1:])
        if key in context.get("run", {}):
            return context["run"][key]
        raise StepError(f"unknown run reference {ref!r}")
    if head == "steps":
        if len(parts) < 3:
            raise StepError(f"step reference {ref!r} needs steps.<id>.<field>")
        step_id, field_name = parts[1], parts[2]
        record = context.get("steps", {}).get(step_id)
        if record is None:
            raise StepError(f"step reference {ref!r} points at an unrun step")
        if field_name not in record:
            raise StepError(f"step {step_id!r} has no field {field_name!r}")
        value: Any = record[field_name]
        for key in parts[3:]:
            if isinstance(value, dict) and key in value:
                value = value[key]
            elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
                value = value[int(key)]
            else:
                raise StepError(f"cannot resolve {ref!r} at {key!r}")
        return value
    raise StepError(f"unknown template reference {ref!r}")


def interpolate(value: Any, context: dict[str, Any]) -> Any:
    """Replace ``{{ ref }}`` placeholders in strings, recursively."""
    if isinstance(value, str):
        full = VAR_RE.fullmatch(value.strip())
        if full:
            return _resolve_ref(full.group(1), context)

        def replace(match: re.Match[str]) -> str:
            return str(_resolve_ref(match.group(1), context))

        return VAR_RE.sub(replace, value)
    if isinstance(value, dict):
        return {k: interpolate(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, context) for v in value]
    return value


def _default_confirm(question: str) -> bool:
    """Prompt on a terminal; refuse when nobody can answer."""
    print(question)
    if not sys.stdin.isatty():
        print("No terminal available to confirm, so the step was declined.")
        return False
    answer = input("Approve? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _summarize(output: Any) -> str:
    try:
        text = json.dumps(output, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(output)
    return text[:900]


class Runner:
    """Executes workflows against one device."""

    def __init__(
        self,
        registry: Registry,
        transport: Any | None = None,
        dry_run: bool = False,
        confirm: Callable[[str], bool] | None = None,
        echo: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        variables: dict[str, str] | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.registry = registry
        self.transport = transport
        self.dry_run = dry_run
        self.confirm = confirm or _default_confirm
        self.echo = echo or (lambda _message: None)
        self.sleep = sleep or time.sleep
        self.variables = dict(variables or {})
        self.run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex[:12])

    # -- public ---------------------------------------------------------
    def run(
        self, workflow: Workflow, device_name: str | None = None, trigger: str = "cli"
    ) -> RunResult:
        target_name = device_name or workflow.device
        if not target_name:
            raise DeviceError(
                "workflow has no device; set device: in the template or pass --device"
            )

        device = self.registry.get_device(target_name)
        if device is None and not self.dry_run:
            raise DeviceError(
                f"device {target_name!r} is not registered; run: phonepilot fleet add {target_name}"
            )
        if device is None:
            device = {"name": target_name, "kind": "dry-run", "bridge": None}
        transport = self._transport_for(device)

        run_id = self.run_id_factory()
        result = RunResult(
            run_id=run_id,
            workflow=workflow.name,
            device=device["name"],
            started_at=_now_iso(),
        )
        self.registry.start_run(run_id, workflow.name, device["name"], trigger)
        context: dict[str, Any] = {
            "run": {"id": run_id, "workflow": workflow.name, "device": device["name"]},
            "vars": self.variables,
            "steps": {},
            "workflow_source": str(workflow.source) if workflow.source else "",
        }

        self.echo(f"run {run_id}  {workflow.name}  ->  {device['name']}")
        stopped = False
        had_failure = False
        for idx, step in enumerate(workflow.steps):
            if stopped:
                record = StepResult(
                    idx, step.id, step.type, "skipped", "earlier step stopped the run"
                )
                result.steps.append(record)
                self._save_step(run_id, record)
                continue

            started = _now_iso()
            self.echo(
                f"  [{idx + 1}/{len(workflow.steps)}] {step.type} {step.id}: {step.describe()}"
            )
            status = "ok"
            output: Any = None
            detail = ""
            try:
                output, detail = self._execute(step, context, device, transport)
            except _Declined as exc:
                status, output, detail = "declined", None, str(exc)
                result.status = "aborted"
                result.error = f"step {step.id}: {exc}"
            except (StepError, TransportError, PhonePilotError) as exc:
                status, output, detail = "failed", None, str(exc)
                result.error = f"step {step.id}: {exc}"
                had_failure = True
            except Exception as exc:  # noqa: BLE001 - a step bug must not lose the run
                status, output, detail = "failed", None, f"{type(exc).__name__}: {exc}"
                result.error = f"step {step.id}: {type(exc).__name__}: {exc}"
                had_failure = True

            record = StepResult(
                idx=idx,
                step_id=step.id,
                type=step.type,
                status=status,
                detail=detail,
                output=output,
                started_at=started,
                finished_at=_now_iso(),
            )
            result.steps.append(record)
            self._save_step(run_id, record)
            if status != "ok":
                self.echo(f"      {status}: {detail}")
                if status == "declined" or not step.continue_on_error:
                    stopped = True

        if result.status == "aborted":
            pass
        elif stopped:
            result.status = "failed"
        elif had_failure:
            result.status = "partial"
        else:
            result.status = "success"
        result.finished_at = _now_iso()
        self.registry.finish_run(run_id, result.status, result.error or None)
        self.echo(f"run {run_id} finished: {result.status}")
        return result

    # -- internals ------------------------------------------------------
    def _transport_for(self, device: dict[str, Any]) -> Any:
        if self.transport is not None:
            return self.transport
        if self.dry_run:
            self.transport = DryRunTransport(device)
        else:
            self.transport = make_transport(self.registry.control_device(device))
        return self.transport

    def _save_step(self, run_id: str, record: StepResult) -> None:
        self.registry.record_step(
            run_id,
            record.idx,
            record.step_id,
            record.type,
            record.status,
            detail=record.detail,
            started_at=record.started_at,
            finished_at=record.finished_at,
        )

    def _execute(
        self,
        step: Step,
        context: dict[str, Any],
        device: dict[str, Any],
        transport: Any,
    ) -> tuple[Any, str]:
        params = interpolate(step.params, context)

        if step.type == "wait":
            seconds = float(params["seconds"])
            if self.dry_run:
                detail = f"would wait {seconds:g}s"
            else:
                self.sleep(seconds)
                detail = f"waited {seconds:g}s"
            self._remember(context, step.id, {"status": "ok", "seconds": seconds})
            return {"seconds": seconds}, detail

        if step.type == "send_keys":
            try:
                output = transport.send_keys(
                    str(params["text"]),
                    enter=bool(params.get("enter", False)),
                    delay_ms=int(params.get("delay_ms", 0)),
                )
            except TransportError as exc:
                raise StepError(str(exc)) from exc
            self._remember(context, step.id, {"status": "ok", "output": output})
            return output, _summarize(output)

        if step.type == "trigger_shortcut":
            wait_after = float(params.get("wait_after", 0) or 0)
            try:
                output = transport.trigger_shortcut(
                    str(params["shortcut"]), wait_after=wait_after
                )
            except TransportError as exc:
                raise StepError(str(exc)) from exc
            if wait_after and not self.dry_run:
                self.sleep(wait_after)
            self._remember(context, step.id, {"status": "ok", "output": output})
            return output, f"queued shortcut {params['shortcut']!r}"

        if step.type == "api_call":
            timeout = float(params.get("timeout_s", 20))
            method = str(params.get("method", "GET"))
            url = str(params["url"])
            expect = params.get("expect_status", [200, 201, 202, 204])
            if self.dry_run:
                record = {"status": "dry-run", "json": None, "text": ""}
                self._remember(context, step.id, record)
                return record, f"would {method} {url} (dry run, no request sent)"
            expected = expect if isinstance(expect, list) else [expect]
            try:
                response = http_request(
                    method,
                    url,
                    headers=params.get("headers"),
                    json_body=params.get("json"),
                    body=params.get("body"),
                    timeout=timeout,
                )
            except TransportError as exc:
                raise StepError(str(exc)) from exc
            if response["status"] not in expected:
                raise StepError(
                    f"{method} {url} returned HTTP {response['status']}, expected {expected}"
                )
            record = {
                "status": response["status"],
                "json": response.get("json"),
                "text": response.get("text", "")[:4000],
            }
            self._remember(context, step.id, record)
            return record, f"HTTP {response['status']} from {url}"

        if step.type == "social_post":
            media = resolve_media(str(params["media"]), template_dir(context))
            platform = str(params["platform"])
            caption = str(params["caption"])
            if not media.exists():
                if self.dry_run:
                    self.echo(f"      note: media not found yet: {media}")
                else:
                    raise StepError(f"media file not found: {media}")
            if params.get("confirm_required", True) is not True:
                raise StepError("social_post refused: confirm_required must stay true")
            question = (
                f"Post to {platform} from {device['name']}?\n"
                f"  media:   {media}\n"
                f"  caption: {caption}"
            )
            if not self.confirm(question):
                raise _Declined("declined by the operator; nothing was posted")
            try:
                output = transport.social_post(
                    platform=platform,
                    media=str(media),
                    caption=caption,
                    target=params.get("target"),
                )
            except TransportError as exc:
                raise StepError(str(exc)) from exc
            self._remember(context, step.id, {"status": "ok", "output": output})
            return output, f"handed to the phone queue for {platform}"

        raise StepError(f"unsupported step type {step.type!r}")

    def _remember(
        self, context: dict[str, Any], step_id: str, record: dict[str, Any]
    ) -> None:
        context.setdefault("steps", {})[step_id] = record


def template_dir(context: dict[str, Any]) -> Path | None:
    """Media paths resolve next to the template file when known."""
    source = context.get("workflow_source")
    if source:
        return Path(source).parent
    return None
