"""Turn a plain language request into a workflow draft.

Works with any OpenAI compatible chat completions endpoint. The model is asked
for JSON only, the result is validated by the same strict template parser used
everywhere else, and one repair round is allowed when validation fails.

The model never gets to run anything. A draft is a file the operator reads
before ``phonepilot workflows run`` touches it.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .errors import DraftError, TemplateError, TransportError
from .templates import STEP_TYPES, Workflow, dump_workflow, parse_workflow
from .transport import http_request

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

SYSTEM_PROMPT = """You write PhonePilot workflow templates as JSON.

Return one JSON object and nothing else. No markdown fences, no commentary.

Schema:
{
  "name": "lowercase-slug",
  "version": 1,
  "description": "one sentence",
  "device": "optional device name",
  "steps": [ ... ]
}

Allowed step objects, each with a unique lowercase "id":
- {"id": "...", "type": "trigger_shortcut", "shortcut": "Shortcut name", "wait_after": 2}
- {"id": "...", "type": "send_keys", "text": "text to type", "enter": true}
- {"id": "...", "type": "wait", "seconds": 2}
- {"id": "...", "type": "api_call", "method": "GET", "url": "https://...", "expect_status": 200}
- {"id": "...", "type": "social_post", "platform": "instagram", "media": "media/queue/photo.jpg", "caption": "text", "mode": "assisted", "confirm_required": true}

Rules:
- Use only the five step types above and only their listed fields.
- social_post always has "confirm_required": true and "mode": "assisted". Never relax them.
- Never add engagement automation (auto like, follow, comment, DM), rate limit workarounds or proxy rotation.
- Put secrets in {{ env.VAR_NAME }} placeholders, never literal keys.
- Captions should be plain text. Keep workflows between 2 and 8 steps.
- If the request is vague, make a small safe draft rather than inventing accounts or URLs.
"""


def _extract_json(text: str) -> Any:
    cleaned = FENCE_RE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError as exc:
                raise DraftError(f"model returned invalid JSON: {exc}") from exc
        raise DraftError("model did not return a JSON object")


class LLMDrafter:
    """Minimal OpenAI compatible client for template drafting."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("PHONEPILOT_LLM_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.api_key = (
            api_key
            or os.environ.get("PHONEPILOT_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY", "")
        )
        self.model = model or os.environ.get("PHONEPILOT_LLM_MODEL") or DEFAULT_MODEL
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _chat(self, messages: list[dict[str, str]]) -> str:
        if not self.api_key:
            raise DraftError(
                "no model API key; set PHONEPILOT_LLM_API_KEY or OPENAI_API_KEY, "
                "or write the template by hand"
            )
        try:
            response = http_request(
                "POST",
                self.base_url + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json_body={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.timeout,
            )
        except TransportError as exc:
            raise DraftError(f"model request failed: {exc}") from exc
        payload = response.get("json")
        try:
            return payload["choices"][0]["message"]["content"]
        except (TypeError, KeyError, IndexError) as exc:
            raise DraftError(
                f"unexpected model response: {str(payload)[:300]}"
            ) from exc

    def draft(
        self,
        request: str,
        device: str | None = None,
        context: str = "",
        repair_rounds: int = 1,
    ) -> Workflow:
        """Ask the model for a template, validate it, optionally ask for a fix."""
        if not request.strip():
            raise DraftError("describe what the workflow should do")
        hint = f"\nTarget device: {device}" if device else ""
        if context.strip():
            hint += f"\nExtra context: {context.strip()}"
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": request.strip() + hint},
        ]
        last_error = ""
        for attempt in range(repair_rounds + 1):
            content = self._chat(messages)
            data = _extract_json(content)
            if isinstance(data, dict):
                if device and not data.get("device"):
                    data["device"] = device
                for step in data.get("steps", []) or []:
                    if isinstance(step, dict) and step.get("type") == "social_post":
                        step["confirm_required"] = True
                        step.setdefault("mode", "assisted")
            try:
                return parse_workflow(data)
            except TemplateError as exc:
                last_error = str(exc)
                if attempt >= repair_rounds:
                    raise DraftError(
                        f"model draft failed validation:\n{last_error}"
                    ) from exc
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": "That draft failed validation. Fix every problem and return JSON only:\n"
                        + last_error,
                    }
                )
        raise DraftError(last_error or "could not draft a workflow")


def draft_yaml(workflow: Workflow) -> str:
    """YAML text for a drafted workflow."""
    return dump_workflow(workflow)


def example_schema() -> dict[str, Any]:
    """Small helper used by tests and docs."""
    return {"step_types": list(STEP_TYPES)}
