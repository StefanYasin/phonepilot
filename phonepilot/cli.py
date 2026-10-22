"""Command line interface for PhonePilot.

    phonepilot fleet add board-01 --kind esp32 --host 192.168.1.50
    phonepilot fleet add iphone-01 --kind iphone --bridge board-01
    phonepilot workflows run photo_post_queue --device iphone-01

No subcommand is required to be online except the ones that actually send a
command. Everything reads and writes the local SQLite registry.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__, config
from .db import Registry, rows_to_table
from .engine import Runner, RunResult
from .errors import DeviceError, PhonePilotError
from .llm import LLMDrafter, dump_workflow
from .scheduler import Scheduler
from .templates import find_template, load_workflow
from .transport import base_url, make_transport

CONFIRM_WARNING = "warning: --yes approves every confirmation, including social posts"


# --------------------------------------------------------------------- utils
def _parse_vars(pairs: list[str] | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise DeviceError(f"--var {pair!r} must look like name=value")
        key, value = pair.split("=", 1)
        if not key.strip():
            raise DeviceError("--var needs a name before =")
        values[key.strip()] = value
    return values


def _confirm_factory(auto: bool, assume_tty: bool = True):
    def confirm(question: str) -> bool:
        print(question)
        if auto:
            print("approved by --yes")
            return True
        if not (sys.stdin.isatty() and assume_tty):
            print(
                "not a terminal, so this step was declined (use --yes for unattended runs)"
            )
            return False
        answer = input("Approve? [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    return confirm


def _print_result(result: RunResult) -> None:
    rows = [
        {
            "STEP": s.idx + 1,
            "TYPE": s.type,
            "STATUS": s.status,
            "DETAIL": s.detail[:70],
        }
        for s in result.steps
    ]
    print()
    print(f"run {result.run_id}  {result.workflow}  {result.status}")
    print(rows_to_table(rows, ["STEP", "TYPE", "STATUS", "DETAIL"]))
    if result.error:
        print(f"error: {result.error}")


# ------------------------------------------------------------------ commands
def cmd_init(args: argparse.Namespace, registry: Registry) -> int:
    source = config.shipped_templates_dir()
    destination = config.templates_dir()
    copied: list[Path] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.name == "__init__.py":
            continue
        target = destination / path.relative_to(source)
        if target.exists() and not args.force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(target)
    print(f"registry: {registry.path}")
    print(f"templates: {destination}")
    if copied:
        for path in copied:
            print(f"  copied {path.relative_to(destination)}")
    else:
        print("  templates already present (use --force to overwrite)")
    print()
    print("next:")
    print("  phonepilot fleet add board-01 --kind esp32 --host 192.168.1.50")
    print("  phonepilot fleet add iphone-01 --kind iphone --bridge board-01")
    print("  phonepilot workflows run photo_post_queue --device iphone-01 --dry-run")
    return 0


def cmd_fleet_add(args: argparse.Namespace, registry: Registry) -> int:
    token = args.token or os.environ.get("PHONEPILOT_TOKEN")
    device = registry.add_device(
        args.name,
        kind=args.kind,
        host=args.host,
        port=args.port,
        token=token,
        bridge=args.bridge,
        notes=args.notes or "",
    )
    print(f"added {device['name']} ({device['kind']})")
    if device["kind"] == "iphone":
        print(f"  commands go through bridge {device['bridge']}")
    elif device["host"]:
        print(f"  control API {base_url(device)}")
    if not token and device["kind"] == "esp32":
        print("  note: no token stored; set PHONEPILOT_TOKEN to match the firmware")
    return 0


def cmd_fleet_list(args: argparse.Namespace, registry: Registry) -> int:
    devices = registry.list_devices()
    rows = [
        {
            "NAME": d["name"],
            "KIND": d["kind"],
            "BRIDGE": d["bridge"] or "",
            "HOST": d["host"] or "",
            "STATUS": d["status"],
            "LAST SEEN": d["last_seen"] or "",
        }
        for d in devices
    ]
    print(
        rows_to_table(rows, ["NAME", "KIND", "BRIDGE", "HOST", "STATUS", "LAST SEEN"])
    )
    return 0


def cmd_fleet_remove(args: argparse.Namespace, registry: Registry) -> int:
    if not registry.remove_device(args.name):
        raise DeviceError(f"device {args.name!r} is not registered")
    print(f"removed {args.name}")
    return 0


def cmd_fleet_status(args: argparse.Namespace, registry: Registry) -> int:
    devices = (
        [registry.require_device(args.name)] if args.name else registry.list_devices()
    )
    if not devices:
        print("no devices registered yet; add one with: phonepilot fleet add ...")
        return 0
    rows = []
    failures = 0
    for device in devices:
        control = (
            registry.control_device(device) if device["kind"] == "iphone" else device
        )
        detail = ""
        status = "offline"
        if control["kind"] == "iphone":
            detail = "register the ESP32 bridge instead"
            failures += 1
        else:
            try:
                info = make_transport(control).status()
                status = "online" if info.get("ok", True) else "error"
                bits = []
                if "firmware" in info:
                    bits.append(f"fw {info['firmware']}")
                if "ble_connected" in info:
                    bits.append("ble " + ("on" if info["ble_connected"] else "off"))
                if "queue" in info:
                    bits.append(f"queue {info['queue']}")
                if "ip" in info:
                    bits.append(str(info["ip"]))
                detail = ", ".join(bits)
            except PhonePilotError as exc:
                detail = str(exc)[:90]
                failures += 1
            registry.update_device_status(device["name"], status)
        rows.append(
            {
                "NAME": device["name"],
                "KIND": device["kind"],
                "STATUS": status,
                "DETAIL": detail,
            }
        )
    print(rows_to_table(rows, ["NAME", "KIND", "STATUS", "DETAIL"]))
    return 0 if failures == 0 else 1


def _template_paths(directory: str | None) -> list[Path]:
    if directory:
        folder = Path(directory).expanduser()
        if not folder.is_dir():
            raise DeviceError(f"{folder} is not a directory")
        return sorted(folder.glob("*.yaml"))
    seen: dict[str, Path] = {}
    for folder in (config.templates_dir(), config.shipped_templates_dir()):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            seen.setdefault(path.name, path)
    return [seen[name] for name in sorted(seen)]


def cmd_workflows_list(args: argparse.Namespace, registry: Registry) -> int:
    rows = []
    for path in _template_paths(args.dir):
        try:
            workflow = load_workflow(path)
            rows.append(
                {
                    "NAME": workflow.name,
                    "STEPS": len(workflow.steps),
                    "TAGS": ",".join(workflow.tags),
                    "DESCRIPTION": workflow.description[:56],
                    "SOURCE": str(path),
                }
            )
        except PhonePilotError as exc:
            rows.append(
                {
                    "NAME": path.stem,
                    "STEPS": "-",
                    "TAGS": "invalid",
                    "DESCRIPTION": str(exc).splitlines()[0][:56],
                    "SOURCE": str(path),
                }
            )
    print(rows_to_table(rows, ["NAME", "STEPS", "TAGS", "DESCRIPTION", "SOURCE"]))
    return 0


def cmd_workflows_validate(args: argparse.Namespace, registry: Registry) -> int:
    paths = [Path(p) for p in args.paths] if args.paths else _template_paths(None)
    if not paths:
        print("no templates found")
        return 1
    failures = 0
    for path in paths:
        try:
            workflow = load_workflow(path, check_media=args.media)
        except PhonePilotError as exc:
            failures += 1
            print(f"FAIL {path}")
            for line in str(exc).splitlines():
                print(f"     {line}")
            continue
        extra = " (media checked)" if args.media else ""
        print(f"OK   {workflow.name:<24} {len(workflow.steps)} steps  {path}{extra}")
    print()
    print(f"{len(paths) - failures}/{len(paths)} templates valid")
    return 0 if failures == 0 else 1


def cmd_workflows_run(args: argparse.Namespace, registry: Registry) -> int:
    path = find_template(args.workflow)
    workflow = load_workflow(path)
    if args.yes:
        print(CONFIRM_WARNING)
    runner = Runner(
        registry,
        dry_run=args.dry_run,
        confirm=_confirm_factory(args.yes),
        echo=(lambda _message: None) if args.json else print,
        variables=_parse_vars(args.var),
    )
    result = runner.run(workflow, device_name=args.device)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_result(result)
    return 0 if result.status in ("success", "partial") else 1


def cmd_workflows_draft(args: argparse.Namespace, registry: Registry) -> int:
    drafter = LLMDrafter(base_url=args.base_url, model=args.model)
    if not drafter.configured:
        raise DeviceError(
            "no model key configured; set PHONEPILOT_LLM_API_KEY or OPENAI_API_KEY, "
            "or copy a shipped template instead"
        )
    workflow = drafter.draft(args.request, device=args.device)
    text = dump_workflow(workflow)
    print(text)
    if args.out:
        out = Path(args.out).expanduser()
        if out.exists() and not args.force:
            raise DeviceError(f"{out} exists; pass --force to overwrite")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {out}", file=sys.stderr)
    return 0


def cmd_schedule_add(args: argparse.Namespace, registry: Registry) -> int:
    runner = Runner(registry, confirm=_confirm_factory(False), echo=print)
    scheduler = Scheduler(registry, runner)
    schedule = scheduler.add(
        args.workflow,
        args.device,
        name=args.name,
        every=args.every,
        daily=args.daily,
    )
    when = (
        f"every {args.every}"
        if schedule["kind"] == "interval"
        else f"daily at {schedule['at_time']}"
    )
    print(
        f"added schedule {schedule['name']} ({when}); next run {schedule['next_run']}"
    )
    return 0


def cmd_schedule_list(args: argparse.Namespace, registry: Registry) -> int:
    rows = [
        {
            "NAME": s["name"],
            "WORKFLOW": s["workflow"],
            "DEVICE": s["device"],
            "WHEN": f"every {s['interval_s']}s"
            if s["kind"] == "interval"
            else f"daily {s['at_time']}",
            "NEXT RUN": s["next_run"],
            "LAST RUN": s["last_run"] or "",
            "ON": s["enabled"],
        }
        for s in registry.list_schedules()
    ]
    print(
        rows_to_table(
            rows, ["NAME", "WORKFLOW", "DEVICE", "WHEN", "NEXT RUN", "LAST RUN", "ON"]
        )
    )
    return 0


def cmd_schedule_remove(args: argparse.Namespace, registry: Registry) -> int:
    if not registry.remove_schedule(args.name):
        raise DeviceError(f"schedule {args.name!r} not found")
    print(f"removed schedule {args.name}")
    return 0


def cmd_schedule_toggle(args: argparse.Namespace, registry: Registry) -> int:
    if not registry.set_schedule_enabled(args.name, args.enabled):
        raise DeviceError(f"schedule {args.name!r} not found")
    print(f"schedule {args.name} {'enabled' if args.enabled else 'disabled'}")
    return 0


def cmd_autorun(args: argparse.Namespace, registry: Registry) -> int:
    if args.yes:
        print(CONFIRM_WARNING)
    runner = Runner(
        registry,
        dry_run=args.dry_run,
        confirm=_confirm_factory(args.yes),
        echo=print,
    )
    scheduler = Scheduler(registry, runner)
    if not registry.list_schedules():
        print("no schedules yet; add one with: phonepilot schedule add ...")
        return 0
    failures = 0
    while True:
        outcomes = scheduler.run_due()
        if not outcomes:
            print("no schedules due")
        for schedule, result, error in outcomes:
            if result is None:
                failures += 1
                print(f"{schedule['name']}: failed to start: {error}")
            else:
                print(f"{schedule['name']}: {result.workflow} {result.status}")
                if error:
                    failures += 1
                    print(f"  {error}")
        if not args.loop:
            break
        time.sleep(args.poll)
    return 1 if failures else 0


def cmd_runs_list(args: argparse.Namespace, registry: Registry) -> int:
    rows = [
        {
            "RUN": r["id"],
            "WORKFLOW": r["workflow"],
            "DEVICE": r["device"],
            "STATUS": r["status"],
            "TRIGGER": r["trigger"],
            "STARTED": r["started_at"],
        }
        for r in registry.list_runs(limit=args.limit)
    ]
    print(
        rows_to_table(
            rows, ["RUN", "WORKFLOW", "DEVICE", "STATUS", "TRIGGER", "STARTED"]
        )
    )
    return 0


def cmd_runs_show(args: argparse.Namespace, registry: Registry) -> int:
    run = registry.get_run(args.run_id)
    if run is None:
        raise DeviceError(f"run {args.run_id!r} not found")
    print(f"run      {run['id']}")
    print(f"workflow {run['workflow']}")
    print(f"device   {run['device']}")
    print(f"status   {run['status']}")
    print(f"started  {run['started_at']}")
    print(f"finished {run['finished_at'] or '-'}")
    if run["error"]:
        print(f"error    {run['error']}")
    print()
    steps = registry.get_run_steps(args.run_id)
    rows = [
        {
            "STEP": s["idx"] + 1,
            "ID": s["step_id"],
            "TYPE": s["type"],
            "STATUS": s["status"],
            "DETAIL": (s["detail"] or "")[:70],
        }
        for s in steps
    ]
    print(rows_to_table(rows, ["STEP", "ID", "TYPE", "STATUS", "DETAIL"]))
    return 0


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phonepilot",
        description="Local control plane for ESP32 driven iPhone automation.",
    )
    parser.add_argument(
        "--home", help="PhonePilot home directory (default ~/.phonepilot)"
    )
    parser.add_argument(
        "--version", action="version", version=f"phonepilot {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create the registry and copy example templates")
    init.add_argument(
        "--force", action="store_true", help="overwrite existing templates"
    )
    init.set_defaults(func=cmd_init)

    fleet = sub.add_parser("fleet", help="manage boards and phones")
    fleet_sub = fleet.add_subparsers(dest="fleet_command", required=True)

    add = fleet_sub.add_parser("add", help="register a device")
    add.add_argument("name")
    add.add_argument("--kind", choices=("esp32", "iphone", "mock"), default="esp32")
    add.add_argument("--host", help="board IP or hostname")
    add.add_argument("--port", type=int, default=80)
    add.add_argument("--token", help="shared token (defaults to $PHONEPILOT_TOKEN)")
    add.add_argument("--bridge", help="esp32 device name, required for iphone")
    add.add_argument("--notes", default="")
    add.set_defaults(func=cmd_fleet_add)

    listing = fleet_sub.add_parser("list", help="list registered devices")
    listing.set_defaults(func=cmd_fleet_list)

    remove = fleet_sub.add_parser("remove", help="forget a device")
    remove.add_argument("name")
    remove.set_defaults(func=cmd_fleet_remove)

    status = fleet_sub.add_parser("status", help="ask boards for their state")
    status.add_argument("name", nargs="?")
    status.set_defaults(func=cmd_fleet_status)

    workflows = sub.add_parser(
        "workflows", help="list, validate, run and draft templates"
    )
    workflow_sub = workflows.add_subparsers(dest="workflow_command", required=True)

    wlist = workflow_sub.add_parser("list", help="list available templates")
    wlist.add_argument("--dir")
    wlist.set_defaults(func=cmd_workflows_list)

    validate = workflow_sub.add_parser(
        "validate", help="check templates without running"
    )
    validate.add_argument("paths", nargs="*")
    validate.add_argument(
        "--media", action="store_true", help="also check that media files exist"
    )
    validate.set_defaults(func=cmd_workflows_validate)

    run = workflow_sub.add_parser("run", help="execute a template")
    run.add_argument("workflow", help="template name or path")
    run.add_argument("--device", help="override the device in the template")
    run.add_argument(
        "--dry-run", action="store_true", help="print commands, send nothing"
    )
    run.add_argument(
        "--yes", action="store_true", help="approve confirmations without prompting"
    )
    run.add_argument("--var", action="append", metavar="NAME=VALUE")
    run.add_argument("--json", action="store_true", help="machine readable result")
    run.set_defaults(func=cmd_workflows_run)

    draft = workflow_sub.add_parser(
        "draft", help="turn a request into a template with a model"
    )
    draft.add_argument("request")
    draft.add_argument("--device")
    draft.add_argument("--out", help="write the YAML here")
    draft.add_argument("--force", action="store_true")
    draft.add_argument("--model")
    draft.add_argument("--base-url")
    draft.set_defaults(func=cmd_workflows_draft)

    schedule = sub.add_parser("schedule", help="autorun schedules")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)

    sadd = schedule_sub.add_parser("add", help="schedule a template")
    sadd.add_argument("workflow")
    sadd.add_argument("--device", required=True)
    sadd.add_argument("--name")
    sadd.add_argument("--every", help="interval such as 30m, 6h, 1d")
    sadd.add_argument("--daily", help="local time HH:MM")
    sadd.set_defaults(func=cmd_schedule_add)

    slist = schedule_sub.add_parser("list")
    slist.set_defaults(func=cmd_schedule_list)

    sremove = schedule_sub.add_parser("remove")
    sremove.add_argument("name")
    sremove.set_defaults(func=cmd_schedule_remove)

    senable = schedule_sub.add_parser("enable")
    senable.add_argument("name")
    senable.set_defaults(func=cmd_schedule_toggle, enabled=True)

    sdisable = schedule_sub.add_parser("disable")
    sdisable.add_argument("name")
    sdisable.set_defaults(func=cmd_schedule_toggle, enabled=False)

    autorun = sub.add_parser("autorun", help="run due schedules once, or poll forever")
    autorun.add_argument(
        "--once", action="store_true", help="run due schedules and exit (default)"
    )
    autorun.add_argument("--loop", action="store_true", help="keep polling")
    autorun.add_argument(
        "--poll", type=int, default=60, help="seconds between polls (default 60)"
    )
    autorun.add_argument("--dry-run", action="store_true")
    autorun.add_argument(
        "--yes", action="store_true", help="approve confirmations without prompting"
    )
    autorun.set_defaults(func=cmd_autorun)

    runs = sub.add_parser("runs", help="inspect past runs")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    rlist = runs_sub.add_parser("list")
    rlist.add_argument("--limit", type=int, default=20)
    rlist.set_defaults(func=cmd_runs_list)
    rshow = runs_sub.add_parser("show")
    rshow.add_argument("run_id")
    rshow.set_defaults(func=cmd_runs_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.home:
        os.environ["PHONEPILOT_HOME"] = str(Path(args.home).expanduser())
    config.ensure_home()
    registry = Registry(config.db_path())
    try:
        return int(args.func(args, registry))
    except PhonePilotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130
    finally:
        registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
