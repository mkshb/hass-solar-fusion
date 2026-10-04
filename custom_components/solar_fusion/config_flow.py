"""Config flow for Solar Fusion."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from . import calc
from .const import (
    ALL_SOURCES,
    CONF_EXCLUSION_FACTOR,
    CONF_HORIZON_SOURCES,
    CONF_MIN_EVAL_DAYS,
    CONF_INSTANCE_NAME,
    CONF_PV_ENTITY,
    CONF_PV_ENTITIES,
    CONF_SHADING_APPLY,
    CONF_SHADING_LEARN,
    CONF_SOURCES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_SHADING_APPLY,
    DEFAULT_SHADING_LEARN,
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


class SolarFusionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
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
            self._data[CONF_INSTANCE_NAME] = user_input.get(CONF_INSTANCE_NAME, "").strip()
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
            return self.async_create_entry(
                title=_entry_title(self._data.get(CONF_INSTANCE_NAME, "")),
                data=self._data,
            )

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
        return SolarFusionOptionsFlow(config_entry)

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _sources_schema(self) -> vol.Schema:
        """Schema that pre-selects detected sources and asks for an instance name."""
        return vol.Schema(
            {
                vol.Optional(CONF_INSTANCE_NAME, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
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


def _entry_title(instance_name: str) -> str:
    """Return the config entry title, optionally suffixed with the instance name."""
    if instance_name:
        return f"Solar Fusion – {instance_name}"
    return "Solar Fusion"


class SolarFusionOptionsFlow(config_entries.OptionsFlow):
    """Allow reconfiguration of all settings without removing the entry."""

    def __init__(self, config_entry) -> None:
        self._entry = config_entry
        self._current = dict(config_entry.data)
        self._selected: List[str] = []
        self._data: Dict[str, Any] = {}

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 1: instance name + source selection."""
        detected = await self.hass.async_add_executor_job(
            detect_available_sources, self.hass
        )
        current_sources: List[str] = self._current.get(CONF_SOURCES, [])
        current_name: str = self._current.get(CONF_INSTANCE_NAME, "")

        if user_input is not None:
            self._selected = user_input.get(CONF_SOURCES, [])
            if not self._selected:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._sources_schema(detected, current_sources, current_name),
                    errors={"base": "no_sources"},
                )
            self._data[CONF_SOURCES] = self._selected
            self._data[CONF_INSTANCE_NAME] = user_input.get(CONF_INSTANCE_NAME, "").strip()
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="init",
            data_schema=self._sources_schema(detected, current_sources, current_name),
            description_placeholders={
                "detected": (
                    "Erkannte Integrationen: " + ", ".join(SOURCE_NAMES[s] for s in detected)
                    if detected else "Keine Integrationen erkannt."
                )
            },
        )

    async def async_step_entities(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 2: confirm / override entity IDs per source."""
        current_map: Dict = self._current.get("entity_map", {})

        if user_input is not None:
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
            saved = current_map.get(source_id, {})
            defaults = _DEFAULT_ENTITIES.get(source_id, {})
            schema_fields[
                vol.Optional(
                    f"{source_id}_today",
                    default=saved.get("today", defaults.get("today", "")),
                )
            ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
            schema_fields[
                vol.Optional(
                    f"{source_id}_tomorrow",
                    default=saved.get("tomorrow", defaults.get("tomorrow", "")),
                )
            ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))

        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema(schema_fields),
            description_placeholders={
                "hint": "Aktuelle Entity-IDs sind vorausgefüllt. Nur ändern wenn nötig."
            },
        )

    async def async_step_settings(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 3: PV sensors + update interval."""
        current_pv: List[str] = self._current.get(CONF_PV_ENTITIES) or (
            [self._current[CONF_PV_ENTITY]] if self._current.get(CONF_PV_ENTITY) else []
        )

        if user_input is not None:
            pv_entities = [e for e in user_input.get(CONF_PV_ENTITIES, []) if e]
            self._data[CONF_PV_ENTITIES] = pv_entities
            self._data[CONF_PV_ENTITY] = pv_entities[0] if len(pv_entities) == 1 else ""
            self._data[CONF_UPDATE_INTERVAL] = user_input[CONF_UPDATE_INTERVAL]
            self._data[CONF_EXCLUSION_FACTOR] = float(user_input[CONF_EXCLUSION_FACTOR])
            self._data[CONF_MIN_EVAL_DAYS] = int(user_input[CONF_MIN_EVAL_DAYS])
            self._data[CONF_SHADING_LEARN] = bool(user_input[CONF_SHADING_LEARN])
            self._data[CONF_SHADING_APPLY] = bool(user_input[CONF_SHADING_APPLY])
            self._data[CONF_HORIZON_SOURCES] = [
                s for s in user_input.get(CONF_HORIZON_SOURCES, []) if s in self._selected
            ]

            # Persist everything back to config entry data
            new_name = self._data.get(CONF_INSTANCE_NAME, "")
            self.hass.config_entries.async_update_entry(
                self._entry,
                title=_entry_title(new_name),
                data={**self._current, **self._data},
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PV_ENTITIES, default=current_pv): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", multiple=True)
                    ),
                    vol.Optional(
                        CONF_UPDATE_INTERVAL,
                        default=self._current.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=15, max=360, step=15, mode="slider")
                    ),
                    vol.Optional(
                        CONF_EXCLUSION_FACTOR,
                        default=self._current.get(
                            CONF_EXCLUSION_FACTOR, calc.DEFAULT_EXCLUSION_FACTOR
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1.0, max=5.0, step=0.1, mode="box")
                    ),
                    vol.Optional(
                        CONF_MIN_EVAL_DAYS,
                        default=self._current.get(
                            CONF_MIN_EVAL_DAYS, calc.DEFAULT_MIN_EVAL_DAYS
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=3, max=14, step=1, mode="box")
                    ),
                    vol.Optional(
                        CONF_SHADING_LEARN,
                        default=self._current.get(CONF_SHADING_LEARN, DEFAULT_SHADING_LEARN),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_SHADING_APPLY,
                        default=self._current.get(CONF_SHADING_APPLY, DEFAULT_SHADING_APPLY),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_HORIZON_SOURCES,
                        default=[
                            s for s in self._current.get(CONF_HORIZON_SOURCES, [])
                            if s in self._selected
                        ],
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=s, label=SOURCE_NAMES[s])
                                for s in self._selected
                            ],
                            multiple=True,
                        )
                    ),
                }
            ),
            description_placeholders={
                "pv_hint": "Mehrere Sensoren werden automatisch summiert (z. B. Dach + Garage)."
            },
        )

    def _sources_schema(
        self,
        detected: List[str],
        current_sources: List[str],
        current_name: str,
    ) -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional(CONF_INSTANCE_NAME, default=current_name): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Required(CONF_SOURCES, default=current_sources): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=s,
                                label=SOURCE_NAMES[s]
                                + (" (aktiv)" if s in current_sources else "")
                                + (" [erkannt]" if s in detected else ""),
                            )
                            for s in ALL_SOURCES
                        ],
                        multiple=True,
                    )
                ),
            }
        )
