"""Config flow for Wake PSX on Bluetooth integration."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import (
    CONF_BT_ADAPTER,
    CONF_BT_STRATEGY,
    CONF_CONSOLE_NAME,
    CONF_CONTROLLER_TYPE,
    CONF_CSR_WRITE_MODE,
    CONF_DSX_MAC,
    CONF_ESP_ENTITY,
    CONF_ESP_SERVICE,
    CONF_PSX_MAC,
    CONF_WAKE_METHOD,
    CONSOLE_NAME_PS3,
    CONSOLE_NAME_PS4,
    CONSOLE_NAME_PS5,
    CSR_WRITE_MODE_PERSISTENT,
    CSR_WRITE_MODE_TRANSIENT,
    DEFAULT_BT_ADAPTER,
    DEFAULT_CONSOLE_NAME,
    DEFAULT_CONTROLLER_NAME,
    DEFAULT_CSR_WRITE_MODE,
    DOMAIN,
    ESPHOME_DOMAIN,
    ESPHOME_PROJECT_NAME,
    ESPHOME_WAKE_SERVICE,
    STATUS_SENSOR_SUFFIX,
    WAKE_METHOD_DONGLE,
    WAKE_METHOD_ESPHOME,
)
from pywakepsx_on_bt.const import (
    CTRL_DUALSENSE,
    CTRL_DUALSENSE_EDGE,
    CTRL_DUALSHOCK3,
    CTRL_DUALSHOCK4,
    KEY_CTRL_TYPE,
    KEY_DSX_MAC,
    KEY_PSX_MAC,
)
from pywakepsx_on_bt.strategies import detect_strategy, list_compatible_adapters
from pywakepsx_on_bt.strategies.csr import CsrSpoofStrategy
from pywakepsx_on_bt.usb import extract_psx_bt_macs

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)
_ADAPTER_PATTERN = re.compile(r"^hci\d+$")

CONTROLLER_TO_CONSOLE: dict[str, str] = {
    CTRL_DUALSHOCK3: CONSOLE_NAME_PS3,
    CTRL_DUALSHOCK4: CONSOLE_NAME_PS4,
    CTRL_DUALSENSE: CONSOLE_NAME_PS5,
    CTRL_DUALSENSE_EDGE: CONSOLE_NAME_PS5,
}

_CONTROLLER_DISPLAY_NAME: dict[str, str] = {
    CTRL_DUALSHOCK3: "DualShock 3",
    CTRL_DUALSHOCK4: "DualShock 4",
    CTRL_DUALSENSE: "DualSense",
    CTRL_DUALSENSE_EDGE: "DualSense Edge",
}


# ---------------------------------------------------------------------------
# Module-level helpers — shared by ConfigFlow and OptionsFlow
# ---------------------------------------------------------------------------


def _is_valid_adapter(adapter: str) -> bool:
    """Return True if adapter matches the hciX format."""
    return bool(_ADAPTER_PATTERN.fullmatch(adapter.strip()))


def _get_selectable_esphome_entities(hass: HomeAssistant) -> dict[str, str]:
    """Return ESPHome status sensors eligible for selection, keyed by entity_id."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    selectable: dict[str, str] = {}

    for entity in ent_reg.entities.values():
        if entity.domain != Platform.SENSOR:
            continue
        if not entity.entity_id.endswith(STATUS_SENSOR_SUFFIX):
            continue
        device = dev_reg.async_get(entity.device_id) if entity.device_id else None
        if not device:
            continue
        is_psx = device.model == ESPHOME_PROJECT_NAME
        if is_psx:
            label = entity.original_name or entity.entity_id
            device_label = device.name_by_user or device.name or entity.entity_id
            selectable[entity.entity_id] = f"{device_label} ({label})"

    # Fallback: any status sensor when no PSX-specific device is found
    if not selectable:
        selectable = {
            e.entity_id: e.entity_id
            for e in ent_reg.entities.values()
            if e.entity_id.endswith(STATUS_SENSOR_SUFFIX)
        }

    return selectable


def _resolve_esphome_service(hass: HomeAssistant, entity_id: str) -> str:
    """Return the ESPHome wake service name for the given status sensor entity.

    Looks up the ESPHome config entry from the entity's device to get the
    authoritative node name. Falls back to suffix-stripping from entity_id.
    """
    ent_reg = er.async_get(hass)
    entity_entry = ent_reg.async_get(entity_id)
    if entity_entry and entity_entry.device_id:
        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get(entity_entry.device_id)
        if device:
            for entry_id in device.config_entries:
                cfg = hass.config_entries.async_get_entry(entry_id)
                if cfg and cfg.domain == ESPHOME_DOMAIN:
                    node_name = (
                        (cfg.data.get("device_name") or cfg.title)
                        .lower()
                        .replace(" ", "_")
                        .replace("-", "_")
                    )
                    return f"{node_name}_{ESPHOME_WAKE_SERVICE}"
    # Fallback: derive from entity_id
    entity_name = entity_id.split(".", 1)[-1]
    node_name = entity_name.removesuffix(f"_{STATUS_SENSOR_SUFFIX}")
    return f"{node_name}_{ESPHOME_WAKE_SERVICE}"


def _scan_adapters() -> tuple[
    list[str], dict[str, str], dict[str, int], dict[str, str]
]:
    """Scan HCI adapters and return (all_names, compatible→label, name→rating, mac→hci).

    MAC addresses come from AdapterInfo.mac_address (HCI_Read_BD_ADDR) so this
    works inside Docker where sysfs address files may be absent.
    Runs in an executor thread — do not call from the event loop directly.
    """
    all_names: list[str] = []
    compatible: dict[str, str] = {}
    ratings: dict[str, int] = {}
    mac_to_hci: dict[str, str] = {}

    try:
        for adapter in list_compatible_adapters():
            all_names.append(adapter.adapter)
            if adapter.compatible:
                compatible[adapter.adapter] = adapter.display_label
                ratings[adapter.adapter] = adapter.support_rating
            if adapter.mac_address:
                mac_to_hci[adapter.mac_address] = adapter.adapter
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("HCI adapter scan failed: %s", err)

    return all_names, compatible, ratings, mac_to_hci


def _build_controller_options(
    controllers: list[dict[str, str]],
    configured_macs: set[str] | None = None,
) -> dict[str, str]:
    """Build {psx_mac: display_label} for the select_controller step.

    Labels: "DualSense / PlayStation 5". When the same controller type appears
    more than once, "(1)", "(2)"... suffixes disambiguate the entries.
    Already-configured controllers are marked with a warning suffix.
    """
    type_counts: dict[str, int] = {}
    for ctrl in controllers:
        ctrl_type = ctrl.get(KEY_CTRL_TYPE, "")
        type_counts[ctrl_type] = type_counts.get(ctrl_type, 0) + 1

    type_seen: dict[str, int] = {}
    options: dict[str, str] = {}
    for ctrl in controllers:
        ctrl_type = ctrl.get(KEY_CTRL_TYPE, "")
        psx_mac = ctrl.get(KEY_PSX_MAC, "")
        display_name = _CONTROLLER_DISPLAY_NAME.get(ctrl_type, ctrl_type)
        console = CONTROLLER_TO_CONSOLE.get(ctrl_type, "PlayStation")
        label = f"{display_name} / {console}"
        if type_counts.get(ctrl_type, 1) > 1:
            type_seen[ctrl_type] = type_seen.get(ctrl_type, 0) + 1
            label = f"{label} ({type_seen[ctrl_type]})"
        if configured_macs and psx_mac.lower() in configured_macs:
            label = f"{label}  ⚠ (already configured)"
        options[psx_mac] = label

    return options


def _get_ha_managed_adapters(
    hass: HomeAssistant, mac_to_hci: dict[str, str]
) -> set[str]:
    """Return adapters owned by HA's native bluetooth integration.

    ``mac_to_hci`` maps BD_ADDR (uppercase, colon-separated) to hciX names;
    it is built by :func:`_scan_adapters` via HCI socket so it works inside Docker.

    HA bluetooth config entries store the adapter MAC in ``entry.unique_id``
    (newer HA) or ``entry.data["adapter"]`` (older HA, hciX format).
    """
    managed: set[str] = set()

    for entry in hass.config_entries.async_entries("bluetooth"):
        # Older HA: adapter stored as hciX in entry.data
        adapter = str(entry.data.get("adapter", "")).strip()
        if _is_valid_adapter(adapter):
            managed.add(adapter.lower())
        elif adapter:
            hci = mac_to_hci.get(adapter.upper())
            if hci:
                managed.add(hci)
        # Newer HA: unique_id is the BD_ADDR (e.g. "B8:27:EB:D4:C3:B7")
        if entry.unique_id:
            hci = mac_to_hci.get(str(entry.unique_id).upper())
            if hci:
                managed.add(hci)

    if managed:
        _LOGGER.debug("HA-managed BT adapters: %s", managed)
    else:
        _LOGGER.debug("No HA-managed BT adapters found")
    return managed


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class PSXWakeBTConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Wake PSX on Bluetooth."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise inter-step state for the multi-step config flow.

        Attributes are populated progressively across steps and consumed by
        ``_finalize_config_entry`` at the end of the flow.

        ``_reconfigure_entry`` is set when the user selects an already-configured
        controller: the flow updates the existing entry instead of creating a new one.
        ``_step2_*`` attributes cache the pre-scan results so the submit handler
        can validate user input without re-running the HCI scan.
        """
        self._psx_mac: str | None = None
        self._dsx_mac: str | None = None
        self._controller_type: str = DEFAULT_CONTROLLER_NAME
        self._console_name: str = DEFAULT_CONSOLE_NAME
        self._pending_bt_adapter: str = ""
        self._pending_bt_strategy: str = ""
        self._detected_controllers: list[dict[str, str]] = []
        self._reconfigure_entry: config_entries.ConfigEntry | None = None
        # Cached from step 2 pre-scan (used for submit validation)
        self._step2_esp_entities: dict[str, str] = {}
        self._step2_adapter_names: list[str] = []

    async def async_step_user(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: Extract MAC addresses from the USB-connected controller."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                controllers = await self.hass.async_add_executor_job(
                    extract_psx_bt_macs
                )
                if not controllers:
                    _LOGGER.warning("No compatible PlayStation controller found on USB")
                    errors["base"] = "no_controller"
                elif len(controllers) == 1:
                    ctrl = controllers[0]
                    self._psx_mac = ctrl[KEY_PSX_MAC]
                    self._dsx_mac = ctrl[KEY_DSX_MAC]
                    self._controller_type = ctrl.get(
                        KEY_CTRL_TYPE, DEFAULT_CONTROLLER_NAME
                    )
                    self._console_name = CONTROLLER_TO_CONSOLE.get(
                        self._controller_type, DEFAULT_CONSOLE_NAME
                    )
                    existing = next(
                        (
                            e
                            for e in self.hass.config_entries.async_entries(DOMAIN)
                            if e.unique_id == self._psx_mac.lower()
                        ),
                        None,
                    )
                    if existing is not None:
                        self._reconfigure_entry = existing
                        self._console_name = existing.data.get(
                            CONF_CONSOLE_NAME, self._console_name
                        )
                    else:
                        await self.async_set_unique_id(self._psx_mac.lower())
                        self._abort_if_unique_id_configured()
                    return await self.async_step_select_wake_method()
                else:
                    self._detected_controllers = controllers
                    return await self.async_step_select_controller()
            except AbortFlow:
                raise
            except Exception:
                _LOGGER.exception("USB extraction failed")
                errors["base"] = "extraction_error"

        status = {
            "no_controller": "⚠ No compatible PlayStation controller detected on USB.",
            "extraction_error": "⚠ Failed to read the USB device. Check system logs.",
        }.get(errors.get("base", ""), "")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"status": status},
        )

    async def async_step_select_controller(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1b (multi-controller only): Select which controller to configure."""
        configured_macs = {
            e.unique_id
            for e in self.hass.config_entries.async_entries(DOMAIN)
            if e.unique_id
        }

        if user_input is not None:
            selected_mac = user_input.get("selected_controller", "")
            ctrl = next(
                (
                    c
                    for c in self._detected_controllers
                    if c.get(KEY_PSX_MAC) == selected_mac
                ),
                None,
            )
            if ctrl is None:
                return self.async_abort(reason="extraction_error")
            self._psx_mac = ctrl[KEY_PSX_MAC]
            self._dsx_mac = ctrl[KEY_DSX_MAC]
            self._controller_type = ctrl.get(KEY_CTRL_TYPE, DEFAULT_CONTROLLER_NAME)
            self._console_name = CONTROLLER_TO_CONSOLE.get(
                self._controller_type, DEFAULT_CONSOLE_NAME
            )
            if self._psx_mac.lower() in configured_macs:
                self._reconfigure_entry = next(
                    e
                    for e in self.hass.config_entries.async_entries(DOMAIN)
                    if e.unique_id == self._psx_mac.lower()
                )
                self._console_name = self._reconfigure_entry.data.get(
                    CONF_CONSOLE_NAME, self._console_name
                )
            else:
                await self.async_set_unique_id(self._psx_mac.lower())
                self._abort_if_unique_id_configured()
            return await self.async_step_select_wake_method()

        options = _build_controller_options(self._detected_controllers, configured_macs)
        schema = vol.Schema(
            {
                vol.Required("selected_controller"): vol.In(options),
            }
        )
        return self.async_show_form(
            step_id="select_controller",
            data_schema=schema,
            description_placeholders={"count": str(len(self._detected_controllers))},
        )

    async def async_step_select_wake_method(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 2: Choose console name and wake method.

        Pre-scans ESPHome entities and Bluetooth adapters so the user sees
        availability before choosing. Both methods require at least one device.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            self._console_name = user_input.get(CONF_CONSOLE_NAME, self._console_name)
            wake_method = user_input.get(CONF_WAKE_METHOD, WAKE_METHOD_ESPHOME)
            if wake_method == WAKE_METHOD_ESPHOME:
                if not self._step2_esp_entities:
                    errors["base"] = "no_esphome_found"
                else:
                    return await self.async_step_select_esp()
            elif wake_method == WAKE_METHOD_DONGLE:
                if not self._step2_adapter_names:
                    errors["base"] = "no_adapter_found"
                else:
                    return await self.async_step_select_dongle()
            else:
                errors["base"] = "invalid_wake_method"

        # (Re-)scan on first display and after each validation error
        self._step2_esp_entities = _get_selectable_esphome_entities(self.hass)
        (
            self._step2_adapter_names,
            step2_compatible,
            _,
            __,
        ) = await self.hass.async_add_executor_job(_scan_adapters)
        available_adapters = self._step2_adapter_names
        esp_count = len(self._step2_esp_entities)
        compatible_count = len(step2_compatible)

        esp_summary = (
            f"{esp_count} compatible node(s)" if esp_count > 0 else "none found ⚠"
        )
        if not available_adapters:
            adapter_summary = "none detected ⚠"
        elif compatible_count > 0:
            adapter_summary = f"{compatible_count} compatible adapter(s)"
        else:
            adapter_summary = (
                f"{len(available_adapters)} adapter(s), none fully compatible ⚠"
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_CONSOLE_NAME, default=self._console_name): str,
                vol.Required(
                    CONF_WAKE_METHOD, default=WAKE_METHOD_ESPHOME
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[WAKE_METHOD_ESPHOME, WAKE_METHOD_DONGLE],
                        translation_key="wake_method",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="select_wake_method",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "esp_summary": esp_summary,
                "adapter_summary": adapter_summary,
            },
        )

    async def async_step_select_esp(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 3a: Select the ESPHome entity to use."""
        if user_input is not None:
            if self._psx_mac is None or self._dsx_mac is None:
                return self.async_abort(reason="extraction_error")
            entity_id: str = user_input.get(CONF_ESP_ENTITY, "")
            entry_data: dict[str, Any] = {
                CONF_PSX_MAC: self._psx_mac,
                CONF_DSX_MAC: self._dsx_mac,
                CONF_CONTROLLER_TYPE: self._controller_type,
                CONF_CONSOLE_NAME: self._console_name,
                CONF_WAKE_METHOD: WAKE_METHOD_ESPHOME,
                CONF_ESP_ENTITY: entity_id,
                CONF_BT_ADAPTER: "",
            }
            if entity_id:
                entry_data[CONF_ESP_SERVICE] = _resolve_esphome_service(
                    self.hass, entity_id
                )
            return self._finalize_config_entry(entry_data)

        selectable = _get_selectable_esphome_entities(self.hass)
        if selectable:
            schema = vol.Schema(
                {
                    vol.Required(CONF_ESP_ENTITY): vol.In(selectable),
                }
            )
            step_id = "select_esp"
        else:
            schema = vol.Schema(
                {
                    vol.Optional(CONF_ESP_ENTITY, default=""): str,
                }
            )
            step_id = "select_esp_warning"

        return self.async_show_form(step_id=step_id, data_schema=schema)

    async def async_step_select_dongle(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 3b: Select the Bluetooth dongle adapter."""
        errors: dict[str, str] = {}

        (
            all_names,
            compatible,
            ratings,
            mac_to_hci,
        ) = await self.hass.async_add_executor_job(_scan_adapters)
        ha_managed = _get_ha_managed_adapters(self.hass, mac_to_hci)
        available_names = all_names

        clean = dict(
            sorted(
                ((k, v) for k, v in compatible.items() if k not in ha_managed),
                key=lambda x: (-ratings.get(x[0], 0), int(x[0].replace("hci", ""))),
            )
        )
        warned = dict(
            sorted(
                (
                    (k, f"⚠ {compatible[k]}  (HA Bluetooth adapter — Not recommended)")
                    for k in compatible
                    if k in ha_managed
                ),
                key=lambda x: (-ratings.get(x[0], 0), int(x[0].replace("hci", ""))),
            )
        )
        available_compatible = {**clean, **warned}

        if user_input is not None:
            bt_adapter = user_input.get(CONF_BT_ADAPTER, DEFAULT_BT_ADAPTER).strip()
            if not _is_valid_adapter(bt_adapter):
                errors["base"] = "invalid_adapter"
            elif available_names and bt_adapter not in available_names:
                errors["base"] = "adapter_not_found"
            elif self._psx_mac is None or self._dsx_mac is None:
                return self.async_abort(reason="extraction_error")
            else:
                self._pending_bt_adapter = bt_adapter
                adapter_index = int(bt_adapter.replace("hci", ""))
                try:
                    strategy = await self.hass.async_add_executor_job(
                        detect_strategy, adapter_index
                    )
                except Exception:
                    _LOGGER.exception("Strategy detection failed for %s", bt_adapter)
                    errors["base"] = "detection_error"
                else:
                    self._pending_bt_strategy = type(strategy).__name__
                    if isinstance(strategy, CsrSpoofStrategy):
                        return await self.async_step_select_csr_write_mode()
                    return self._finalize_config_entry(
                        {
                            CONF_PSX_MAC: self._psx_mac,
                            CONF_DSX_MAC: self._dsx_mac,
                            CONF_CONTROLLER_TYPE: self._controller_type,
                            CONF_CONSOLE_NAME: self._console_name,
                            CONF_WAKE_METHOD: WAKE_METHOD_DONGLE,
                            CONF_BT_ADAPTER: bt_adapter,
                            CONF_BT_STRATEGY: self._pending_bt_strategy,
                            CONF_ESP_ENTITY: "",
                        }
                    )

        if available_compatible:
            default = next(iter(clean)) if clean else next(iter(available_compatible))
            schema = vol.Schema(
                {
                    vol.Required(CONF_BT_ADAPTER, default=default): vol.In(
                        available_compatible
                    ),
                }
            )
            step_id = "select_dongle"
        else:
            schema = vol.Schema(
                {
                    vol.Required(CONF_BT_ADAPTER, default=DEFAULT_BT_ADAPTER): str,
                }
            )
            step_id = "select_dongle_warning"

        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def async_step_select_csr_write_mode(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 4 (CSR only): Choose transient or persistent BD_ADDR write mode."""
        if user_input is not None:
            return self._finalize_config_entry(
                {
                    CONF_PSX_MAC: self._psx_mac,
                    CONF_DSX_MAC: self._dsx_mac,
                    CONF_CONTROLLER_TYPE: self._controller_type,
                    CONF_CONSOLE_NAME: self._console_name,
                    CONF_WAKE_METHOD: WAKE_METHOD_DONGLE,
                    CONF_BT_ADAPTER: self._pending_bt_adapter,
                    CONF_BT_STRATEGY: self._pending_bt_strategy,
                    CONF_ESP_ENTITY: "",
                    CONF_CSR_WRITE_MODE: user_input.get(
                        CONF_CSR_WRITE_MODE, DEFAULT_CSR_WRITE_MODE
                    ),
                }
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CSR_WRITE_MODE, default=DEFAULT_CSR_WRITE_MODE
                ): vol.In(
                    {
                        CSR_WRITE_MODE_TRANSIENT: "Transient (Recommended)",
                        CSR_WRITE_MODE_PERSISTENT: "Persistent",
                    }
                ),
            }
        )
        return self.async_show_form(step_id="select_csr_write_mode", data_schema=schema)

    async def async_step_select_dongle_warning(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Delegate to select_dongle (shown when no compatible adapter is found)."""
        return await self.async_step_select_dongle(user_input)

    async def async_step_select_esp_warning(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Delegate to select_esp (shown when no ESPHome entity is found)."""
        return await self.async_step_select_esp(user_input)

    def _finalize_config_entry(
        self, entry_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Create a new entry or update the existing one when reconfiguring."""
        if self._reconfigure_entry is not None:
            self.hass.config_entries.async_update_entry(
                self._reconfigure_entry, data=entry_data
            )
            return self.async_abort(reason="reconfigure_successful")
        return self.async_create_entry(title=self._console_name, data=entry_data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return PSXWakeBTOptionsFlowHandler()


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


class PSXWakeBTOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow to reconfigure wake method and target."""

    def __init__(self) -> None:
        self._pending_bt_adapter: str = ""
        self._pending_bt_strategy: str = ""
        # Cached from init pre-scan (used for submit validation)
        self._init_esp_entities: dict[str, str] = {}
        self._init_adapter_names: list[str] = []

    async def async_step_init(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Choose wake method; blocks if the chosen method has no available device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            wake_method = user_input.get(CONF_WAKE_METHOD, WAKE_METHOD_ESPHOME)
            if wake_method == WAKE_METHOD_ESPHOME:
                if not self._init_esp_entities:
                    errors["base"] = "no_esphome_found"
                else:
                    return await self.async_step_init_esphome()
            elif wake_method == WAKE_METHOD_DONGLE:
                if not self._init_adapter_names:
                    errors["base"] = "no_adapter_found"
                else:
                    return await self.async_step_init_dongle()
            else:
                errors["base"] = "invalid_wake_method"

        # (Re-)scan on first display and after each validation error
        self._init_esp_entities = _get_selectable_esphome_entities(self.hass)
        (
            self._init_adapter_names,
            init_compatible,
            _,
            __,
        ) = await self.hass.async_add_executor_job(_scan_adapters)
        available_adapters = self._init_adapter_names
        esp_count = len(self._init_esp_entities)
        compatible_count = len(init_compatible)

        esp_summary = (
            f"{esp_count} compatible node(s)" if esp_count > 0 else "none found ⚠"
        )
        if not available_adapters:
            adapter_summary = "none detected ⚠"
        elif compatible_count > 0:
            adapter_summary = f"{compatible_count} compatible adapter(s)"
        else:
            adapter_summary = (
                f"{len(available_adapters)} adapter(s), none fully compatible ⚠"
            )

        current = self.config_entry.data.get(CONF_WAKE_METHOD, WAKE_METHOD_ESPHOME)
        schema = vol.Schema(
            {
                vol.Required(CONF_WAKE_METHOD, default=current): SelectSelector(
                    SelectSelectorConfig(
                        options=[WAKE_METHOD_ESPHOME, WAKE_METHOD_DONGLE],
                        translation_key="wake_method",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "esp_summary": esp_summary,
                "adapter_summary": adapter_summary,
            },
        )

    async def async_step_init_esphome(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure ESPHome wake target."""
        if user_input is not None:
            entity_id = user_input.get(CONF_ESP_ENTITY, "")
            new_data = dict(self.config_entry.data)
            new_data[CONF_WAKE_METHOD] = WAKE_METHOD_ESPHOME
            new_data[CONF_ESP_ENTITY] = entity_id
            new_data[CONF_BT_ADAPTER] = ""
            if entity_id:
                new_data[CONF_ESP_SERVICE] = _resolve_esphome_service(
                    self.hass, entity_id
                )
            else:
                new_data.pop(CONF_ESP_SERVICE, None)
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            return self.async_create_entry(title="", data={})

        selectable = _get_selectable_esphome_entities(self.hass)
        current_esp = self.config_entry.data.get(CONF_ESP_ENTITY, "")

        if selectable:
            default = current_esp if current_esp in selectable else vol.UNDEFINED
            schema = vol.Schema(
                {
                    vol.Required(CONF_ESP_ENTITY, default=default): vol.In(selectable),
                }
            )
            step_id = "init_esphome"
        else:
            schema = vol.Schema(
                {
                    vol.Optional(CONF_ESP_ENTITY, default=current_esp): str,
                }
            )
            step_id = "init_esphome_warning"

        return self.async_show_form(step_id=step_id, data_schema=schema)

    async def async_step_init_esphome_warning(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Delegate to init_esphome (shown when no ESPHome entity is found)."""
        return await self.async_step_init_esphome(user_input)

    async def async_step_init_dongle(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure Bluetooth dongle adapter."""
        errors: dict[str, str] = {}

        (
            all_names,
            compatible,
            ratings,
            mac_to_hci,
        ) = await self.hass.async_add_executor_job(_scan_adapters)
        ha_managed = _get_ha_managed_adapters(self.hass, mac_to_hci)
        available_names = all_names

        clean = dict(
            sorted(
                ((k, v) for k, v in compatible.items() if k not in ha_managed),
                key=lambda x: (-ratings.get(x[0], 0), int(x[0].replace("hci", ""))),
            )
        )
        warned = dict(
            sorted(
                (
                    (k, f"⚠ {compatible[k]}  (HA Bluetooth adapter — Not recommended)")
                    for k in compatible
                    if k in ha_managed
                ),
                key=lambda x: (-ratings.get(x[0], 0), int(x[0].replace("hci", ""))),
            )
        )
        available_compatible = {**clean, **warned}

        if user_input is not None:
            adapter = user_input.get(CONF_BT_ADAPTER, DEFAULT_BT_ADAPTER).strip()
            if not _is_valid_adapter(adapter):
                errors["base"] = "invalid_adapter"
            elif available_names and adapter not in available_names:
                errors["base"] = "adapter_not_found"
            else:
                self._pending_bt_adapter = adapter
                adapter_index = int(adapter.replace("hci", ""))
                try:
                    strategy = await self.hass.async_add_executor_job(
                        detect_strategy, adapter_index
                    )
                except Exception:
                    _LOGGER.exception("Strategy detection failed for %s", adapter)
                    errors["base"] = "detection_error"
                else:
                    self._pending_bt_strategy = type(strategy).__name__
                    if isinstance(strategy, CsrSpoofStrategy):
                        return await self.async_step_init_csr_write_mode()
                    new_data = dict(self.config_entry.data)
                    new_data[CONF_WAKE_METHOD] = WAKE_METHOD_DONGLE
                    new_data[CONF_BT_ADAPTER] = adapter
                    new_data[CONF_BT_STRATEGY] = self._pending_bt_strategy
                    new_data[CONF_ESP_ENTITY] = ""
                    new_data.pop(CONF_CSR_WRITE_MODE, None)
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data=new_data
                    )
                    return self.async_create_entry(title="", data={})

        current_adapter = self.config_entry.data.get(
            CONF_BT_ADAPTER, DEFAULT_BT_ADAPTER
        )
        if available_compatible:
            default = (
                current_adapter
                if current_adapter in available_compatible
                else next(iter(clean))
                if clean
                else next(iter(available_compatible))
            )
            schema = vol.Schema(
                {
                    vol.Required(CONF_BT_ADAPTER, default=default): vol.In(
                        available_compatible
                    ),
                }
            )
            step_id = "init_dongle"
        else:
            schema = vol.Schema(
                {
                    vol.Required(CONF_BT_ADAPTER, default=current_adapter): str,
                }
            )
            step_id = "init_dongle_warning"

        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def async_step_init_csr_write_mode(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure CSR write mode."""
        if user_input is not None:
            new_data = dict(self.config_entry.data)
            new_data[CONF_WAKE_METHOD] = WAKE_METHOD_DONGLE
            new_data[CONF_BT_ADAPTER] = self._pending_bt_adapter
            new_data[CONF_BT_STRATEGY] = self._pending_bt_strategy
            new_data[CONF_ESP_ENTITY] = ""
            new_data[CONF_CSR_WRITE_MODE] = user_input.get(
                CONF_CSR_WRITE_MODE, DEFAULT_CSR_WRITE_MODE
            )
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            return self.async_create_entry(title="", data={})

        current = self.config_entry.data.get(
            CONF_CSR_WRITE_MODE, DEFAULT_CSR_WRITE_MODE
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_CSR_WRITE_MODE, default=current): vol.In(
                    {
                        CSR_WRITE_MODE_TRANSIENT: "Transient (Recommended)",
                        CSR_WRITE_MODE_PERSISTENT: "Persistent",
                    }
                ),
            }
        )
        return self.async_show_form(step_id="init_csr_write_mode", data_schema=schema)

    async def async_step_init_dongle_warning(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Delegate to init_dongle (shown when no compatible adapter is found)."""
        return await self.async_step_init_dongle(user_input)
