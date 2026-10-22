# PhonePilot

An ESP32 board, an iPhone, and workflow files you can read before they run.

PhonePilot is a local control plane for iPhone automation. The board is a
Bluetooth LE keyboard, a physical approval button, and a small HTTP bridge. iOS
Shortcuts do the app level work. A Python CLI keeps a SQLite device registry,
validates YAML workflow templates, executes them, and runs them on a schedule.

The point is not to fake a human. The point is to make repetitive phone work
repeatable: open the right app, type the same text, run the same Shortcut, hand
a queued photo to a composer, and stop for a yes before anything is published.

## Status

This is v0.1.0, a working vertical slice:

- Python control plane with a tested workflow engine, template validator,
  device registry, autorun scheduler, and LLM drafting.
- 102 tests, including end to end runs against a local HTTP server that speaks
  the firmware API.
- ESP32 sketch for a classic ESP32 with BLE HID, a button, and the same HTTP
  API. The sketch has not been compiled or flashed on physical hardware in this
  release. Treat the first flash as bring-up.
- Three example templates, including a confirmed Instagram photo queue.

What is not here yet: a packaged `.shortcut` file, direct platform APIs, image
understanding, a GUI, and hardware validation. See [Deferred](#deferred).

## How it fits together

```
+-------------------+        HTTP + token        +----------------------+
|  fleet manager    |  ----------------------->  |  ESP32               |
|  Python CLI       |                            |  BLE HID keyboard    |
|  SQLite registry  |  <-----------------------  |  button + LED        |
|  workflow engine  |        JSON status         |  job queue           |
+-------------------+                            +----------+-----------+
                                                            |
                                                   BLE HID  |  queue poll
                                                            v
                                                 +----------------------+
                                                 |  iPhone Shortcuts    |
                                                 |  open app, deep link |
                                                 |  type, confirm, post |
                                                 +----------------------+
```

The fleet manager never talks to the iPhone directly. An iPhone is registered
with a `--bridge` pointing at its board, because the board is the only thing
with an IP address and a queue. The phone Shortcut polls the board, performs
the action, and reports the result back.

Why this shape:

- iOS gives no general automation API. Shortcuts is the supported surface.
- A BLE keyboard can type into any app, including apps with no API.
- A physical button is a better confirmation than a dialog you can tap without
  looking.
- A workflow file is reviewable. A binary automation is not.

## Hardware

| Part | Notes |
|---|---|
| ESP32 dev board | Classic ESP32 with BLE. ESP32-S3 works if your BLE keyboard library build supports it. |
| Momentary button | GPIO 0 to GND. The BOOT button works for a first test. |
| LED | GPIO 2 with a 220 ohm resistor. Optional. |
| iPhone | iOS 16 or later. iOS 17 or later recommended for Shortcuts features. |
| USB power | A charger or a camera adapter. The board does not need a computer at run time. |

## Install

Python 3.10 or later.

```bash
git clone https://github.com/StefanYasin/phonepilot
cd phonepilot
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS and Linux
pip install -e ".[dev]"
phonepilot init
```

Flash the board:

```bash
arduino-cli core install esp32:esp32
arduino-cli lib install "ESP32-BLE-Keyboard" "ArduinoJson"
cp firmware/phonepilot_esp32/config.h.example firmware/phonepilot_esp32/config.h
# edit Wi-Fi, API_TOKEN and pins, then:
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/phonepilot_esp32
arduino-cli upload  --fqbn esp32:esp32:esp32 -p COM5 firmware/phonepilot_esp32
```

Then follow [docs/ios-setup.md](docs/ios-setup.md) to pair the board and build
the polling Shortcut. That document is the other half of the install.

## Quick start

Register the board and the phone:

```bash
phonepilot fleet add board-01 --kind esp32 --host 192.168.1.50 --token "$PHONEPILOT_TOKEN"
phonepilot fleet add iphone-01 --kind iphone --bridge board-01
phonepilot fleet status
```

Try the flagship template without sending anything:

```bash
phonepilot workflows run photo_post_queue --device iphone-01 --dry-run
```

A dry run still asks for confirmation, because the confirmation is the point.
Use `--yes` only when you are deliberately rehearsing unattended.

Validate every template, including that the media files exist:

```bash
phonepilot workflows validate --media
```

Run for real, and watch every step:

```bash
phonepilot workflows run photo_post_queue --device iphone-01
```

Schedule it:

```bash
phonepilot schedule add photo_post_queue --device iphone-01 --every 6h
phonepilot autorun --once
```

`autorun` refuses social posts unless you pass `--yes`, because an unattended
process has nobody to ask. Read [docs/safety.md](docs/safety.md) before you use
that flag.

Draft a template from a sentence, with any OpenAI compatible endpoint:

```bash
export PHONEPILOT_LLM_API_KEY=...
export PHONEPILOT_LLM_BASE_URL=https://api.openai.com/v1   # optional
phonepilot workflows draft "post the newest photo in my queue to Instagram with a short caption" \
  --device iphone-01 --out drafts/photo.yaml
```

The draft is validated by the same parser as a hand written template. Read it
before running it.

## Templates

Three ship in `phonepilot/templates/` and are copied to `~/.phonepilot/templates`
by `phonepilot init`.

| Template | What it does |
|---|---|
| `daily_briefing` | Runs a briefing Shortcut, types a marker, posts a run record to a local endpoint. |
| `photo_post_queue` | Picks the next queued photo, prepares an Instagram post, requires a confirmation. |
| `launch_day_blast` | Announces a launch on X and LinkedIn, with one confirmation per post. |

A template is data:

```yaml
name: photo_post_queue
version: 1
description: Post the next queued photo to Instagram.
device: iphone-01
steps:
  - id: pick_photo
    type: trigger_shortcut
    shortcut: PhonePilot Pick Next Photo
    wait_after: 2
  - id: post_photo
    type: social_post
    platform: instagram
    media: media/queue/sample.png
    caption: New drop is live.
    mode: assisted
    confirm_required: true
```

Five step types: `trigger_shortcut`, `send_keys`, `wait`, `api_call`, and
`social_post`. Unknown fields are errors, not warnings, so a typo cannot quietly
turn a confirmation off. Full schema in
[docs/architecture.md](docs/architecture.md#template-schema).

## The confirmation rule

`social_post` always requires `confirm_required: true`. The validator rejects
anything else. At run time the CLI asks before the command reaches the board,
and the phone Shortcut asks again before it opens a composer. A scheduled run
declines by default because there is no terminal to answer.

This is deliberate friction. Posting is the one step where a mistake is public.

## Honesty rails

- Platform terms of service differ and change. Read them for the accounts you
  automate. PhonePilot does not interpret them for you.
- No fake engagement. The validator rejects `auto_like`, `auto_follow`,
  `auto_comment`, `auto_dm`, and similar keys.
- No rate limit evasion. Retrying on HTTP 403 or 429 is rejected at load time,
  and there is no proxy rotation or device spoofing.
- No private iOS APIs. Shortcuts, BLE HID, and the app's own UI only.
- Secrets stay local. Tokens live in `config.h`, the registry, or environment
  variables. Nothing is sent anywhere except the model endpoint you configure.

## Testing

```bash
python -m pytest -q
python -m ruff check phonepilot tests
```

102 tests cover template validation and the policy rails, the engine and
confirmation path, SQLite storage, scheduling, transport against a real local
HTTP server, the CLI, and LLM draft validation.

## Layout

```
phonepilot/            Python package
  cli.py               fleet, workflows, schedule, autorun, runs
  engine.py            step execution, interpolation, confirmations
  templates.py         strict YAML schema and validation
  policy.py            engagement and rate limit rails
  db.py                SQLite registry, runs, schedules
  scheduler.py         interval and daily autorun
  llm.py               natural language to workflow draft
  transport.py         HTTP board client, mock, dry run
  templates/           the three examples and sample media
firmware/              Arduino sketch and wiring notes
docs/                  iOS setup, architecture, safety
tests/                 101 pytest tests
```

## Deferred

- Flashing and running the sketch on a physical board. The API is tested, the
  hardware is not.
- A ready made `.shortcut` file. The setup document describes the Shortcut,
  building it takes about ten minutes.
- Direct posting through platform APIs for business accounts.
- Image and screen understanding. This release is deterministic by design.
- A web UI. The CLI and SQLite file are enough for one person and one phone.

## Credits

PhonePilot is inspired by
[onehumanbeing/esp32-iphone-agent](https://github.com/onehumanbeing/esp32-iphone-agent),
an MIT licensed project by Henry Yuan that puts a vision model in the loop and
mirrors the iPhone screen to the board. I read its README and LICENSE before
starting. The license permits adaptation, but this is an independent
implementation and no source code was copied. The approach here is different:
no AirPlay, no vision model, Shortcuts for app actions, and a Python control
plane built around reviewable templates.

The firmware uses [ESP32-BLE-Keyboard](https://github.com/T-vK/ESP32-BLE-Keyboard),
[ArduinoJson](https://arduinojson.org/), and the
[Arduino ESP32 core](https://github.com/espressif/arduino-esp32).

MIT licensed. See [LICENSE](LICENSE).

Built by Stefan Yasin at https://stefanyasin.github.io/
