"""Solar Fusion – Home Assistant Integration."""
from __future__ import annotations

import logging
from datetime import date

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify

from .card import async_setup_card
from .const import CONF_INSTANCE_NAME, DOMAIN, device_name
from .coordinator import SolarForecastCoordinator, SolarFusionConfigEntry

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _loaded_coordinators(hass: HomeAssistant) -> dict[str, SolarForecastCoordinator]:
    """Coordinator je geladenem Eintrag: {entry_id: coordinator}."""
    return {
        entry.entry_id: entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    }


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Solar Fusion integration.

    This function must exist so that Home Assistant fires EVENT_COMPONENT_LOADED
    for the 'solar_fusion' domain. Without it, async_process_integration_platforms
    may not discover energy.py and Solar Fusion won't appear in the Energy Dashboard
    as a forecast provider.
    """
    await async_setup_card(hass)

    async def handle_take_snapshot(call: ServiceCall) -> None:
        """Manually trigger a morning snapshot for all Solar Fusion instances."""
        coordinators = _loaded_coordinators(hass)
        if not coordinators:
            _LOGGER.warning("take_snapshot: no active Solar Fusion instances found")
            return
        for coordinator in coordinators.values():
            await coordinator.async_take_snapshot_now()

    hass.services.async_register(DOMAIN, "take_snapshot", handle_take_snapshot)

    async def handle_repair_history(call: ServiceCall) -> None:
        """Re-read actual production from recorder and fix corrupted history."""
        raw_from = call.data.get("date_from")
        raw_to = call.data.get("date_to")
        date_from: str | None = raw_from.isoformat() if isinstance(raw_from, date) else raw_from
        date_to: str | None = raw_to.isoformat() if isinstance(raw_to, date) else raw_to

        coordinators = _loaded_coordinators(hass)
        if not coordinators:
            _LOGGER.warning("repair_history: no active Solar Fusion instances found")
            return
        for coordinator in coordinators.values():
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

    async def handle_learn_shading(call: ServiceCall) -> ServiceResponse:
        """Learn the shading map retroactively from the recorder."""
        coordinators = _loaded_coordinators(hass)
        if not coordinators:
            _LOGGER.warning("learn_shading: no active Solar Fusion instances found")
            return {}
        days = int(call.data.get("days", 10))
        return {
            entry_id: await coordinator.async_learn_shading(days=days)
            for entry_id, coordinator in coordinators.items()
        }

    hass.services.async_register(
        DOMAIN,
        "learn_shading",
        handle_learn_shading,
        schema=vol.Schema({
            vol.Optional("days", default=10): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: SolarFusionConfigEntry) -> bool:
    """Set up Solar Fusion from a config entry."""
    _async_fix_doubled_entity_ids(hass, entry)
    coordinator = SolarForecastCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _async_fix_doubled_entity_ids(hass: HomeAssistant, entry: SolarFusionConfigEntry) -> None:
    """Entity-IDs mit doppeltem Gerätepräfix umbenennen.

    Bis 0.3.0 enthielten die Entitätsnamen das Gerätepräfix selbst
    ("Solar Fusion Home – …"); HA 2026.10 setzt den Gerätenamen zusätzlich
    davor. Neu angelegte Entitäten bekamen so IDs wie
    ``sensor.solar_fusion_home_solar_fusion_home_diagnostics_shading``.
    Umbenannt wird nur, wenn die Ziel-ID frei ist; Statistik und Historie
    zieht der Recorder mit.
    """
    slug = slugify(device_name(entry.data.get(CONF_INSTANCE_NAME, "")))
    doubled = f"sensor.{slug}_{slug}_"
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not reg_entry.entity_id.startswith(doubled):
            continue
        new_id = f"sensor.{slug}_" + reg_entry.entity_id[len(doubled):]
        if registry.async_get(new_id) is not None or hass.states.get(new_id) is not None:
            _LOGGER.warning(
                "Cannot rename %s to %s: target already exists", reg_entry.entity_id, new_id
            )
            continue
        _LOGGER.info("Renaming %s to %s", reg_entry.entity_id, new_id)
        registry.async_update_entity(reg_entry.entity_id, new_entity_id=new_id)


async def async_unload_entry(hass: HomeAssistant, entry: SolarFusionConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: SolarFusionConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
