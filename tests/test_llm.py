"""Drafting workflows with an OpenAI compatible endpoint."""

from __future__ import annotations

import json

import pytest

from phonepilot.errors import DraftError
from phonepilot.llm import LLMDrafter, _extract_json


def valid_draft(**overrides):
    data = {
        "name": "drafted_flow",
        "version": 1,
        "description": "drafted",
        "steps": [
            {"type": "send_keys", "text": "hello"},
            {
                "type": "social_post",
                "platform": "instagram",
                "media": "photo.jpg",
                "caption": "hi",
            },
        ],
    }
    data.update(overrides)
    return json.dumps(data)


def test_extract_json_handles_fences():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('here you go: {"a": 2}') == {"a": 2}
    with pytest.raises(DraftError):
        _extract_json("not json at all")


def test_draft_forces_confirmation_even_if_model_relaxes_it(monkeypatch):
    drafter = LLMDrafter(api_key="test-key")
    payload = valid_draft()
    data = json.loads(payload)
    data["steps"][1]["confirm_required"] = False
    monkeypatch.setattr(drafter, "_chat", lambda messages: json.dumps(data))

    workflow = drafter.draft("post the next photo")

    assert workflow.steps[1].params["confirm_required"] is True


def test_draft_attaches_the_device(monkeypatch):
    drafter = LLMDrafter(api_key="test-key")
    monkeypatch.setattr(drafter, "_chat", lambda messages: valid_draft())
    workflow = drafter.draft("post a photo", device="iphone-01")
    assert workflow.device == "iphone-01"


def test_draft_repairs_once(monkeypatch):
    drafter = LLMDrafter(api_key="test-key")
    calls: list[list[dict[str, str]]] = []

    def fake_chat(messages):
        calls.append(list(messages))
        if len(calls) == 1:
            return json.dumps(
                {"name": "bad", "version": 1, "steps": [{"type": "teleport"}]}
            )
        return valid_draft()

    monkeypatch.setattr(drafter, "_chat", fake_chat)
    workflow = drafter.draft("do something")

    assert workflow.name == "drafted_flow"
    assert len(calls) == 2
    assert "failed validation" in calls[1][-1]["content"]


def test_draft_gives_up_after_repair_round(monkeypatch):
    drafter = LLMDrafter(api_key="test-key")
    monkeypatch.setattr(
        drafter,
        "_chat",
        lambda messages: json.dumps(
            {"name": "bad", "version": 1, "steps": [{"type": "teleport"}]}
        ),
    )
    with pytest.raises(DraftError) as caught:
        drafter.draft("do something")
    assert "failed validation" in str(caught.value)


def test_draft_without_key(monkeypatch):
    monkeypatch.delenv("PHONEPILOT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    drafter = LLMDrafter(api_key="")
    assert drafter.configured is False
    with pytest.raises(DraftError) as caught:
        drafter.draft("post a photo")
    assert "no model API key" in str(caught.value)


def test_draft_over_http(board):
    drafter = LLMDrafter(
        base_url=board.base + "/v1", api_key="secret", model="test-model"
    )
    workflow = drafter.draft("post the next photo")
    assert workflow.name == "drafted_post"

    request = board.requests[-1]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == "Bearer secret"
    assert request["body"]["model"] == "test-model"
    assert request["body"]["response_format"] == {"type": "json_object"}


def test_draft_rejects_empty_request():
    with pytest.raises(DraftError):
        LLMDrafter(api_key="x").draft("   ")
