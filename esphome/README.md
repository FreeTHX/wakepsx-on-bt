# esp-wakepsx-on-bt

This directory contains the ESPHome firmware for the ESP32 gateway used by the **Wake PSX on Bluetooth** Home Assistant integration. The ESP32 acts as a Bluetooth Classic bridge: when Home Assistant sends a wake command, the ESP32 impersonates your PlayStation controller over Bluetooth and wakes the console.

---

## Before You Start

### Compatible Hardware

**You need an ESP32 with Classic Bluetooth support.** Not all ESP32 variants support this.

| Module | Classic BT | Compatible |
|---|---|---|
| ESP32 (WROOM-32, WROVER) | ✅ | ✅ **Use this** |
| ESP32-S2 | ❌ | ❌ No Bluetooth at all |
| ESP32-S3 | BLE only | ❌ BLE only, no Classic BT |
| ESP32-C3 | BLE only | ❌ BLE only, no Classic BT |
| ESP32-C6 | BLE only | ❌ BLE only, no Classic BT |
| ESP32-H2 | BLE only | ❌ BLE only, no Classic BT |

> **In short: only the original ESP32 (WROOM-32 or WROVER) works.**
> When buying a board, look for "ESP32" without any suffix, or explicitly "Classic Bluetooth" in the specs.

### What You Also Need

- **Home Assistant** with the **Wake PSX on Bluetooth** integration installed
- **ESPHome** (add-on or standalone CLI) to compile and flash the firmware
- Your **PlayStation controller paired to the console at least once** (the HA integration extracts the MAC addresses via USB — see the integration README)
- The ESP32 placed **within Bluetooth range of your console** (typically 5–10 metres, line of sight preferred)

### Supported Consoles

The wake protocol is identical across all PlayStation generations:

| Console | Controller | Tested |
|---|---|---|
| PlayStation 3 | DualShock 3 | ✅ |
| PlayStation 4 | DualShock 4 | ✅ |
| PlayStation 5 | DualSense / DualSense Edge | ✅ |

---

## Installation

### 1. Adapt the YAML

The YAML uses an ESPHome [packages include](https://esphome.io/components/packages.html) to pull WiFi settings from a shared file:

```yaml
packages:
  wifi: !include ../../common/wifi.yaml
```

Either adapt this path to point to your own shared WiFi config, or replace the entire `packages:` block with an inline `wifi:` section following the [ESPHome WiFi documentation](https://esphome.io/components/wifi.html):

```yaml
wifi:
  ssid: "YourSSID"
  password: "YourPassword"
```

The node name (`esp-wakepsx-on-bt`) and friendly name (`Wake PSX on Bluetooth`) can also be changed in the `esphome:` section if you run multiple units.

### 2. Flash the ESP32

```bash
esphome run esp-wakepsx-on-bt.yaml
```

Or use the ESPHome dashboard in Home Assistant (add the file to your ESPHome config folder).

### 3. Add to Home Assistant

Once the ESP32 is online, Home Assistant will discover it automatically via the ESPHome integration. Add it, then configure the **Wake PSX on Bluetooth** integration and select this node as your wake gateway.

---

## How It Works (Simple Version)

1. You press the **Wake** button in Home Assistant.
2. HA calls the `wake_psx` service on the ESP32, passing two MAC addresses: the console's and the controller's.
3. The ESP32 **changes its Bluetooth address** to match your controller (spoofing).
4. The ESP32 sends a standard Bluetooth connection request to the console.
5. The console sees a known controller trying to connect and wakes up.
6. The ESP32 shuts down the Bluetooth controller and reports the result back to HA. The spoofed address is re-applied at the start of the next wake call.

The whole sequence takes 1–6 seconds depending on how quickly the console responds.

---

## Status Sensor

The firmware exposes a `PSX Wake Status` sensor in Home Assistant that shows the current state:

| Value | Meaning |
|---|---|
| `Ready` | Idle, waiting for a wake command |
| `Connecting...` | Wake sequence in progress |
| `Success` | Console responded and is waking up |
| `Page Timeout` | Console did not respond within 6 seconds (off, out of range, or already on) |
| `No Response` | No HCI event received — possible firmware issue |
| `Busy` | A wake command is already running |
| `Spoof Error` | Failed to change the Bluetooth address |
| `Init Error` / `Enable Error` | BT controller initialisation failed |
| `VHCI Error` | Virtual HCI interface error |
| `TX Unavailable` | Controller not ready to send — try again |
| `Invalid MAC` | Malformed MAC address received from HA |
| `Task Error` | FreeRTOS task could not be created (out of memory) |

The status resets to `Ready` 5 seconds after the result is displayed.

---

## Technical Details

### Firmware Architecture

The firmware disables the full Bluetooth host stack (Bluedroid and NimBLE) and uses the **Virtual HCI (VHCI)** interface directly. This frees RAM and gives direct access to the HCI transport layer without a protocol stack in the way.

```
ESPHome YAML lambda
        │
        ▼
wakepsx_on_bt::wake_psx()     ← API entry point, mutex guard
        │
        ▼
FreeRTOS task: wake_task()    ← runs on APP_CPU, stack 4096 words (16 KB)
        │
        ├─ esp_bt_controller_deinit()     BT controller reset to clean state
        ├─ esp_iface_mac_addr_set()       BD_ADDR spoof (must happen before init)
        ├─ esp_bt_controller_init()       Classic BT only (ESP_BT_MODE_CLASSIC_BT)
        ├─ esp_bt_controller_enable()
        ├─ esp_vhci_host_register_callback()
        ├─ esp_vhci_host_send_packet()    Raw HCI Create Connection injection
        └─ poll connection_status         Updated by notify_host_recv() callback
```

### HCI Packet

The wake packet is a raw **HCI Create Connection** command (opcode `0x0405`), identical to what the Python library sends on Linux:

```
01 05 04 0D             HCI Command, opcode 0x0405, 13 param bytes
XX XX XX XX XX XX       Target console MAC (little-endian)
18 CC                   Packet type: DM1|DH1|DM3|DH3|DM5|DH5 (0xCC18)
02                      Page Scan Repetition Mode R2
00                      Reserved
00 00                   Clock Offset (not valid, ignored)
01                      Allow Role Switch
```

Ref: Bluetooth Core Spec Vol 4 Part E §7.1.5.

### Concurrency

A FreeRTOS mutex prevents concurrent wake attempts. A second call while a wake is in progress returns `Busy` immediately. The mutex is released 5 seconds after the result is published (while the status is displayed), so rapid retries within that window are also blocked.

### Timing Constants

| Constant | Value | Reason |
|---|---|---|
| `BT_CONTROLLER_SETTLE_MS` | 100 ms | Hardware settling after controller deinit (empirical) |
| `VHCI_WARMUP_MS` | 200 ms | Controller internal init after enable — no "ready" event in VHCI mode |
| `VHCI_TX_POLL_MAX_CYCLES × INTERVAL` | 50 × 10 ms = 500 ms | Safety poll if warmup is not enough |
| `WAKE_TIMEOUT_CYCLES × INTERVAL` | 60 × 100 ms = 6 s | Page timeout margin (BT default = 5.12 s) |
| `WAKE_RESULT_DISPLAY_MS` | 5 s | UX display time before reset to Ready |

### sdkconfig Options

| Option | Value | Reason |
|---|---|---|
| `CONFIG_BT_ENABLED` | y | Enable BT subsystem |
| `CONFIG_BT_CLASSIC_ENABLED` | y | Enable Classic BT (BR/EDR) |
| `CONFIG_BT_CONTROLLER_ONLY` | y | No host stack — VHCI only |
| `CONFIG_BT_BLUEDROID_ENABLED` | n | Disabled — not needed, saves RAM |
| `CONFIG_BT_NIMBLE_ENABLED` | n | Disabled — not needed, saves RAM |
| `CONFIG_BT_HCI_MODE_VHCI` | y | Route HCI over Virtual HCI interface |
| `CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY` | y | Classic BT only — disables BLE hardware to save RAM |
