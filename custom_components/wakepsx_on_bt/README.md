# wakepsx-on-bt

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)

Wake up your PlayStation (PS3, PS4, PS5) from Home Assistant over Bluetooth.

The integration impersonates your paired DualShock/DualSense controller using a low-level Bluetooth HCI wake packet. Two wake methods are supported:

| Method | Description | Recommended |
|---|---|---|
| **ESPHome** | A dedicated ESP32 handles Bluetooth and stays always available. No system-level constraints, works reliably in automations. | ✅ Strongly recommended |
| **Bluetooth Dongle** | A USB Bluetooth adapter plugged directly into your HA host. Requires raw HCI access and a dedicated adapter. | Fallback when no ESP32 is available |

---

## Prerequisites

### Both methods

- A **paired DualShock 3**, **DualShock 4**, or **DualSense** controller
- A **USB data cable** to connect the controller to your HA host during setup (one-time only)
- Home Assistant OS, Supervised, or Container with USB passthrough enabled

### ESPHome method

- An **ESP32 with Classic Bluetooth** — see [Compatible Hardware](../../esphome/README.md#compatible-hardware)
- The **ESPHome** add-on or standalone CLI to compile and flash the firmware

### Bluetooth Dongle method

- A USB Bluetooth adapter with a **compatible chipset**
- The adapter must be within Bluetooth range of the console (~10 metres)

→ See [pywakepsx-on-bt](https://github.com/FreeTHX/pywakepsx-on-bt) for the full chipset compatibility list and support ratings.

---

## Installation

### ESPHome method

1. Flash the ESP32 firmware — see [esphome/README.md](../../esphome/README.md).
2. Copy the `wakepsx_on_bt` folder into your HA `custom_components` directory.
3. Restart Home Assistant.

### Bluetooth Dongle method

1. Copy the `wakepsx_on_bt` folder into your HA `custom_components` directory.
2. Restart Home Assistant.

Home Assistant automatically installs all required dependencies on restart. The Bluetooth layer is powered by [pywakepsx-on-bt](https://github.com/FreeTHX/pywakepsx-on-bt).

---

## Bluetooth Dongle — Usage Recommendations

> **The ESPHome method is strongly recommended for automations and repeated use.** The Dongle method involves low-level OS operations that carry hardware-specific constraints described below.

The Dongle method sends the wake packet by temporarily spoofing the Bluetooth adapter's MAC address via a raw HCI socket. This works reliably in most setups, but involves constraints that the ESPHome method avoids entirely:

- **Use a dedicated adapter.** Do not use an adapter already managed by Home Assistant's native Bluetooth integration. Sharing an adapter between HA Bluetooth and this integration causes conflicts: HA's stack holds the device open, and BD_ADDR spoofing is incompatible with a running host stack.
- **A dedicated USB dongle is the cleanest solution.** A compatible USB Bluetooth adapter (~5€) plugged in alongside your existing setup avoids any conflict.
- The config flow automatically flags adapters already managed by HA (marked ⚠ Not recommended).
- **⚠ Experimental — concurrent access and recovery timing.** The integration serialises access to the Bluetooth adapter using an internal lock and reports "Busy" if a wake sequence is already in progress. After each sequence, a short recovery window is enforced to allow the adapter to reinitialise. This mechanism is functional but **experimental**: timing behaviour varies between chipsets and host systems. If you trigger the button in rapid succession, the second press may be silently dropped or return a "Busy" state. **Wait at least 3–5 seconds between attempts.** Using two console entries that share the same adapter increases the risk of conflict.
- **Some chipsets reset after a wake sequence.** On certain adapters (Broadcom, CSR, Intel, Cypress), the BD_ADDR restore triggers an HCI Reset, which may cause the USB device to briefly disconnect and re-enumerate. During this window the adapter is temporarily unavailable. The integration attempts to absorb this delay, but if the adapter takes longer than expected to recover, the next wake attempt will fail with a timeout error. Unplug and replug the dongle to recover immediately.
- **Raw HCI access is required.** The HA process must have `CAP_NET_RAW` / `CAP_NET_ADMIN` privileges. On HA OS and Supervised, this is granted automatically. On Container setups, ensure the container has the necessary permissions.

---

## Configuration

1. Plug your paired PlayStation controller into the USB port of your HA host. *(Controller must be off before plugging in.)*
2. Go to **Settings → Devices & Services → Add Integration**.
3. Search for **Wake PSX on Bluetooth**.
4. **Step 1:** The integration reads the MAC addresses from the USB controller automatically.
5. **Step 2:** Choose your wake method and console name.
6. **Step 3 (ESPHome):** Select your ESPHome node from the list.
   **Step 3 (Dongle):** Select your Bluetooth adapter from the list.
7. Unplug the controller. Done.

A **Button** entity is created for the console. The entity ID is derived from the console name — for example, `button.my_ps5_wake` for a console named "My PS5". Find the exact ID in **Developer Tools → States**.

---

## Entity Attributes

After each wake attempt the button exposes its result as attributes:

| Attribute | Example values |
|---|---|
| `last_wake_status_text` | `Ready`, `Success`, `Page Timeout`, `Task Error` |
| `last_wake_result` | `ready`, `success`, `timeout`, `error` |
| `last_wake_method` | `esphome_psx_waker`, `bluetooth_dongle` |

The status resets to `Ready` automatically after 5 seconds.

---

## Dashboard

### Option 1: Native HA cards (no extra dependencies)

```yaml
type: vertical-stack
cards:
  - type: button
    entity: button.YOUR_CONSOLE_wake
    name: Wake PlayStation
    icon: mdi:sony-playstation
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.YOUR_CONSOLE_wake
  - type: markdown
    content: >
      {{ state_attr('button.YOUR_CONSOLE_wake', 'last_wake_status_text') | default('Ready') }}
```

### Option 2: custom:button-card

Requires [custom:button-card](https://github.com/custom-cards/button-card) installed via HACS → Frontend.

```yaml
type: custom:button-card
entity: button.YOUR_CONSOLE_wake
name: Wake PlayStation
icon: mdi:sony-playstation
show_name: true
show_icon: true
show_label: true
label: >
  [[[
    return states['button.YOUR_CONSOLE_wake'].attributes.last_wake_status_text || 'Ready';
  ]]]
tap_action:
  action: perform-action
  perform_action: button.press
  target:
    entity_id: button.YOUR_CONSOLE_wake
styles:
  icon:
    - color: "#1e6eb5"
    - width: 70px
  name:
    - font-size: 14px
  label:
    - font-size: 12px
    - color: gray
```

---

## Troubleshooting

### ESPHome method

- **ESPHome node not found during setup:** Make sure the ESP32 is online and already discovered by the ESPHome integration in HA before running the setup.
- **Page Timeout:** The ESP32 is too far from the console, or the console's Bluetooth is asleep. Move the ESP32 closer or wake the console manually once.
- **Firmware compilation fails:** Confirm you are using an `esp32dev` board with the ESP-IDF framework (not Arduino). See [esphome/README.md](../../esphome/README.md).

### Bluetooth Dongle method

- **No compatible adapter detected:** Check that the adapter is plugged in and recognised by the OS. Run `hciconfig` to list available adapters.
- **Adapter flagged as ⚠ Not recommended:** The adapter is already managed by HA's Bluetooth integration. Use a dedicated dongle instead.
- **Page Timeout:** The adapter is too far from the console. Move it closer or use an extension cable to reposition it.
- **Detection error:** HA may not have the permissions to open a raw HCI socket. Check that the HA process has `CAP_NET_RAW` / `CAP_NET_ADMIN` or is running as root.

### Both methods

- **USB Extraction Failed / No Controller Found:** Use a data cable (not charge-only). Check that your HA host OS allows USB device access (udev rules on custom Linux hosts).

---

## License

MIT License — see the LICENSE file for details.
