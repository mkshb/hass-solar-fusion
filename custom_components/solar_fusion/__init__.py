"""Solar Fusion – Home Assistant Integration."""
from __future__ import annotations

import logging
from datetime import date

import voluptuous as vol

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

    async def handle_repair_history(call: ServiceCall) -> None:
        """Re-read actual production from recorder and fix corrupted history."""
        raw_from = call.data.get("date_from")
        raw_to = call.data.get("date_to")
        date_from: str | None = raw_from.isoformat() if isinstance(raw_from, date) else raw_from
        date_to: str | None = raw_to.isoformat() if isinstance(raw_to, date) else raw_to

        coordinators = [
            c for c in hass.data.get(DOMAIN, {}).values()
            if isinstance(c, SolarForecastCoordinator)
        ]
        if not coordinators:
            _LOGGER.warning("repair_history: no active Solar Fusion instances found")
            return
        for coordinator in coordinators:
            result = await coordinator.async_repair_history(
                date_from=date_from, date_to=date_to
            )
            _LOGGER.info("repair_history result: %s", result)

    hass.services.async_register(
        DOMAIN,
        "repair_history",
        handle_repair_history,
        schema=vol.Schema({
            vol.Optional("date_from"): cv.date,
            vol.Optional("date_to"): cv.date,
        }),
    )

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
