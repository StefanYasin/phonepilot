"""End to end CLI tests: init, fleet, workflows, schedules, autorun."""

from __future__ import annotations

import pytest

from phonepilot.cli import main


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("PHONEPILOT_HOME", str(home))
    monkeypatch.delenv("PHONEPILOT_DB", raising=False)
    monkeypatch.delenv("PHONEPILOT_TOKEN", raising=False)
    return home


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_init_copies_templates(cli_home, capsys):
    code, out, _ = run_cli(capsys, "init")
    assert code == 0
    assert (cli_home / "templates" / "photo_post_queue.yaml").is_file()
    assert (cli_home / "templates" / "media" / "queue" / "sample.png").is_file()
    assert (cli_home / "phonepilot.db").is_file()
    assert "copied" in out

    code, out, _ = run_cli(capsys, "init")
    assert code == 0
    assert "already present" in out


def test_fleet_add_list_remove(cli_home, capsys):
    assert run_cli(capsys, "init")[0] == 0
    code, out, _ = run_cli(capsys, "fleet", "add", "board-01", "--kind", "mock")
    assert code == 0
    assert "added board-01" in out

    code, out, _ = run_cli(
        capsys, "fleet", "add", "iphone-01", "--kind", "iphone", "--bridge", "board-01"
    )
    assert code == 0
    assert "bridge board-01" in out

    code, out, _ = run_cli(capsys, "fleet", "list")
    assert code == 0
    assert "board-01" in out and "iphone-01" in out

    code, out, _ = run_cli(capsys, "fleet", "remove", "iphone-01")
    assert code == 0
    assert "removed iphone-01" in out


def test_fleet_add_duplicate_reports_error(cli_home, capsys):
    run_cli(capsys, "init")
    run_cli(capsys, "fleet", "add", "board-01", "--kind", "mock")
    code, _, err = run_cli(capsys, "fleet", "add", "board-01", "--kind", "mock")
    assert code == 1
    assert "already exists" in err


def test_fleet_status_reports_mock_online(cli_home, capsys):
    run_cli(capsys, "init")
    run_cli(capsys, "fleet", "add", "board-01", "--kind", "mock")
    code, out, _ = run_cli(capsys, "fleet", "status")
    assert code == 0
    assert "online" in out


def test_workflows_list_and_validate(cli_home, capsys):
    run_cli(capsys, "init")
    code, out, _ = run_cli(capsys, "workflows", "list")
    assert code == 0
    assert "photo_post_queue" in out

    code, out, _ = run_cli(capsys, "workflows", "validate")
    assert code == 0
    assert "3/3 templates valid" in out

    code, out, _ = run_cli(capsys, "workflows", "validate", "--media")
    assert code == 0
    assert "media checked" in out


def test_workflows_run_dry_run_creates_a_record(cli_home, capsys):
    run_cli(capsys, "init")
    run_cli(capsys, "fleet", "add", "board-01", "--kind", "mock")
    run_cli(
        capsys, "fleet", "add", "iphone-01", "--kind", "iphone", "--bridge", "board-01"
    )

    code, out, _ = run_cli(
        capsys,
        "workflows",
        "run",
        "photo_post_queue",
        "--device",
        "iphone-01",
        "--dry-run",
        "--yes",
    )
    assert code == 0
    assert "finished: success" in out
    assert "social_post" in out

    code, out, _ = run_cli(capsys, "runs", "list")
    assert code == 0
    assert "photo_post_queue" in out


def test_workflows_run_declined_returns_nonzero(cli_home, capsys, monkeypatch):
    run_cli(capsys, "init")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, out, _ = run_cli(capsys, "workflows", "run", "photo_post_queue", "--dry-run")
    assert code == 1
    assert "declined" in out


def test_workflows_run_unknown_template(cli_home, capsys):
    run_cli(capsys, "init")
    code, _, err = run_cli(capsys, "workflows", "run", "nope", "--dry-run", "--yes")
    assert code == 1
    assert "not found" in err


def test_workflows_run_bad_var(cli_home, capsys):
    run_cli(capsys, "init")
    code, _, err = run_cli(
        capsys, "workflows", "run", "photo_post_queue", "--dry-run", "--var", "oops"
    )
    assert code == 1
    assert "name=value" in err


def test_schedule_add_list_disable(cli_home, capsys):
    run_cli(capsys, "init")
    run_cli(capsys, "fleet", "add", "board-01", "--kind", "mock")
    run_cli(
        capsys, "fleet", "add", "iphone-01", "--kind", "iphone", "--bridge", "board-01"
    )

    code, out, _ = run_cli(
        capsys,
        "schedule",
        "add",
        "photo_post_queue",
        "--device",
        "iphone-01",
        "--every",
        "2h",
    )
    assert code == 0
    assert "next run" in out

    code, out, _ = run_cli(capsys, "schedule", "list")
    assert code == 0
    assert "photo_post_queue" in out

    code, out, _ = run_cli(capsys, "schedule", "disable", "photo_post_queue")
    assert code == 0
    assert "disabled" in out

    code, out, _ = run_cli(capsys, "schedule", "enable", "photo_post_queue")
    assert code == 0
    assert "enabled" in out

    code, out, _ = run_cli(capsys, "schedule", "remove", "photo_post_queue")
    assert code == 0
    assert "removed" in out


def test_autorun_without_due_schedules_is_clean(cli_home, capsys):
    run_cli(capsys, "init")
    run_cli(capsys, "fleet", "add", "board-01", "--kind", "mock")
    run_cli(
        capsys, "fleet", "add", "iphone-01", "--kind", "iphone", "--bridge", "board-01"
    )
    run_cli(
        capsys,
        "schedule",
        "add",
        "photo_post_queue",
        "--device",
        "iphone-01",
        "--every",
        "6h",
    )

    code, out, _ = run_cli(capsys, "autorun", "--once", "--yes")
    assert code == 0
    assert "no schedules due" in out


def test_autorun_without_schedules_explains(cli_home, capsys):
    run_cli(capsys, "init")
    code, out, _ = run_cli(capsys, "autorun", "--once")
    assert code == 0
    assert "no schedules yet" in out


def test_workflows_draft_without_key_is_a_clear_error(cli_home, capsys, monkeypatch):
    run_cli(capsys, "init")
    monkeypatch.delenv("PHONEPILOT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code, _, err = run_cli(capsys, "workflows", "draft", "post a photo")
    assert code == 1
    assert "no model key" in err


def test_runs_show(cli_home, capsys):
    run_cli(capsys, "init")
    run_cli(capsys, "fleet", "add", "board-01", "--kind", "mock")
    run_cli(
        capsys, "fleet", "add", "iphone-01", "--kind", "iphone", "--bridge", "board-01"
    )
    _, out, _ = run_cli(
        capsys,
        "workflows",
        "run",
        "photo_post_queue",
        "--device",
        "iphone-01",
        "--dry-run",
        "--yes",
        "--json",
    )
    import json

    run_id = json.loads(out[out.index("{") :])["run_id"]

    code, out, _ = run_cli(capsys, "runs", "show", run_id)
    assert code == 0
    assert "social_post" in out

    code, _, err = run_cli(capsys, "runs", "show", "nope")
    assert code == 1
    assert "not found" in err
