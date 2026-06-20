"""The Wake PSX on Bluetooth integration.

This module is the entry point for Home Assistant. It handles the setup
and unloading of the integration's configuration entries.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its data is updated by the options flow.

    Registered as an update listener so that changes saved via the options
    flow take effect immediately without requiring a manual HA restart.
    """
    await hass.config_entries.async_reload(entry.entry_id)


# List of supported platforms. We only use the 'button' platform.
PLATFORMS: list[Platform] = [Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wake PSX on Bluetooth from a config entry.

    Args:
        hass: The Home Assistant instance.
        entry: The configuration entry to setup.

    Returns:
        True if the setup was successful.
    """
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload the integration when options-flow writes new data so that
    # async_added_to_hass runs again and recreates ESPHome state subscriptions.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry when the user removes or reloads the integration.

    Args:
        hass: The Home Assistant instance.
        entry: The configuration entry to unload.

    Returns:
        True if the unloading was successful.
    """
    unload_ok: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
