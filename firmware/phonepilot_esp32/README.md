# PhonePilot firmware

Arduino sketch for an ESP32 dev board with Bluetooth LE. It exposes three things:

- a Bluetooth LE HID keyboard named `PhonePilot`
- a physical button: short press arms a 90 second approval, long press clears the queue
- an HTTP control API on port 80, protected by `X-PhonePilot-Token`

## Dependencies

| Dependency | Version tested | Role |
|---|---|---|
| `esp32:esp32` (Espressif Arduino core) | 3.3.12 | board support, Wi-Fi, `WebServer`, `ESPmDNS` |
| `HijelHID_BLEKeyboard` | 0.5.0 | BLE HID keyboard over NimBLE |
| `NimBLE-Arduino` | 2.5.1 | BLE stack, installed automatically as a dependency of the above |
| `ArduinoJson` | 7.4.2 | JSON request and response bodies |

The sketch used `ESP32-BLE-Keyboard` by T-vK up to v0.1.0. That library does not
build against ESP32 core 3.3.12: `BleKeyboard.cpp` passes `std::string` where the
core now expects `String`, and `BLECharacteristic::setValue(std::string)` no
longer has a matching overload. It has also had no upstream commits since
August 2024. v0.1.1 replaces it with `HijelHID_BLEKeyboard`, which is NimBLE
based, is in the Arduino library registry, and supports core 3.3.7 and newer.

## Install

```bash
arduino-cli core install esp32:esp32
arduino-cli lib install "HijelHID_BLEKeyboard@0.5.0" "ArduinoJson@7.4.2"
cp firmware/phonepilot_esp32/config.h.example firmware/phonepilot_esp32/config.h
# edit Wi-Fi, API_TOKEN, pins and device name in config.h, then:
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/phonepilot_esp32
arduino-cli upload  --fqbn esp32:esp32:esp32 -p COM5 firmware/phonepilot_esp32
```

The single `lib install` command also pulls `NimBLE-Arduino` 2.5.1, because
`HijelHID_BLEKeyboard` declares it as a dependency. Pinning the versions keeps
the build reproducible; drop the `@version` suffixes to track latest.

`config.h` is gitignored, so your Wi-Fi password and API token stay local. It
must exist before compiling, since the sketch includes it directly.

### Board

`esp32:esp32:esp32` is the classic ESP32 with Bluetooth, and it is the only
target built for this release. The library also supports the ESP32 variants
that have a radio (S3, C3, C6); the ESP32-S2 and ESP32-P4 have no Bluetooth and
cannot run the sketch.

### Build size

Measured with core 3.3.12 on the default partition scheme:

```
Sketch uses 1243068 bytes (94%) of program storage space. Maximum is 1310720 bytes.
Global variables use 60556 bytes (18%) of dynamic memory, leaving 267124 bytes for local variables. Maximum is 327680 bytes.
```

The default app partition is nearly full. If you add features, compile with
`--board-options PartitionScheme=huge_app`, the 3 MB app layout with no OTA and
1 MB SPIFFS. That build reports 1,243,084 bytes (39%) of flash. The sketch has
no OTA path, so nothing is lost by switching.

## First pairing

1. Flash the board and open the serial monitor at 115200 baud.
2. On the iPhone: Settings, Bluetooth, then tap `PhonePilot`.
3. Pairing is Just Works, so there is no code to type. The library advertises
   as a HID keyboard with a battery level of 100 percent.
4. The serial log prints `waiting for the iPhone to connect over BLE` until the
   link is up. `/api/v1/status` reports both `ble_connected` (radio link) and
   `ble_paired` (encryption and HID ready).

To re-pair, remove the device on the iPhone and reset the board. The library
also exposes `clearBonds()` if you want a sketch level reset button later.

## Wiring

| Part | Connection |
|---|---|
| Button | GPIO 0 to GND (internal pull up) |
| LED | GPIO 2 through a 220 ohm resistor to GND |
| Power | USB, from a charger or the iPhone through a camera adapter |

GPIO 0 is the BOOT button on most dev boards, so you can start with the board
button and add an external one later. Holding it at reset still enters the
bootloader, which is how you flash.

## HTTP API

All routes require `X-PhonePilot-Token` when `API_TOKEN` is set.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | small status page |
| GET | `/api/v1/status` | firmware, BLE state, queue length, approval state |
| GET | `/api/v1/queue` | queued jobs |
| GET | `/api/v1/next` | take the next job; shortcut jobs are one shot and removed when taken |
| POST | `/api/v1/result` | report a job as posted, done or failed |
| POST | `/api/v1/keys` | type text over BLE HID |
| POST | `/api/v1/shortcut` | queue a named Shortcut for the phone |
| POST | `/api/v1/post` | queue a social post for the phone |
| POST | `/api/v1/approve` | arm the approval window |
| POST | `/api/v1/panic` | clear the queue and send Escape |

`/api/v1/keys` takes `text` (up to 512 ASCII characters), `enter` and
`delay_ms`. `delay_ms` is the key hold and the gap between keys, clamped to
0 to 50 ms with a 15 ms floor, because iOS drops taps shorter than that. The
call blocks while it types, so keep `text` short when the phone is polling
the same board.

## Verification status

The sketch compiles clean for `esp32:esp32:esp32` with core 3.3.12, the
HijelHID 0.5.0 and NimBLE-Arduino 2.5.1 combination, and the linker reports the
flash and RAM numbers above. It has not been flashed on physical hardware or
paired with a real iPhone as part of this release, so treat the first flash as
a bring-up step and watch the serial log at 115200 baud.

The Python control plane tests speak this exact HTTP shape against a mock
board, so the route table and the JSON field names are pinned by tests even
though the radio side is unproven.

## Security notes

This board has no TLS and no user accounts. Keep it on a network you trust,
set a long `API_TOKEN`, and do not expose port 80 to the internet. Anyone who
can pair with the board can type into the phone it is attached to, which is the
same trust you extend to a Bluetooth keyboard.
