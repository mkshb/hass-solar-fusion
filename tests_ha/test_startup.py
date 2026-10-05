"""Start ohne oder mit teilweise verfügbaren Quellen, Rückkehr einer Quelle."""
from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from common import OM, OM_TOM, PV, SC, SC_TOM, set_sources, setup_entry, state


async def _after_recovery_delay(hass, freezer):
    freezer.tick(timedelta(seconds=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _set_unavailable(hass, *entity_ids):
    for entity_id in entity_ids:
        hass.states.async_set(entity_id, "unavailable", {})


async def test_entry_loads_without_any_source_and_recovers(berlin, freezer):
    hass = berlin
    _set_unavailable(hass, OM, OM_TOM, SC, SC_TOM)
    hass.states.async_set(PV, 12.0, {"unit_of_measurement": "kWh",
                                     "state_class": "total_increasing"})
    entry, coord = await setup_entry(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert coord.last_update_success is False
    assert state(hass, "forecast_today").state == "unavailable"
    # Unabhängig von den Quellen läuft der PV-Tageszähler
    assert state(hass, "diagnostics_pv_daily_production").state == "0.0"

    # Sobald eine Quelle Werte hat: sofort neu fusionieren, nicht erst nach 60 min
    set_sources(hass)
    await hass.async_block_till_done()
    assert coord.last_update_success is False      # heute und morgen abwarten
    await _after_recovery_delay(hass, freezer)
    assert coord.last_update_success is True
    assert set(coord.data["active_sources"]) == {"open_meteo_solar_forecast", "solcast"}
    assert float(state(hass, "forecast_today").state) > 0


async def test_missing_source_rejoins_without_waiting_for_the_interval(berlin, freezer):
    hass = berlin
    set_sources(hass)
    _set_unavailable(hass, SC, SC_TOM)
    entry, coord = await setup_entry(hass)
    assert coord.data["active_sources"] == ["open_meteo_solar_forecast"]
    assert coord.data["missing_sources"] == ["solcast"]
    with_one = float(state(hass, "forecast_today").state)
    assert with_one > 0

    set_sources(hass)
    await _after_recovery_delay(hass, freezer)
    assert coord.data["active_sources"] == ["open_meteo_solar_forecast", "solcast"]
    assert coord.data["missing_sources"] == []
    assert float(state(hass, "forecast_today").state) != with_one


async def test_changes_of_available_sources_do_not_trigger_refreshes(berlin, freezer):
    hass = berlin
    set_sources(hass)
    _, coord = await setup_entry(hass)
    updated = coord.data["last_updated"]
    hass.states.async_set(SC, 40.0, {"unit_of_measurement": "kWh"})
    await _after_recovery_delay(hass, freezer)
    assert coord.data["last_updated"] == updated


async def test_unload_and_reload(berlin, freezer):
    hass = berlin
    set_sources(hass)
    entry, coord = await setup_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert state(hass, "forecast_today").state == "unavailable"
    # Nach dem Entladen löst eine Quelle keinen Refresh mehr aus
    coord._unavailable_sources.add("solcast")
    updated = coord.data["last_updated"]
    set_sources(hass)
    await _after_recovery_delay(hass, freezer)
    assert coord.data["last_updated"] == updated

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not coord
    assert float(state(hass, "forecast_today").state) > 0
