"""Shared builders for the test suite."""

from __future__ import annotations

from typing import Any

from phonepilot.templates import parse_workflow


def make_workflow(steps: list[dict[str, Any]], **overrides: Any):
    """Parse a minimal valid template around the given steps."""
    data: dict[str, Any] = {
        "name": "test_flow",
        "version": 1,
        "steps": steps,
    }
    data.update(overrides)
    return parse_workflow(data)
