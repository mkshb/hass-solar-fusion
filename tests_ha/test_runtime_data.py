"""Coordinator am Konfigurationseintrag: Energie-Plattform, Aktionen, Entladen."""
from homeassistant.config_entries import ConfigEntryState

from common import DOMAIN, set_sources, setup_entry
from custom_components.solar_fusion.energy import async_get_solar_forecast


async def test_energy_forecast_follows_entry_state(berlin):
    hass = berlin
    set_sources(hass)
    entry, coord = await setup_entry(hass)
    assert entry.runtime_data is coord

    forecast = await async_get_solar_forecast(hass, entry.entry_id)
    assert forecast["wh_hours"]
    assert await async_get_solar_forecast(hass, "unknown") is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert await async_get_solar_forecast(hass, entry.entry_id) is None


async def test_actions_skip_unloaded_entries(berlin):
    hass = berlin
    set_sources(hass)
    entry, _ = await setup_entry(hass)
    response = await hass.services.async_call(
        DOMAIN, "learn_shading", {"days": 1}, blocking=True, return_response=True)
    assert list(response) == [entry.entry_id]

    assert await hass.config_entries.async_unload(entry.entry_id)
    response = await hass.services.async_call(
        DOMAIN, "learn_shading", {"days": 1}, blocking=True, return_response=True)
    assert response == {}
