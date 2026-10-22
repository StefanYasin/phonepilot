"""Refuse to automate the things this project will not automate.

The checks are intentionally narrow and mechanical: they catch the obvious
requests (fake engagement, confirmation opt out, rate limit evasion) at
template load time, before any command reaches a phone.
"""

from __future__ import annotations

from typing import Any

FORBIDDEN_KEYS = {
    "auto_like",
    "auto_follow",
    "auto_comment",
    "auto_dm",
    "engagement_boost",
    "bypass_rate_limit",
    "rate_limit_bypass",
    "rotate_proxy",
    "spoof_device",
    "retry_on_429",
}


def _walk_keys(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key)
            found.append((prefix + name, name))
            found.extend(_walk_keys(child, prefix + name + "."))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_keys(item, prefix))
    return found


def check_policy(workflow: Any) -> list[str]:
    """Return policy violations for a parsed workflow."""
    problems: list[str] = []
    for step in workflow.steps:
        label = f"steps[{step.id}]"
        for path, key in _walk_keys(step.params):
            if key in FORBIDDEN_KEYS:
                problems.append(
                    f"{label}: {path} is not allowed; PhonePilot does not fake engagement "
                    "or work around platform rate limits"
                )
        if step.type == "social_post":
            if step.params.get("confirm_required") is not True:
                problems.append(
                    f"{label}: social_post must keep confirm_required: true; "
                    "posting always needs a human answer"
                )
            mode = step.params.get("mode", "assisted")
            if mode != "assisted":
                problems.append(
                    f"{label}: mode {mode!r} is not supported in v0.1.0; "
                    "use assisted posting through the phone Shortcut"
                )
    return problems
