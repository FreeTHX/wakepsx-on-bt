"""Button platform for Wake PSX on Bluetooth.

Creates a simple, always-available button entity to trigger the wake up sequence.
It does not track the ESP state to avoid UI glitches during Bluetooth execution.
"""

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import State

from .const import (
    CONF_BT_ADAPTER,
    CONF_BT_STRATEGY,
    CONF_CONSOLE_NAME,
    CONF_CSR_WRITE_MODE,
    CONF_DSX_MAC,
    CONF_ESP_ENTITY,
    CONF_ESP_SERVICE,
    CONF_PSX_MAC,
    CONF_WAKE_METHOD,
    CSR_WRITE_MODE_PERSISTENT,
    DEFAULT_BT_ADAPTER,
    DEFAULT_CONSOLE_NAME,
    DEFAULT_CSR_WRITE_MODE,
    DOMAIN,
    ESPHOME_DOMAIN,
    ESPHOME_WAKE_SERVICE,
    MANUFACTURER_SONY,
    STATUS_SENSOR_SUFFIX,
    WAKE_METHOD_DONGLE,
    WAKE_METHOD_ESPHOME,
)
from pywakepsx_on_bt import strategies as _strategies
from pywakepsx_on_bt.exceptions import PsxWakeBtError
from pywakepsx_on_bt.strategies import detect_strategy
from pywakepsx_on_bt.wake import WakeResult, wake_psx

_LOGGER = logging.getLogger(__name__)
_ADAPTER_PATTERN = re.compile(r"^hci(\d+)$")


# Seconds before the wake status resets to "Ready" after a dongle result.
# Matches WAKE_RESULT_DISPLAY_MS on the ESP firmware side for consistency.
_WAKE_STATUS_RESET_DELAY: int = 5

_ESP_TIMEOUT_STATES: frozenset[str] = frozenset({"Page Timeout", "No Response"})

# Per-adapter asyncio locks. Prevents concurrent HCI operations (BD_ADDR spoof +
# wake sequence) when two console entries share the same Bluetooth dongle.
_ADAPTER_LOCKS: dict[str, asyncio.Lock] = {}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Wake button from a config entry."""
    async_add_entities([PSXWakeButton(entry)])


class PSXWakeButton(ButtonEntity):
    """Representation of the PlayStation Wake button."""

    _attr_icon = "mdi:sony-playstation"
    _attr_has_entity_name = True
    _attr_name = "Wake"

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the button."""
        self._entry = entry
        psx_mac: str = entry.data[CONF_PSX_MAC]
        self._attr_unique_id = f"{DOMAIN}_{psx_mac.replace(':', '').lower()}_wake"
        # Last wake status — updated after each press
        self._last_wake_result: str = "ready"
        self._last_wake_status_code: int | None = None
        self._last_wake_status_text: str = "Ready"
        self._last_wake_method: str = ""
        self._reset_timer_cancel: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Config helpers — always read from entry.data so options-flow
    # changes take effect immediately without an integration reload.
    # ------------------------------------------------------------------

    @property
    def _psx_mac(self) -> str:
        return self._entry.data[CONF_PSX_MAC]

    @property
    def _dsx_mac(self) -> str:
        return self._entry.data[CONF_DSX_MAC]

    @property
    def _console_name(self) -> str:
        return self._entry.data.get(CONF_CONSOLE_NAME, DEFAULT_CONSOLE_NAME)

    @property
    def _esp_entity_id(self) -> str | None:
        return self._entry.data.get(CONF_ESP_ENTITY) or None

    @property
    def _wake_method(self) -> str:
        return self._entry.data.get(
            CONF_WAKE_METHOD,
            WAKE_METHOD_ESPHOME if self._esp_entity_id else WAKE_METHOD_DONGLE,
        )

    @property
    def _bt_adapter(self) -> str:
        return self._entry.data.get(CONF_BT_ADAPTER, DEFAULT_BT_ADAPTER)

    @property
    def _csr_write_mode(self) -> str:
        return self._entry.data.get(CONF_CSR_WRITE_MODE, DEFAULT_CSR_WRITE_MODE)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose last wake result as entity attributes."""
        return {
            "last_wake_result": self._last_wake_result,
            "last_wake_status_code": self._last_wake_status_code,
            "last_wake_status_text": self._last_wake_status_text,
            "last_wake_method": self._last_wake_method,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to ESPHome status sensor state changes."""
        await super().async_added_to_hass()
        esp_entity = self._esp_entity_id
        if esp_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [esp_entity],
                    self._handle_esp_status_change,
                )
            )
        self.async_on_remove(self._cancel_reset_timer)

    @callback
    def _handle_esp_status_change(self, event: Event) -> None:
        """Mirror ESPHome status sensor into button attributes."""
        new_state: State | None = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        state_str = new_state.state
        # "Connecting..." is transient — skip to avoid clobbering last result
        if state_str == "Connecting...":
            return
        if state_str == "Ready":
            result = "ready"
        elif state_str == "Success":
            result = "success"
        elif state_str in _ESP_TIMEOUT_STATES:
            result = "timeout"
        else:
            result = "error"
        self._last_wake_method = WAKE_METHOD_ESPHOME
        self._last_wake_result = result
        self._last_wake_status_code = None
        self._last_wake_status_text = state_str
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information to link this entity to the HA device registry."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._psx_mac)},
            name=self._console_name,
            manufacturer=MANUFACTURER_SONY,
            model=self._console_name,
        )

    @property
    def available(self) -> bool:
        """Button availability depends on selected wake method.

        ESPHome mode without a configured entity returns False (unavailable).
        This can only happen if the user bypassed the config flow warning step
        and left the entity field empty — not a normal user path.
        """
        if self._wake_method == WAKE_METHOD_DONGLE:
            return True
        return bool(self._esp_entity_id)

    @staticmethod
    def _adapter_index(adapter: str) -> int | None:
        """Parse hci adapter name into its integer index."""
        match = _ADAPTER_PATTERN.fullmatch(adapter.strip())
        if not match:
            return None
        return int(match.group(1))

    def _wake_via_dongle(self) -> WakeResult:
        """Execute the local wake sequence via raw HCI dongle.

        Blocking — must be called via ``async_add_executor_job``, never
        directly from the event loop. May block for up to several seconds
        while waiting for the HCI Connection Complete event.
        """
        adapter_index = self._adapter_index(self._bt_adapter)
        if adapter_index is None:
            raise ValueError(
                f"Invalid Bluetooth adapter '{self._bt_adapter}'. Expected format hciX"
            )

        use_transient = self._csr_write_mode != CSR_WRITE_MODE_PERSISTENT
        cached_name = self._entry.data.get(CONF_BT_STRATEGY, "")
        if cached_name:
            strategy_cls = getattr(_strategies, cached_name, None)
            if strategy_cls is not None:
                try:
                    strategy = strategy_cls(use_transient=use_transient)
                except TypeError:
                    strategy = strategy_cls()
                _LOGGER.debug(
                    "Using cached strategy %s for %s", cached_name, self._bt_adapter
                )
            else:
                _LOGGER.debug(
                    "Cached strategy %s not found, falling back to detect", cached_name
                )
                strategy = detect_strategy(adapter_index, use_transient=use_transient)
        else:
            strategy = detect_strategy(adapter_index, use_transient=use_transient)

        return wake_psx(
            dsx_mac=self._dsx_mac,
            psx_mac=self._psx_mac,
            adapter=self._bt_adapter,
            spoof_strategy=strategy,
        )

    def _resolve_esphome_service_name(self) -> str:
        """Return the ESPHome service name for this node.

        Uses the cached value from config entry when available. Falls back to
        device registry lookup, then suffix-stripping from entity_id.
        """
        cached = self._entry.data.get(CONF_ESP_SERVICE, "")
        if cached:
            return cached

        ent_reg = er.async_get(self.hass)
        entity_entry = ent_reg.async_get(self._esp_entity_id)
        if entity_entry and entity_entry.device_id:
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get(entity_entry.device_id)
            if device:
                for entry_id in device.config_entries:
                    cfg = self.hass.config_entries.async_get_entry(entry_id)
                    if cfg and cfg.domain == ESPHOME_DOMAIN:
                        node_name = (
                            (cfg.data.get("device_name") or cfg.title)
                            .lower()
                            .replace(" ", "_")
                            .replace("-", "_")
                        )
                        return f"{node_name}_{ESPHOME_WAKE_SERVICE}"

        # Fallback: derive from entity_id
        entity_name = self._esp_entity_id.split(".", 1)[-1]
        node_name = entity_name.removesuffix(f"_{STATUS_SENSOR_SUFFIX}")
        return f"{node_name}_{ESPHOME_WAKE_SERVICE}"

    async def _async_press_esphome(self) -> None:
        """Handle wake by sending service call to ESPHome."""
        if not self._esp_entity_id:
            _LOGGER.warning("Cannot wake console: no ESPHome node is configured")
            return

        service_name = self._resolve_esphome_service_name()

        if not self.hass.services.has_service(ESPHOME_DOMAIN, service_name):
            available = [
                s
                for s in self.hass.services.async_services_for_domain(ESPHOME_DOMAIN)
                if ESPHOME_WAKE_SERVICE in s
            ]
            _LOGGER.warning(
                "ESPHome service '%s.%s' not found. Wake services available: %s",
                ESPHOME_DOMAIN,
                service_name,
                available or "none",
            )
            raise HomeAssistantError(
                f"ESPHome service '{ESPHOME_DOMAIN}.{service_name}' not found. "
                f"Check that the ESPHome node is online."
            )

        _LOGGER.debug(
            "Calling %s.%s (PSX=%s DSX=%s)",
            ESPHOME_DOMAIN,
            service_name,
            self._psx_mac,
            self._dsx_mac,
        )
        await self.hass.services.async_call(
            ESPHOME_DOMAIN,
            service_name,
            {CONF_PSX_MAC: self._psx_mac, CONF_DSX_MAC: self._dsx_mac},
            blocking=True,
        )

    @callback
    def _cancel_reset_timer(self) -> None:
        """Cancel any pending status-reset timer."""
        if self._reset_timer_cancel is not None:
            self._reset_timer_cancel()
            self._reset_timer_cancel = None

    @callback
    def _schedule_status_reset(self) -> None:
        """Reset wake status to 'Ready' after _WAKE_STATUS_RESET_DELAY seconds.

        Cancels any pending reset first so rapid presses don't stack timers.
        """
        self._cancel_reset_timer()

        @callback
        def _reset(_now) -> None:
            self._last_wake_result = "ready"
            self._last_wake_status_code = None
            self._last_wake_status_text = "Ready"
            self._last_wake_method = ""
            self.async_write_ha_state()

        self._reset_timer_cancel = async_call_later(
            self.hass, _WAKE_STATUS_RESET_DELAY, _reset
        )

    @callback
    def _set_error_state(self) -> None:
        """Mark the last wake attempt as failed and schedule a status reset."""
        self._last_wake_result = "error"
        self._last_wake_method = self._wake_method
        self.async_write_ha_state()
        self._schedule_status_reset()

    async def async_press(self) -> None:
        """Handle button press according to selected wake method."""
        try:
            if self._wake_method == WAKE_METHOD_DONGLE:
                lock = _ADAPTER_LOCKS.setdefault(self._bt_adapter, asyncio.Lock())
                if lock.locked():
                    self._last_wake_result = "error"
                    self._last_wake_status_text = "Busy"
                    self._last_wake_method = WAKE_METHOD_DONGLE
                    self.async_write_ha_state()
                    self._schedule_status_reset()
                    return
                async with lock:
                    try:
                        result = await self.hass.async_add_executor_job(
                            self._wake_via_dongle
                        )
                    finally:
                        # Hold the lock during the adapter recovery window.
                        # Some chipsets (Broadcom, CSR) briefly re-enumerate
                        # their USB device after the HCI Reset issued by the
                        # spoof restore. Without this delay the next press
                        # would try to open the HCI socket while the adapter
                        # is still offline.
                        await asyncio.sleep(1.5)
                self._last_wake_method = WAKE_METHOD_DONGLE
                self._last_wake_status_code = result.status_code
                self._last_wake_status_text = result.status_text
                self._last_wake_result = result.outcome
                _LOGGER.debug(
                    "BT wake via %s: %s (0x%02X)",
                    result.adapter,
                    result.status_text,
                    result.status_code,
                )
                self.async_write_ha_state()
                self._schedule_status_reset()
            else:
                await self._async_press_esphome()
                # ESPHome status arrives asynchronously via _handle_esp_status_change
                # and resets to "Ready" when the ESP firmware publishes it after 5 s.
        except HomeAssistantError:
            self._set_error_state()
            raise
        except PsxWakeBtError as err:
            _LOGGER.exception("Wake failed (%s)", self._wake_method)
            self._set_error_state()
            raise HomeAssistantError(str(err)) from err
        except Exception as err:
            _LOGGER.exception(
                "Unexpected error during wake sequence (%s)", self._wake_method
            )
            self._set_error_state()
            raise HomeAssistantError(str(err)) from err
