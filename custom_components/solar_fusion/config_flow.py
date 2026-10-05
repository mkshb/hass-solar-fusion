"""Config flow for Solar Fusion."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
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
    FORECAST_SOLAR_TODAY,
    FORECAST_SOLAR_TOMORROW,
    OPEN_METEO_TODAY,
    OPEN_METEO_TOMORROW,
    SOLCAST_TODAY,
    SOLCAST_TOMORROW,
)
from .source_reader import detect_available_sources, resolve_entities
from .validation import forecast_entity_error, pv_entity_error

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
    Three-step config flow, also used to reconfigure an existing entry:
      Step 1 (user / reconfigure) – instance name and sources to combine
      Step 2 (entities)           – confirm / adjust entity IDs per source
      Step 3 (settings)           – PV production sensors (+ update interval on setup)

    Tuning parameters (update interval, weighting, shading) live in the
    options flow and are stored in ``entry.options``.
    """

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}
        self._detected: List[str] = []
        self._selected: List[str] = []
        self._reconfigure_entry: Optional[config_entries.ConfigEntry] = None

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 1: detect installed sources, let user select which to combine."""
        self._detected = detect_available_sources(self.hass)
        return await self._async_sources_step("user", user_input, "", self._detected)

    async def async_step_reconfigure(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 1 of reconfiguring: instance name and sources of an existing entry."""
        self._reconfigure_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        entry_data = self._reconfigure_entry.data
        self._detected = detect_available_sources(self.hass)
        return await self._async_sources_step(
            "reconfigure",
            user_input,
            entry_data.get(CONF_INSTANCE_NAME, ""),
            entry_data.get(CONF_SOURCES, []),
        )

    async def _async_sources_step(
        self,
        step_id: str,
        user_input: Optional[Dict[str, Any]],
        default_name: str,
        default_sources: List[str],
    ) -> FlowResult:
        """Instance name and sources (setup and reconfigure)."""
        if user_input is not None:
            self._selected = user_input.get(CONF_SOURCES, [])
            if not self._selected:
                return self.async_show_form(
                    step_id=step_id,
                    data_schema=self._sources_schema(default_name, default_sources),
                    errors={"base": "no_sources"},
                    description_placeholders=self._source_hints(),
                )
            self._data[CONF_SOURCES] = self._selected
            self._data[CONF_INSTANCE_NAME] = user_input.get(CONF_INSTANCE_NAME, "").strip()
            return await self.async_step_entities()

        return self.async_show_form(
            step_id=step_id,
            data_schema=self._sources_schema(default_name, default_sources),
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
            errors = self._entity_errors(entity_map)
            if not errors:
                self._data["entity_map"] = entity_map
                return await self.async_step_settings()
            return self.async_show_form(
                step_id="entities",
                data_schema=self.add_suggested_values_to_schema(
                    self._entities_schema(), user_input
                ),
                errors=errors,
                description_placeholders=_ENTITIES_HINT,
            )

        return self.async_show_form(
            step_id="entities",
            data_schema=self._entities_schema(),
            description_placeholders=_ENTITIES_HINT,
        )

    def _entity_errors(self, entity_map: Dict[str, Dict]) -> Dict[str, str]:
        """Fehler je Feld; geprüft wird die Entität, die der Reader liest."""
        errors: Dict[str, str] = {}
        for source_id, entities in entity_map.items():
            resolved = resolve_entities(self.hass, source_id, entities)
            if resolved is None:
                continue
            for day, entity_id in zip(("today", "tomorrow"), resolved):
                if error := forecast_entity_error(self.hass, entity_id):
                    errors[f"{source_id}_{day}"] = error
        return errors

    async def async_step_settings(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 3: PV production sensor(s); update interval only on first setup."""
        entry = self._reconfigure_entry
        errors: Dict[str, str] = {}
        if user_input is not None:
            pv_entities: List[str] = [
                e for e in user_input.get(CONF_PV_ENTITIES, []) if e
            ]
            for entity_id in pv_entities:
                if error := pv_entity_error(self.hass, entity_id):
                    errors[CONF_PV_ENTITIES] = error
                    break
        if user_input is not None and not errors:
            self._data[CONF_PV_ENTITIES] = pv_entities
            self._data[CONF_PV_ENTITY] = pv_entities[0] if len(pv_entities) == 1 else ""
            title = _entry_title(self._data.get(CONF_INSTANCE_NAME, ""))
            if entry is not None:
                # Horizont-Quellen auf die gewählten Quellen beschränken
                options = dict(entry.options)
                if CONF_HORIZON_SOURCES in options:
                    options[CONF_HORIZON_SOURCES] = [
                        s for s in options[CONF_HORIZON_SOURCES] if s in self._selected
                    ]
                # Löst über den Update-Listener das Neuladen aus
                self.hass.config_entries.async_update_entry(
                    entry, title=title, data={**entry.data, **self._data}, options=options
                )
                return self.async_abort(reason="reconfigure_successful")
            return self.async_create_entry(
                title=title,
                data=self._data,
                options={CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL])},
            )

        fields: Dict = {
            vol.Optional(CONF_PV_ENTITIES, default=_pv_entities(entry.data) if entry else []):
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", multiple=True)
                ),
        }
        if entry is None:
            fields[vol.Optional(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL)] = (
                _update_interval_selector()
            )
        schema = vol.Schema(fields)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="settings",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "pv_hint": (
                    "Wähle einen oder mehrere PV-Produktionssensoren. "
                    "Bei mehreren Sensoren (z. B. Dach + Garage) werden die Werte "
                    "automatisch summiert – ein gemeinsamer Tageszähler wird erstellt."
                )
            },
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Options flow (tuning parameters)
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SolarFusionOptionsFlow(config_entry)

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _sources_schema(self, default_name: str, default_sources: List[str]) -> vol.Schema:
        """Schema with instance name and the sources to combine."""
        return vol.Schema(
            {
                vol.Optional(CONF_INSTANCE_NAME, default=default_name): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Required(
                    CONF_SOURCES,
                    default=default_sources,
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

    def _entities_schema(self) -> vol.Schema:
        """Entity fields per selected source; prefilled from the entry when reconfiguring."""
        saved_map: Dict = (
            self._reconfigure_entry.data.get("entity_map", {}) if self._reconfigure_entry else {}
        )
        schema_fields: Dict = {}
        for source_id in self._selected:
            saved = saved_map.get(source_id, {})
            defaults = _DEFAULT_ENTITIES.get(source_id, {})
            for day in ("today", "tomorrow"):
                schema_fields[
                    vol.Optional(
                        f"{source_id}_{day}", default=saved.get(day, defaults.get(day, ""))
                    )
                ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
        return vol.Schema(schema_fields)

    def _source_hints(self) -> Dict[str, str]:
        if self._detected:
            found = ", ".join(SOURCE_NAMES[s] for s in self._detected)
            return {"detected": f"Auto-detected: {found}"}
        return {"detected": "No forecast integrations detected. Install one first."}


_ENTITIES_HINT = {
    "hint": "Verify the entity IDs match those created by each forecast integration. "
            "Leave unchanged if you used the default integration setup."
}


def _entry_title(instance_name: str) -> str:
    """Return the config entry title, optionally suffixed with the instance name."""
    if instance_name:
        return f"Solar Fusion – {instance_name}"
    return "Solar Fusion"


def _pv_entities(data: Mapping[str, Any]) -> List[str]:
    """PV sensors of an entry (older entries: single ``pv_entity``)."""
    return list(data.get(CONF_PV_ENTITIES) or (
        [data[CONF_PV_ENTITY]] if data.get(CONF_PV_ENTITY) else []
    ))


def _update_interval_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=15, max=360, step=15, mode="slider")
    )


class SolarFusionOptionsFlow(config_entries.OptionsFlow):
    """Tuning parameters; sources, entities and PV sensors via reconfigure."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    def _current(self, key: str, default: Any) -> Any:
        """Current value: options, else data (entry before version 1.2), else default."""
        return self._entry.options.get(key, self._entry.data.get(key, default))

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Update interval, weighting and shading."""
        sources: List[str] = self._entry.data.get(CONF_SOURCES, [])
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL]),
                    CONF_EXCLUSION_FACTOR: float(user_input[CONF_EXCLUSION_FACTOR]),
                    CONF_MIN_EVAL_DAYS: int(user_input[CONF_MIN_EVAL_DAYS]),
                    CONF_SHADING_LEARN: bool(user_input[CONF_SHADING_LEARN]),
                    CONF_SHADING_APPLY: bool(user_input[CONF_SHADING_APPLY]),
                    CONF_HORIZON_SOURCES: [
                        s for s in user_input.get(CONF_HORIZON_SOURCES, []) if s in sources
                    ],
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_UPDATE_INTERVAL,
                        default=self._current(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                    ): _update_interval_selector(),
                    vol.Optional(
                        CONF_EXCLUSION_FACTOR,
                        default=self._current(
                            CONF_EXCLUSION_FACTOR, calc.DEFAULT_EXCLUSION_FACTOR
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1.0, max=5.0, step=0.1, mode="box")
                    ),
                    vol.Optional(
                        CONF_MIN_EVAL_DAYS,
                        default=self._current(CONF_MIN_EVAL_DAYS, calc.DEFAULT_MIN_EVAL_DAYS),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=3, max=14, step=1, mode="box")
                    ),
                    vol.Optional(
                        CONF_SHADING_LEARN,
                        default=self._current(CONF_SHADING_LEARN, DEFAULT_SHADING_LEARN),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_SHADING_APPLY,
                        default=self._current(CONF_SHADING_APPLY, DEFAULT_SHADING_APPLY),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_HORIZON_SOURCES,
                        default=[
                            s for s in self._current(CONF_HORIZON_SOURCES, [])
                            if s in sources
                        ],
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=s, label=SOURCE_NAMES[s])
                                for s in sources
                            ],
                            multiple=True,
                        )
                    ),
                }
            ),
        )
