# Changelog

## 0.1.1, 2026-10-22

Firmware build fix. The Python control plane is unchanged apart from the
version string.

### Fixed

- The ESP32 sketch now compiles against `esp32:esp32` core 3.3.12. It previously
  failed in `BleKeyboard.cpp` with `std::string` to `String` conversion errors
  and a missing `BLECharacteristic::setValue(std::string)` overload.

### Changed

- Replaced T-vK/ESP32-BLE-Keyboard (unmaintained since August 2024) with
  HijelHID_BLEKeyboard 0.5.0, which builds on NimBLE-Arduino 2.5.1. The helper
  library is installed by the same `arduino-cli lib install` command.
- `/api/v1/keys` maps `delay_ms` onto the library key hold and inter key gap,
  clamped to 0 to 50 ms with a 15 ms floor, because iOS drops shorter taps.
- `/api/v1/status` also reports `ble_paired`, which is true only after the BLE
  link is encrypted and HID is ready. `ble_connected` still reports the radio
  link.
- Panic and Enter are sent with `tap(KEY_ESCAPE)` and `tap(KEY_RETURN)` instead
  of the removed `write(KEY_*)` path.

### Build size

`arduino-cli compile --fqbn esp32:esp32:esp32 firmware/phonepilot_esp32` on
core 3.3.12 reports 1,243,068 bytes of flash (94 percent of 1,310,720) and
60,556 bytes of static RAM (18 percent of 327,680).

### Documentation

- `firmware/phonepilot_esp32/README.md` lists the exact dependency versions,
  install commands, the `config.h` copy step, the board, and the measured build
  size.
- The top level README firmware section matches.

## 0.1.0, 2026-10-22

First public release: the Python control plane with a tested workflow engine,
template validator, device registry, scheduler, LLM drafting, three example
templates, and the ESP32 sketch and iOS setup docs.
