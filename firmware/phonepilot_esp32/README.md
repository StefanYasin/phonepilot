# PhonePilot firmware

Arduino sketch for a classic ESP32 dev board. It exposes three things:

- a Bluetooth LE HID keyboard named `PhonePilot`
- a physical button: short press arms a 90 second approval, long press clears the queue
- an HTTP control API on port 80, protected by `X-PhonePilot-Token`

## Install

```bash
arduino-cli core install esp32:esp32
arduino-cli lib install "ESP32-BLE-Keyboard" "ArduinoJson"
cp config.h.example config.h      # edit Wi-Fi, token and pins
arduino-cli compile --fqbn esp32:esp32:esp32 phonepilot_esp32
arduino-cli upload  --fqbn esp32:esp32:esp32 -p COM5 phonepilot_esp32
```

Use `esp32:esp32:esp32s3` for an S3 board if your BLE keyboard library build
supports it. The sketch itself is board agnostic.

## Wiring

| Part | Connection |
|---|---|
| Button | GPIO 0 to GND (internal pull up) |
| LED | GPIO 2 through a 220 ohm resistor to GND |
| Power | USB, from a charger or the iPhone through a camera adapter |

GPIO 0 is the BOOT button on most dev boards, so you can start with the board
button and add an external one later.

## HTTP API

All routes require `X-PhonePilot-Token` when `API_TOKEN` is set.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/status` | firmware, BLE state, queue length, approval state |
| GET | `/api/v1/queue` | queued jobs |
| GET | `/api/v1/next` | take the next job for the phone Shortcut |
| POST | `/api/v1/result` | report a job as posted, done or failed |
| POST | `/api/v1/keys` | type text over BLE HID |
| POST | `/api/v1/shortcut` | queue a named Shortcut for the phone |
| POST | `/api/v1/post` | queue a social post for the phone |
| POST | `/api/v1/approve` | arm the approval window |
| POST | `/api/v1/panic` | clear the queue and send Escape |

## Verification status

The Python control plane is covered by tests that speak this exact HTTP shape.
The sketch has not been compiled or flashed on physical hardware as part of
this release. Treat the first flash as a bring-up step and watch the serial
log at 115200 baud.

## Security notes

This board has no TLS and no user accounts. Keep it on a network you trust,
set a long `API_TOKEN`, and do not expose port 80 to the internet.
