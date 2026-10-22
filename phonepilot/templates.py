"""Load and validate YAML workflow templates.

A template is data, not code. Validation is strict: unknown keys are errors so
a typo cannot silently disable a confirmation. Every error is collected and
reported together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import policy
from .errors import TemplateError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

WORKFLOW_FIELDS = {
    "name",
    "version",
    "description",
    "device",
    "defaults",
    "steps",
    "tags",
}
COMMON_STEP_FIELDS = {"id", "type", "continue_on_error"}

STEP_FIELDS: dict[str, set[str]] = {
    "trigger_shortcut": {"shortcut", "wait_after"},
    "send_keys": {"text", "enter", "delay_ms"},
    "wait": {"seconds"},
    "api_call": {
        "method",
        "url",
        "headers",
        "json",
        "body",
        "expect_status",
        "timeout_s",
    },
    "social_post": {
        "platform",
        "media",
        "caption",
        "mode",
        "confirm_required",
        "target",
    },
}

REQUIRED_FIELDS: dict[str, set[str]] = {
    "trigger_shortcut": {"shortcut"},
    "send_keys": {"text"},
    "wait": {"seconds"},
    "api_call": {"url"},
    "social_post": {"platform", "media", "caption"},
}

STEP_TYPES = tuple(STEP_FIELDS)
PLATFORMS = (
    "instagram",
    "tiktok",
    "x",
    "linkedin",
    "facebook",
    "threads",
    "youtube",
    "pinterest",
    "custom",
)
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
DEFAULT_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class Step:
    """One action in a workflow."""

    id: str
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    continue_on_error: bool = False

    def describe(self) -> str:
        if self.type == "social_post":
            return f"{self.params.get('platform')} post: {str(self.params.get('caption', ''))[:60]}"
        if self.type == "api_call":
            return f"{self.params.get('method', 'GET')} {self.params.get('url')}"
        if self.type == "send_keys":
            return f"type {len(str(self.params.get('text', '')))} characters"
        if self.type == "wait":
            return f"wait {self.params.get('seconds')}s"
        return f"run shortcut {self.params.get('shortcut')!r}"


@dataclass
class Workflow:
    """A parsed, validated workflow template."""

    name: str
    version: int
    description: str
    device: str | None
    defaults: dict[str, Any]
    steps: list[Step]
    tags: list[str] = field(default_factory=list)
    source: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "steps": [
                {
                    "id": s.id,
                    "type": s.type,
                    **s.params,
                    **({"continue_on_error": True} if s.continue_on_error else {}),
                }
                for s in self.steps
            ],
        }
        if self.description:
            data["description"] = self.description
        if self.device:
            data["device"] = self.device
        if self.defaults:
            data["defaults"] = self.defaults
        if self.tags:
            data["tags"] = self.tags
        return data


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _default_step_id(step_type: str, index: int) -> str:
    return f"{step_type}_{index + 1}"


def _parse_step(
    raw: Any, index: int, base_dir: Path | None, check_media: bool
) -> tuple[Step, list[str]]:
    problems: list[str] = []
    label = f"steps[{index}]"
    if not isinstance(raw, dict):
        return Step(id=_default_step_id("step", index), type="invalid"), [
            f"{label}: must be a mapping"
        ]

    step_type = raw.get("type")
    if not isinstance(step_type, str) or step_type not in STEP_FIELDS:
        allowed = ", ".join(STEP_FIELDS)
        return Step(
            id=_default_step_id(str(step_type or "step"), index), type=str(step_type)
        ), [f"{label}: type must be one of {allowed}"]

    step_id = raw.get("id") or _default_step_id(step_type, index)
    if not isinstance(step_id, str) or not NAME_RE.match(step_id):
        problems.append(f"{label}: id must be a lowercase slug")
        step_id = _default_step_id(step_type, index)

    allowed = COMMON_STEP_FIELDS | STEP_FIELDS[step_type]
    for key in raw:
        if key not in allowed:
            problems.append(f"steps[{step_id}]: unknown field {key!r}")
    for required in REQUIRED_FIELDS[step_type]:
        if required not in raw:
            problems.append(f"steps[{step_id}]: missing required field {required!r}")

    params: dict[str, Any] = {
        k: v for k, v in raw.items() if k not in COMMON_STEP_FIELDS
    }
    continue_on_error = raw.get("continue_on_error", False)
    if not isinstance(continue_on_error, bool):
        problems.append(f"steps[{step_id}]: continue_on_error must be true or false")
        continue_on_error = False

    if "shortcut" in params and (
        not isinstance(params["shortcut"], str) or not params["shortcut"].strip()
    ):
        problems.append(f"steps[{step_id}]: shortcut must be a non-empty string")
    if "wait_after" in params and (
        not _is_number(params["wait_after"]) or not 0 <= params["wait_after"] <= 600
    ):
        problems.append(
            f"steps[{step_id}]: wait_after must be between 0 and 600 seconds"
        )

    if step_type == "send_keys":
        if not isinstance(params.get("text"), str) or params.get("text") == "":
            problems.append(f"steps[{step_id}]: text must be a non-empty string")
        if "enter" in params and not isinstance(params["enter"], bool):
            problems.append(f"steps[{step_id}]: enter must be true or false")
        if "delay_ms" in params and (
            not _is_int(params["delay_ms"]) or not 0 <= params["delay_ms"] <= 5000
        ):
            problems.append(
                f"steps[{step_id}]: delay_ms must be an integer between 0 and 5000"
            )

    if step_type == "wait":
        seconds = params.get("seconds")
        if not _is_number(seconds) or not 0 <= seconds <= 3600:
            problems.append(
                f"steps[{step_id}]: seconds must be a number between 0 and 3600"
            )

    if step_type == "api_call":
        method = params.get("method", "GET")
        if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
            problems.append(
                f"steps[{step_id}]: method must be one of {', '.join(HTTP_METHODS)}"
            )
        else:
            params["method"] = method.upper()
        url = params.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            problems.append(
                f"steps[{step_id}]: url must start with http:// or https://"
            )
        if "json" in params and "body" in params:
            problems.append(f"steps[{step_id}]: use either json or body, not both")
        if "headers" in params:
            headers = params["headers"]
            if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
            ):
                problems.append(
                    f"steps[{step_id}]: headers must map strings to strings"
                )
        expect = params.get("expect_status")
        if expect is not None:
            values = expect if isinstance(expect, list) else [expect]
            if not values or not all(_is_int(v) and 100 <= v <= 599 for v in values):
                problems.append(
                    f"steps[{step_id}]: expect_status must be an HTTP status or list of them"
                )
        if "timeout_s" in params and (
            not _is_number(params["timeout_s"]) or not 1 <= params["timeout_s"] <= 300
        ):
            problems.append(f"steps[{step_id}]: timeout_s must be between 1 and 300")

    if step_type == "social_post":
        platform = params.get("platform")
        if not isinstance(platform, str) or platform.lower() not in PLATFORMS:
            problems.append(
                f"steps[{step_id}]: platform must be one of {', '.join(PLATFORMS)}"
            )
        else:
            params["platform"] = platform.lower()
        media = params.get("media")
        if not isinstance(media, str) or not media.strip():
            problems.append(
                f"steps[{step_id}]: media must be a path to a photo or video"
            )
        elif check_media:
            resolved = resolve_media(media, base_dir)
            if not resolved.exists():
                problems.append(f"steps[{step_id}]: media file not found: {resolved}")
        caption = params.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            problems.append(f"steps[{step_id}]: caption must be a non-empty string")
        mode = params.get("mode", "assisted")
        if mode != "assisted":
            problems.append(f"steps[{step_id}]: mode must be assisted in v0.1.0")
        confirm = params.get("confirm_required", True)
        if confirm is not True:
            problems.append(
                f"steps[{step_id}]: confirm_required must be true for social_post"
            )

    return (
        Step(
            id=str(step_id),
            type=step_type,
            params=params,
            continue_on_error=continue_on_error,
        ),
        problems,
    )


def parse_workflow(
    data: Any,
    source: Path | None = None,
    check_media: bool = False,
) -> Workflow:
    """Validate a parsed YAML document and return a Workflow."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise TemplateError("template root must be a mapping")
    for key in data:
        if key not in WORKFLOW_FIELDS:
            problems.append(f"unknown top level field {key!r}")

    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.append("name must be a lowercase slug such as photo_post_queue")
        name = name if isinstance(name, str) else "unnamed"

    version = data.get("version", 1)
    if not _is_int(version) or version != 1:
        problems.append("version must be 1; this release supports schema version 1")
        version = 1

    description = data.get("description", "")
    if not isinstance(description, str):
        problems.append("description must be a string")
        description = ""

    device = data.get("device")
    if device is not None and (not isinstance(device, str) or not device.strip()):
        problems.append("device must be a device name")
        device = None

    tags = data.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        problems.append("tags must be a list of strings")
        tags = []

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        problems.append("defaults must be a mapping")
        defaults = {}
    for key in defaults:
        if key not in {"confirm_required", "timeout_s"}:
            problems.append(f"defaults: unknown field {key!r}")
    if "confirm_required" in defaults and defaults["confirm_required"] is not True:
        problems.append("defaults.confirm_required may only be true")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise TemplateError("template needs a non empty steps list", problems)
    if len(raw_steps) > 50:
        problems.append("a workflow may contain at most 50 steps")

    base_dir = source.parent if source else None
    steps: list[Step] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_steps):
        step, step_problems = _parse_step(raw, index, base_dir, check_media)
        problems.extend(step_problems)
        if step.id in seen:
            problems.append(f"steps[{index}]: duplicate id {step.id!r}")
        seen.add(step.id)
        if step.type == "social_post" and "confirm_required" not in step.params:
            step.params["confirm_required"] = True
        if "confirm_required" not in step.params:
            step.params["confirm_required"] = defaults.get("confirm_required", True)
        if "timeout_s" not in step.params and "timeout_s" in defaults:
            step.params["timeout_s"] = defaults["timeout_s"]
        steps.append(step)

    workflow = Workflow(
        name=name,
        version=version,
        description=description,
        device=device,
        defaults=defaults,
        steps=steps,
        tags=tags,
        source=source,
    )
    problems.extend(policy.check_policy(workflow))
    if problems:
        raise TemplateError(f"invalid workflow {name!r}", problems)
    return workflow


def load_workflow(path: str | Path, check_media: bool = False) -> Workflow:
    """Read a YAML file from disk and validate it."""
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise TemplateError(f"template not found: {file_path}")
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateError(f"cannot read {file_path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TemplateError(f"{file_path.name} is not valid YAML: {exc}") from exc
    return parse_workflow(data, source=file_path.resolve(), check_media=check_media)


def resolve_media(media: str, base_dir: Path | None) -> Path:
    """Resolve a media path against the template directory, then the cwd."""
    candidate = Path(media).expanduser()
    if candidate.is_absolute():
        return candidate
    if base_dir is not None:
        first = (base_dir / candidate).resolve()
        if first.exists():
            return first
    second = (Path.cwd() / candidate).resolve()
    if second.exists():
        return second
    return (
        (base_dir / candidate).resolve()
        if base_dir is not None
        else candidate.resolve()
    )


def dump_workflow(workflow: Workflow) -> str:
    """Serialize a workflow to YAML for ``workflows draft``."""
    return yaml.safe_dump(workflow.to_dict(), sort_keys=False, allow_unicode=True)


def find_template(reference: str, extra_dirs: tuple[Path, ...] = ()) -> Path:
    """Resolve a workflow name or path to a YAML file.

    Search order: an explicit path, extra directories, ``PHONEPILOT_HOME``
    templates, the templates shipped in the package, then the cwd.
    """
    from . import config

    candidate = Path(reference).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    directories = list(extra_dirs) + [
        config.templates_dir(),
        config.shipped_templates_dir(),
        Path.cwd(),
    ]
    for directory in directories:
        for suffix in (".yaml", ".yml"):
            path = Path(directory) / f"{reference}{suffix}"
            if path.is_file():
                return path.resolve()
    raise TemplateError(
        f"workflow {reference!r} not found; looked in "
        + ", ".join(str(d) for d in directories)
    )
