"""Config flow for Solar Forecast Fusion."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    ALL_SOURCES,
    CONF_PV_ENTITY,
    CONF_PV_ENTITIES,
    CONF_SOURCES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    SOURCE_NAMES,
    SOURCE_DOCS,
    FORECAST_SOLAR_TODAY,
    FORECAST_SOLAR_TOMORROW,
    OPEN_METEO_TODAY,
    OPEN_METEO_TOMORROW,
    SOLCAST_TODAY,
    SOLCAST_TOMORROW,
)
from .source_reader import detect_available_sources

_LOGGER = logging.getLogger(__name__)

# Default entity IDs per source
_DEFAULT_ENTITIES = {
    "forecast_solar": {
        "today": FORECAST_SOLAR_TODAY,
        "tomorrow": FORECAST_SOLAR_TOMORROW,
    },
    "open_meteo_solar_forecast": {
        "today": OPEN_METEO_TODAY,
        "tomorrow": OPEN_METEO_TOMORROW,
    },
    "solcast": {
        "today": SOLCAST_TODAY,
        "tomorrow": SOLCAST_TOMORROW,
    },
}


class SolarForecastFusionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Three-step config flow:
      Step 1 (user)    – auto-detect & select which installed sources to use
      Step 2 (entities)– confirm / adjust entity IDs per source
      Step 3 (options) – PV production sensor + update interval
    """

    VERSION = 1
    _data: Dict[str, Any] = {}
    _detected: List[str] = []
    _selected: List[str] = []

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 1: detect installed sources, let user select which to combine."""
        self._detected = await self.hass.async_add_executor_job(
            detect_available_sources, self.hass
        )

        if user_input is not None:
            self._selected = user_input[CONF_SOURCES]
            if not self._selected:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._sources_schema(),
                    errors={"base": "no_sources"},
                    description_placeholders=self._source_hints(),
                )
            self._data[CONF_SOURCES] = self._selected
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="user",
            data_schema=self._sources_schema(),
            description_placeholders=self._source_hints(),
        )

    async def async_step_entities(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 2: confirm or override entity IDs for each selected source."""
        if user_input is not None:
            # Build entity_map from flat form fields
            entity_map: Dict[str, Dict] = {}
            for source_id in self._selected:
                entity_map[source_id] = {
                    "today": user_input.get(f"{source_id}_today", _DEFAULT_ENTITIES[source_id]["today"]),
                    "tomorrow": user_input.get(f"{source_id}_tomorrow", _DEFAULT_ENTITIES[source_id]["tomorrow"]),
                }
            self._data["entity_map"] = entity_map
            return await self.async_step_settings()

        schema_fields: Dict = {}
        for source_id in self._selected:
            defaults = _DEFAULT_ENTITIES.get(source_id, {})
            schema_fields[
                vol.Optional(f"{source_id}_today", default=defaults.get("today", ""))
            ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
            schema_fields[
                vol.Optional(f"{source_id}_tomorrow", default=defaults.get("tomorrow", ""))
            ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))

        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema(schema_fields),
            description_placeholders={
                "hint": "Verify the entity IDs match those created by each forecast integration. "
                        "Leave unchanged if you used the default integration setup."
            },
        )

    async def async_step_settings(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 3: PV production sensor(s) and update interval."""
        if user_input is not None:
            pv_entities: List[str] = [
                e for e in user_input.get(CONF_PV_ENTITIES, []) if e
            ]
            self._data[CONF_PV_ENTITIES] = pv_entities
            self._data[CONF_PV_ENTITY] = pv_entities[0] if len(pv_entities) == 1 else ""
            self._data[CONF_UPDATE_INTERVAL] = user_input[CONF_UPDATE_INTERVAL]
            return self.async_create_entry(title="Solar Forecast Fusion", data=self._data)

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PV_ENTITIES, default=[]): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", multiple=True)
                    ),
                    vol.Optional(
                        CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=15, max=360, step=15, mode="slider")
                    ),
                }
            ),
            description_placeholders={
                "pv_hint": (
                    "Wähle einen oder mehrere PV-Produktionssensoren. "
                    "Bei mehreren Sensoren (z. B. Dach + Garage) werden die Werte "
                    "automatisch summiert – ein gemeinsamer Tageszähler wird erstellt."
                )
            },
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Options flow (re-configure without removing the entry)
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SolarForecastFusionOptionsFlow(config_entry)

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _sources_schema(self) -> vol.Schema:
        """Schema that pre-selects detected sources."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_SOURCES,
                    default=self._detected,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=s,
                                label=SOURCE_NAMES[s]
                                + (" ✓ detected" if s in self._detected else " (not found)"),
                            )
                            for s in ALL_SOURCES
                        ],
                        multiple=True,
                    )
                ),
            }
        )

    def _source_hints(self) -> Dict[str, str]:
        if self._detected:
            found = ", ".join(SOURCE_NAMES[s] for s in self._detected)
            return {"detected": f"Auto-detected: {found}"}
        return {"detected": "No forecast integrations detected. Install one first."}


class SolarForecastFusionOptionsFlow(config_entries.OptionsFlow):
    """Allow reconfiguration of settings without removing the entry."""

    def __init__(self, config_entry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        current = dict(self._entry.data)
        # Migrate legacy single-entity key to list
        current_pv: List[str] = current.get(CONF_PV_ENTITIES) or (
            [current[CONF_PV_ENTITY]] if current.get(CONF_PV_ENTITY) else []
        )

        if user_input is not None:
            pv_entities = [e for e in user_input.get(CONF_PV_ENTITIES, []) if e]
            user_input[CONF_PV_ENTITIES] = pv_entities
            user_input[CONF_PV_ENTITY] = pv_entities[0] if len(pv_entities) == 1 else ""
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PV_ENTITIES, default=current_pv): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", multiple=True)
                    ),
                    vol.Optional(
                        CONF_UPDATE_INTERVAL,
                        default=current.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=15, max=360, step=15, mode="slider")
                    ),
                }
            ),
            description_placeholders={
                "pv_hint": (
                    "Mehrere Sensoren werden automatisch summiert "
                    "(z. B. Dach + Garage)."
                )
            },
        )
