# wakepsx-on-bt

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)
[![HA Custom Component](https://img.shields.io/badge/Home%20Assistant-Custom%20Component-blue?style=for-the-badge&logo=homeassistant)](custom_components/wakepsx_on_bt/)
[![ESPHome](https://img.shields.io/badge/ESPHome-Firmware-blue?style=for-the-badge&logo=esphome)](esphome/)

Wake up your PlayStation (PS3, PS4, PS5) from Home Assistant over Bluetooth.

This repository contains two components that work together:

| Component | Description | Documentation |
|---|---|---|
| **HA Custom Component** | Home Assistant integration — button entity, config flow, wake logic | [custom_components/wakepsx_on_bt/](custom_components/wakepsx_on_bt/README.md) |
| **ESPHome Firmware** | ESP32 gateway for reliable Bluetooth wake (recommended method) | [esphome/](esphome/README.md) |

The integration relies on **[pywakepsx-on-bt](https://github.com/FreeTHX/pywakepsx-on-bt)** — a standalone Python library that handles HCI-level Bluetooth operations and MAC address extraction from USB controllers.

## Quick start

1. Copy `custom_components/wakepsx_on_bt/` into your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Add the integration via **Settings → Devices & Services → Add Integration → Wake PSX on Bluetooth**
4. Follow the config flow — connect your controller via USB when prompted

For the ESPHome method, flash the firmware first: see [esphome/README.md](esphome/README.md).

## License

MIT License
