"""Template parsing, validation, policy rails, and the shipped examples."""

from __future__ import annotations

import pytest
import yaml
from helpers import make_workflow

from phonepilot.config import shipped_templates_dir
from phonepilot.errors import TemplateError
from phonepilot.templates import (
    find_template,
    load_workflow,
    parse_workflow,
    resolve_media,
)


def test_minimal_workflow_parses_and_fills_defaults():
    workflow = make_workflow(
        [
            {
                "type": "social_post",
                "platform": "instagram",
                "media": "photo.jpg",
                "caption": "hello",
            }
        ]
    )
    step = workflow.steps[0]
    assert workflow.name == "test_flow"
    assert workflow.version == 1
    assert step.id == "social_post_1"
    assert step.params["confirm_required"] is True


def test_step_ids_are_preserved():
    workflow = make_workflow([{"id": "first_move", "type": "wait", "seconds": 1}])
    assert workflow.steps[0].id == "first_move"


def test_social_post_without_confirmation_is_rejected():
    with pytest.raises(TemplateError) as caught:
        make_workflow(
            [
                {
                    "type": "social_post",
                    "platform": "instagram",
                    "media": "photo.jpg",
                    "caption": "hello",
                    "confirm_required": False,
                }
            ]
        )
    assert "confirm_required" in str(caught.value)


def test_defaults_cannot_switch_confirmation_off():
    with pytest.raises(TemplateError) as caught:
        make_workflow(
            [
                {
                    "type": "social_post",
                    "platform": "x",
                    "media": "photo.jpg",
                    "caption": "hi",
                }
            ],
            defaults={"confirm_required": False},
        )
    assert "confirm_required" in str(caught.value)


def test_unknown_step_field_catches_typos():
    with pytest.raises(TemplateError) as caught:
        make_workflow([{"type": "send_keys", "text": "hi", "confirm": True}])
    assert "unknown field" in str(caught.value)


def test_unknown_step_type_lists_allowed_types():
    with pytest.raises(TemplateError) as caught:
        make_workflow([{"type": "teleport", "target": "phone"}])
    message = str(caught.value)
    assert "type must be one of" in message
    assert "social_post" in message


def test_missing_required_field():
    with pytest.raises(TemplateError) as caught:
        make_workflow([{"type": "trigger_shortcut"}])
    assert "shortcut" in str(caught.value)


def test_duplicate_step_ids_are_rejected():
    with pytest.raises(TemplateError) as caught:
        make_workflow(
            [
                {"id": "same", "type": "wait", "seconds": 1},
                {"id": "same", "type": "wait", "seconds": 2},
            ]
        )
    assert "duplicate id" in str(caught.value)


def test_unknown_top_level_field_is_rejected():
    with pytest.raises(TemplateError) as caught:
        make_workflow([{"type": "wait", "seconds": 1}], schedule="daily")
    assert "unknown top level field" in str(caught.value)


def test_bad_expect_status_is_rejected():
    with pytest.raises(TemplateError) as caught:
        make_workflow(
            [{"type": "api_call", "url": "https://example.com", "expect_status": 999}]
        )
    assert "expect_status" in str(caught.value)


def test_api_call_json_and_body_are_mutually_exclusive():
    with pytest.raises(TemplateError) as caught:
        make_workflow(
            [
                {
                    "type": "api_call",
                    "url": "https://example.com",
                    "json": {"a": 1},
                    "body": "raw",
                }
            ]
        )
    assert "either json or body" in str(caught.value)


def test_get_method_is_normalized():
    workflow = make_workflow(
        [{"type": "api_call", "url": "https://example.com", "method": "get"}]
    )
    assert workflow.steps[0].params["method"] == "GET"


def test_wait_seconds_must_be_sane():
    with pytest.raises(TemplateError):
        make_workflow([{"type": "wait", "seconds": 99999}])


def test_social_platform_must_be_known():
    with pytest.raises(TemplateError) as caught:
        make_workflow(
            [
                {
                    "type": "social_post",
                    "platform": "myspace",
                    "media": "a.jpg",
                    "caption": "hi",
                }
            ]
        )
    assert "platform" in str(caught.value)


def test_media_check_passes_for_existing_file(tmp_path):
    media = tmp_path / "photo.png"
    media.write_bytes(b"not really a png")
    source = tmp_path / "flow.yaml"
    data = {
        "name": "media_flow",
        "version": 1,
        "steps": [
            {
                "type": "social_post",
                "platform": "instagram",
                "media": "photo.png",
                "caption": "hi",
            }
        ],
    }
    workflow = parse_workflow(data, source=source, check_media=True)
    assert workflow.steps[0].params["media"] == "photo.png"


def test_media_check_fails_for_missing_file(tmp_path):
    data = {
        "name": "media_flow",
        "version": 1,
        "steps": [
            {
                "type": "social_post",
                "platform": "instagram",
                "media": "nope.png",
                "caption": "hi",
            }
        ],
    }
    with pytest.raises(TemplateError) as caught:
        parse_workflow(data, source=tmp_path / "flow.yaml", check_media=True)
    assert "media file not found" in str(caught.value)


def test_policy_blocks_fake_engagement():
    with pytest.raises(TemplateError) as caught:
        make_workflow([{"type": "send_keys", "text": "hi", "auto_like": True}])
    assert "fake engagement" in str(caught.value)


def test_policy_blocks_rate_limit_retry():
    with pytest.raises(TemplateError) as caught:
        make_workflow(
            [{"type": "api_call", "url": "https://example.com", "retry_on_429": True}]
        )
    assert "rate limit" in str(caught.value)


def test_policy_blocks_direct_api_posting():
    with pytest.raises(TemplateError) as caught:
        make_workflow(
            [
                {
                    "type": "social_post",
                    "platform": "instagram",
                    "media": "a.jpg",
                    "caption": "hi",
                    "mode": "api",
                }
            ]
        )
    assert "assisted" in str(caught.value)


def test_all_shipped_templates_are_valid_with_media():
    templates = sorted(shipped_templates_dir().glob("*.yaml"))
    assert len(templates) >= 3
    for path in templates:
        workflow = load_workflow(path, check_media=True)
        assert workflow.steps, path.name


def test_shipped_photo_template_has_a_confirmed_social_step():
    workflow = load_workflow(
        shipped_templates_dir() / "photo_post_queue.yaml", check_media=True
    )
    social = [s for s in workflow.steps if s.type == "social_post"]
    assert social, "the flagship template must post something"
    assert social[0].params["confirm_required"] is True
    assert social[0].params["platform"] == "instagram"


def test_load_missing_file_raises():
    with pytest.raises(TemplateError):
        load_workflow("does-not-exist.yaml")


def test_bad_yaml_reports_file_name(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(TemplateError) as caught:
        load_workflow(path)
    assert "broken.yaml" in str(caught.value)


def test_find_template_by_name():
    path = find_template("photo_post_queue")
    assert path.name == "photo_post_queue.yaml"


def test_find_template_unknown_name():
    with pytest.raises(TemplateError):
        find_template("definitely_not_here")


def test_resolve_media_prefers_template_directory(tmp_path, monkeypatch):
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"x")
    resolved = resolve_media("photo.jpg", tmp_path)
    assert resolved == media.resolve()


def test_yaml_round_trip_has_no_python_objects():
    workflow = make_workflow([{"type": "wait", "seconds": 0.5}])
    text = yaml.safe_dump(workflow.to_dict())
    assert "steps:" in text
    assert "!!python" not in text
