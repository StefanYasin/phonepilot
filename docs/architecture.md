# Architecture

PhonePilot is three processes and one file format.

- A Python control plane on your computer.
- An ESP32 firmware image that owns the radio and the button.
- iOS Shortcuts on the phone, which owns the apps.
- YAML workflow templates, which are the contract between them.

Nothing is hidden in a binary. A workflow you cannot read is a workflow you
should not run.

## Components

| Component | Lives in | Responsibility |
|---|---|---|
| CLI | `phonepilot/cli.py` | `fleet`, `workflows`, `schedule`, `autorun`, `runs` |
| Engine | `phonepilot/engine.py` | step execution, interpolation, confirmations, run records |
| Template parser | `phonepilot/templates.py` | strict schema, defaults, media resolution |
| Policy | `phonepilot/policy.py` | refuses engagement faking and rate limit evasion |
| Registry | `phonepilot/db.py` | SQLite devices, runs, steps, schedules |
| Scheduler | `phonepilot/scheduler.py` | interval and daily next run calculation |
| Transports | `phonepilot/transport.py` | HTTP board client, mock, dry run |
| Drafter | `phonepilot/llm.py` | prompt, JSON parsing, validation, one repair round |
| Firmware | `firmware/phonepilot_esp32/` | BLE HID, button, HTTP API, job queue |
| Phone bridge | `docs/ios-setup.md` | Shortcuts that poll and execute |

## A run, end to end

```
phonepilot workflows run photo_post_queue --device iphone-01
        |
        | 1. parse and validate the template
        | 2. resolve iphone-01 to its bridge board (board-01)
        | 3. create a run row in SQLite
        v
    trigger_shortcut  --- POST /api/v1/shortcut --->  board queue
        |
    wait              --- local sleep ------------->  (no network)
        |
    social_post       --- terminal confirmation
                      --- POST /api/v1/post ------>  board queue
        |
        v
    run row status success, steps recorded

Phone, independently:
    PhonePilot Sync -> GET /api/v1/next -> menu -> share sheet -> POST /api/v1/result
```

The control plane does not wait for the phone. A queued social post is a
handoff, not a synchronous post. The board records the job, the phone reports
back when a human finishes, and `GET /api/v1/status` shows `last_result`.

This is a deliberate trade. Waiting would block a CLI run for as long as it
takes you to write a caption. A queue lets you run the workflow, walk away, and
deal with the phone when you pick it up.

## Template schema

Top level:

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | slug | yes | Lowercase, digits, hyphen, underscore. 2 to 64 characters. |
| `version` | integer | no | Must be `1`. |
| `description` | string | no | One sentence for `workflows list`. |
| `device` | string | no | Registry device name. `--device` overrides it. |
| `defaults` | mapping | no | Only `confirm_required: true` and `timeout_s`. |
| `tags` | list of strings | no | For your own filtering. |
| `steps` | list | yes | 1 to 50 steps. |

Step common fields:

| Field | Type | Notes |
|---|---|---|
| `id` | slug | Unique. Auto generated as `<type>_<n>` when omitted. |
| `type` | string | One of the five below. |
| `continue_on_error` | boolean | Default false. A failed step stops the run. |

### `trigger_shortcut`

Asks the phone to run a named Shortcut.

| Field | Required | Notes |
|---|---|---|
| `shortcut` | yes | Exact Shortcut name on the phone. |
| `wait_after` | no | Seconds to sleep after queueing, 0 to 600. |

The board does not run the Shortcut. It queues a job, and `PhonePilot Sync`
calls `Run Shortcut` with the name.

### `send_keys`

Types over BLE HID into whatever has focus on the phone.

| Field | Required | Notes |
|---|---|---|
| `text` | yes | Up to 512 characters, ASCII. |
| `enter` | no | Press Return after the text. Default false. |
| `delay_ms` | no | Per character delay, 0 to 5000, capped at 50 on the board. |

This is the blunt instrument. It works in any app, including apps with no API,
but it depends on focus and layout. Prefer a Shortcut when one exists.

### `wait`

| Field | Required | Notes |
|---|---|---|
| `seconds` | yes | 0 to 3600. Dry runs do not actually sleep. |

### `api_call`

| Field | Required | Notes |
|---|---|---|
| `url` | yes | `http://` or `https://`. |
| `method` | no | GET, POST, PUT, PATCH, DELETE. Default GET. |
| `headers` | no | String to string mapping. |
| `json` | no | Serialized as the JSON body. Mutually exclusive with `body`. |
| `body` | no | Raw string body. |
| `expect_status` | no | Integer or list. Default 200, 201, 202, 204. |
| `timeout_s` | no | 1 to 300. Default from `defaults.timeout_s`, else 20. |

The response is stored on the step as `status`, `json`, and `text`, which later
steps can read through interpolation. Dry runs do not perform the request.

### `social_post`

| Field | Required | Notes |
|---|---|---|
| `platform` | yes | instagram, tiktok, x, linkedin, facebook, threads, youtube, pinterest, custom. |
| `media` | yes | Path to a photo or video. Relative paths resolve next to the template, then the cwd. |
| `caption` | yes | Plain text. |
| `mode` | no | Only `assisted` in v0.1.0. |
| `confirm_required` | yes, effectively | Must be `true`. Anything else is a validation error. |
| `target` | no | Passed through for a profile, page, or album name. |

The engine checks the media file exists, asks for confirmation, then queues the
job. The phone asks again. See [safety.md](safety.md).

## Interpolation

Strings may contain `{{ reference }}`. Whole-string references keep their type,
so `seconds: "{{ vars.delay }}"` works with a numeric variable.

| Reference | Example | Value |
|---|---|---|
| `env.NAME` | `{{ env.PHONEPILOT_LOG_URL }}` | Environment variable, error if unset. |
| `vars.NAME` | `{{ vars.caption }}` | From `--var caption=...`. |
| `run.id` | `{{ run.id }}` | The 12 character run id. |
| `run.workflow` | `{{ run.workflow }}` | Template name. |
| `run.device` | `{{ run.device }}` | Device name. |
| `steps.ID.status` | `{{ steps.fetch.status }}` | HTTP status or step status. |
| `steps.ID.json.PATH` | `{{ steps.fetch.json.data.title }}` | Nested response value. |
| `steps.ID.text` | `{{ steps.fetch.text }}` | Raw response text, first 4000 characters. |

Unknown references fail the step with a message naming the reference. Secrets
should be `{{ env.NAME }}`, never literal strings in a template.

## Failure semantics

| Situation | Step status | Run status |
|---|---|---|
| Every step succeeds | `ok` | `success` |
| A step fails, no `continue_on_error` | `failed` | `failed`, later steps `skipped` |
| A step fails with `continue_on_error` | `failed` | `partial` if the rest finishes |
| A social confirmation is declined | `declined` | `aborted`, later steps `skipped` |

A declined confirmation always stops the run, even with `continue_on_error`.
Saying no to a post is not a soft error.

`phonepilot runs show <id>` reads the same records, and `--json` returns the
full structure for another program.

## Registry

SQLite, one file, default `~/.phonepilot/phonepilot.db`.

```
devices(id, name UNIQUE, kind, host, port, token, bridge, status, last_seen, notes, created_at)
runs(id, workflow, device, trigger, status, started_at, finished_at, error)
run_steps(id, run_id, idx, step_id, type, status, started_at, finished_at, detail)
schedules(name, workflow, device, kind, at_time, interval_s, enabled, last_run, next_run, created_at)
```

Kinds are `esp32`, `iphone`, and `mock`. An iPhone row has no host; it points at
a board through `bridge`. `Registry.control_device` follows that edge, so every
transport call lands on a board.

`mock` is a board you can demo without hardware. It records calls in memory and
answers with plausible JSON. It is not a substitute for the HTTP tests, which
run against a real local server.

## Scheduling

Two kinds:

- `interval`: `--every 30m`, `6h`, `1d`. Minimum 60 seconds.
- `daily`: `--daily 07:30`, interpreted in the local timezone, stored in UTC.

`phonepilot autorun --once` runs everything with `next_run <= now`, advances
each schedule whether the run succeeded or not, and exits. `--loop --poll 60`
does the same forever. There is no daemon and no background thread. Use cron,
Task Scheduler, or `--loop`, whichever you already trust.

A broken template does not wedge the schedule. The error is printed, the run is
not created, and `next_run` still advances.

## Transport and firmware API

`HttpTransport` talks to one board. The token goes in `X-PhonePilot-Token`.

| Method | Path | Body | Purpose |
|---|---|---|---|
| GET | `/api/v1/status` | | firmware, BLE, queue, approval |
| GET | `/api/v1/queue` | | all jobs |
| GET | `/api/v1/next` | | take the next undelivered job |
| POST | `/api/v1/result` | `job_id`, `status`, `detail` | report back |
| POST | `/api/v1/keys` | `text`, `enter`, `delay_ms` | BLE HID typing |
| POST | `/api/v1/shortcut` | `name` | queue a Shortcut job |
| POST | `/api/v1/post` | `platform`, `media`, `caption`, `target` | queue a post job |
| POST | `/api/v1/approve` | `seconds` | arm the approval window |
| POST | `/api/v1/panic` | | clear the queue |

The board has eight job slots, no persistence, and no TLS. A reboot clears the
queue. That is a feature for a device with a physical panic button, and a
limitation you should know about.

## LLM drafting

`phonepilot workflows draft` sends a system prompt and your request to
`POST {base}/chat/completions` with `response_format: json_object`. The response
is stripped of fences, parsed, forced through `parse_workflow`, and repaired
once if validation fails. The model cannot execute anything. The only output is
YAML on stdout and, if you ask, a file.

If the model returns `confirm_required: false`, the drafter sets it back to
true before validation. If it invents a step type or field, validation fails
and the draft is rejected rather than silently repaired.

## Testing

- `tests/test_templates.py` covers every validation rule and both policy rails.
- `tests/test_engine.py` covers order, confirmation, decline, failure, partial
  runs, interpolation, dry runs, and iPhone to board routing.
- `tests/test_transport.py` and the HTTP engine tests run against a real
  `ThreadingHTTPServer` on localhost that speaks the firmware API.
- `tests/test_cli.py` drives `main(argv)` end to end in a temporary home.
- `tests/test_llm.py` stubs the model but exercises the real HTTP client in one
  test.

What the tests cannot prove: that the sketch compiles for your board, that your
phone pairs, or that a platform accepts a post. Those are hardware and policy
questions, and the docs say so instead of pretending.

## Design choices

- **Only PyYAML at run time.** A control plane you cannot install in two years
  is not a control plane.
- **Strict schemas.** Unknown fields are errors. Silent typos in automation are
  how you publish the wrong thing.
- **SQLite over a service.** One person, one phone, one file. Back it up with
  `cp`.
- **Board queue over direct phone access.** iOS has no server. Polling a board
  is the smallest reliable bridge.
- **Confirmation in two places.** The terminal catches mistakes before they
  leave the computer; the phone menu catches mistakes before they leave the
  phone.
- **No vision model.** Deterministic steps are easier to audit, cheaper to run,
  and do not need a model key. Drafting is the one place a model helps.
