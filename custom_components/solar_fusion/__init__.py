"""Solar Fusion – Home Assistant Integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import SolarForecastCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Solar Fusion integration.

    This function must exist so that Home Assistant fires EVENT_COMPONENT_LOADED
    for the 'solar_fusion' domain. Without it, async_process_integration_platforms
    may not discover energy.py and Solar Fusion won't appear in the Energy Dashboard
    as a forecast provider.
    """
    hass.data.setdefault(DOMAIN, {})

    async def handle_take_snapshot(call: ServiceCall) -> None:
        """Manually trigger a morning snapshot for all Solar Fusion instances."""
        coordinators = [
            c for c in hass.data.get(DOMAIN, {}).values()
            if isinstance(c, SolarForecastCoordinator)
        ]
        if not coordinators:
            _LOGGER.warning("take_snapshot: no active Solar Fusion instances found")
            return
        for coordinator in coordinators:
            await coordinator.async_take_snapshot_now()

    hass.services.async_register(DOMAIN, "take_snapshot", handle_take_snapshot)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solar Fusion from a config entry."""
    coordinator = SolarForecastCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
