"""DataUpdateCoordinator for Solar Fusion."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_PV_ENTITY,
    CONF_PV_ENTITIES,
    CONF_SOURCES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    SOURCE_NAMES,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .fusion import FusionEngine
from .source_reader import SourceReading, SourceUnavailable, read_source

_LOGGER = logging.getLogger(__name__)

# Hour at which the morning forecast snapshot is taken
_SNAPSHOT_HOUR = 6


class SolarForecastCoordinator(DataUpdateCoordinator):
    """
    Orchestrates reading, fusing, and persisting solar forecast data.

    Morning snapshot
    ----------------
    At 06:00 each day the coordinator stores the current "today" forecast from
    every source as the reference forecast for RMSE calculation.  This avoids
    the "cheating" effect where providers refine their same-day forecast
    throughout the day, making the evening value artificially close to actual.

    After midnight the stored morning snapshot is compared against actual
    production and written to history.  Only the 06:00 value is used; intraday
    updates are ignored for accuracy tracking.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._config = entry.data
        self._entry = entry
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY + "_" + entry.entry_id)
        self._history: List[Dict] = []
        self._fusion: Optional[FusionEngine] = None

        # {date_iso: {source_id: forecast_kwh}}  – persisted in storage
        self._morning_snapshots: Dict[str, Dict[str, float]] = {}

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=self._config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            ),
        )

    async def async_setup(self) -> None:
        """Load persisted data and register time-based callbacks."""
        stored = await self._store.async_load()
        if stored:
            if "history" in stored:
                self._history = stored["history"]
                _LOGGER.debug("Loaded %d history records", len(self._history))
            if "morning_snapshots" in stored:
                self._morning_snapshots = stored["morning_snapshots"]
                _LOGGER.debug(
                    "Loaded morning snapshots for %d days",
                    len(self._morning_snapshots),
                )
        self._fusion = FusionEngine(self._history)

        # Register 06:00 snapshot trigger
        self.config_entry.async_on_unload(
            async_track_time_change(
                self.hass,
                self._async_take_morning_snapshot,
                hour=_SNAPSHOT_HOUR,
                minute=0,
                second=0,
            )
        )

        # If HA started after 06:00 today and we have no snapshot yet, take one now
        now = dt_util.now()
        today_str = now.date().isoformat()
        if now.hour >= _SNAPSHOT_HOUR and today_str not in self._morning_snapshots:
            _LOGGER.debug(
                "HA started after %02d:00 with no snapshot for today – will snapshot on next update",
                _SNAPSHOT_HOUR,
            )
            self._snapshot_pending = True
        else:
            self._snapshot_pending = False

    @callback
    def _async_take_morning_snapshot(self, now: datetime) -> None:
        """Triggered at 06:00 – schedule a snapshot on next data update."""
        _LOGGER.debug("06:00 trigger: morning snapshot will be taken on next update")
        self._snapshot_pending = True
        # Fire an immediate refresh so the snapshot is taken without waiting
        # for the next scheduled update interval
        self.hass.async_create_task(self.async_refresh())

    async def _async_update_data(self) -> Dict[str, Any]:
        """Read all configured source entities, fuse the results."""
        configured_sources: List[str] = self._config.get(CONF_SOURCES, [])
        entity_map: Dict[str, Dict] = self._config.get("entity_map", {})

        # ── 1. Read each source from HA state machine ──────────────────────
        readings: List[SourceReading] = []
        missing: List[str] = []

        for source_id in configured_sources:
            source_entity_map = entity_map.get(source_id, {})
            try:
                reading = read_source(self.hass, source_id, source_entity_map)
                readings.append(reading)
                _LOGGER.debug(
                    "Read %s: today=%.2f kWh, tomorrow=%.2f kWh",
                    SOURCE_NAMES.get(source_id, source_id),
                    reading.today_kwh,
                    reading.tomorrow_kwh,
                )
            except SourceUnavailable as err:
                _LOGGER.warning("%s", err)
                missing.append(source_id)

        if not readings:
            raise UpdateFailed(
                "No forecast sources available. "
                "Ensure at least one forecast integration is installed and has data."
            )

        # ── 2. Take morning snapshot if pending ────────────────────────────
        if getattr(self, "_snapshot_pending", False):
            self._take_morning_snapshot(readings)
            self._snapshot_pending = False

        # ── 3. Record yesterday's actuals if not yet done ──────────────────
        await self._async_maybe_record_yesterday(readings)

        # ── 4. Fuse forecasts ──────────────────────────────────────────────
        today = dt_util.now().date()
        tomorrow = today + timedelta(days=1)

        fused_today, unc_today, weights = self._fusion.fuse(readings, today)
        fused_tomorrow, unc_tomorrow, _ = self._fusion.fuse(readings, tomorrow)

        # ── 5. Persist ─────────────────────────────────────────────────────
        await self._store.async_save({
            "history": self._history,
            "morning_snapshots": self._morning_snapshots,
        })

        return {
            "fused_today": fused_today,
            "fused_tomorrow": fused_tomorrow,
            "fused_today_kwh": round(sum(fused_today.values()) / 1000, 3),
            "fused_tomorrow_kwh": round(sum(fused_tomorrow.values()) / 1000, 3),
            "uncertainty_pct": round((unc_today + unc_tomorrow) / 2, 1),
            "weights": weights,
            "source_quality": self._fusion.source_quality(),
            "raw_readings": {
                r.source_id: {
                    "today_kwh": r.today_kwh,
                    "tomorrow_kwh": r.tomorrow_kwh,
                }
                for r in readings
            },
            "active_sources": [r.source_id for r in readings],
            "missing_sources": missing,
            "last_updated": dt_util.now().isoformat(),
            "morning_snapshot": self._morning_snapshots.get(dt_util.now().date().isoformat(), {}),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Morning snapshot
    # ──────────────────────────────────────────────────────────────────────────

    def _take_morning_snapshot(self, readings: List[SourceReading]) -> None:
        """Store today's 06:00 forecast as reference for RMSE calculation."""
        today_str = dt_util.now().date().isoformat()
        snapshot = {r.source_id: r.today_kwh for r in readings}
        self._morning_snapshots[today_str] = snapshot
        _LOGGER.info(
            "Morning snapshot taken for %s: %s",
            today_str,
            {SOURCE_NAMES.get(k, k): f"{v:.2f} kWh" for k, v in snapshot.items()},
        )
        # Prune snapshots older than 30 days to keep storage clean
        cutoff = (dt_util.now().date() - timedelta(days=30)).isoformat()
        self._morning_snapshots = {
            d: v for d, v in self._morning_snapshots.items() if d >= cutoff
        }

    # ──────────────────────────────────────────────────────────────────────────
    # History recording
    # ──────────────────────────────────────────────────────────────────────────

    async def _async_maybe_record_yesterday(
        self, current_readings: List[SourceReading]
    ) -> None:
        """
        Record yesterday's actual production against the morning snapshot.
        Supports multiple PV sensors (summed). Skips recording if no morning
        snapshot exists for yesterday to avoid tainting RMSE history.
        """
        # Build list of PV entity IDs to read – prefer new multi-entity key,
        # fall back to legacy single-entity key for existing installations.
        pv_entities: List[str] = self._config.get(CONF_PV_ENTITIES) or []
        if not pv_entities and self._config.get(CONF_PV_ENTITY):
            pv_entities = [self._config[CONF_PV_ENTITY]]
        if not pv_entities:
            return

        daily_meter_entity = self._find_daily_meter_entity()

        yesterday = dt_util.now().date() - timedelta(days=1)
        date_str = yesterday.isoformat()

        if any(r["date"] == date_str for r in self._history):
            return  # already recorded

        # Prefer the integrated daily meter (most accurate, single entity)
        if daily_meter_entity:
            actual_kwh = await self._async_read_actual_from_history(daily_meter_entity, yesterday)
        else:
            actual_kwh = None

        # Fall back to summing individual PV sensors
        if actual_kwh is None:
            total = 0.0
            any_found = False
            for entity_id in pv_entities:
                kwh = await self._async_read_actual_from_history(entity_id, yesterday)
                if kwh is not None:
                    total += kwh
                    any_found = True
            actual_kwh = total if any_found else None

        if actual_kwh is None:
            _LOGGER.debug("No actual production data found for %s", date_str)
            return

        # Use morning snapshot as reference forecast – skip if none exists.
        # Falling back to current readings would reintroduce the "cheating" effect
        # (intraday-refined forecasts being used as the reference), so we prefer
        # to miss one day rather than record inaccurate history.
        morning = self._morning_snapshots.get(date_str)
        if not morning:
            _LOGGER.warning(
                "No morning snapshot for %s – skipping RMSE recording for this day",
                date_str,
            )
            return

        _LOGGER.debug(
            "Using morning snapshot for %s RMSE calculation: %s",
            date_str,
            morning,
        )
        reference_readings = [
            SourceReading(
                source_id=sid,
                today_kwh=kwh,
                tomorrow_kwh=0.0,
            )
            for sid, kwh in morning.items()
        ]

        self._fusion.record_actual(yesterday, actual_kwh, reference_readings)
        _LOGGER.info("Recorded actual %.3f kWh for %s", actual_kwh, date_str)

    async def async_take_snapshot_now(self) -> None:
        """Manually trigger a morning snapshot on the next coordinator update."""
        _LOGGER.info("Manual snapshot requested – will be taken on next update")
        self._snapshot_pending = True
        await self.async_refresh()

    @property
    def history(self) -> List[Dict]:
        """Public read-only view of the history records."""
        return self._history

    @property
    def morning_snapshots(self) -> Dict[str, Dict[str, float]]:
        """Public read-only view of the morning snapshots."""
        return self._morning_snapshots

    def _find_daily_meter_entity(self) -> Optional[str]:
        """Find our PVDailyMeterSensor in the entity registry."""
        target_unique_id = f"{self._entry.entry_id}_pv_daily_meter"
        try:
            from homeassistant.helpers import entity_registry as er
            registry = er.async_get(self.hass)
            return registry.async_get_entity_id("sensor", DOMAIN, target_unique_id)
        except Exception:  # noqa: BLE001
            return None

    async def _async_read_actual_from_history(
        self, entity_id: str, target_date: date
    ) -> Optional[float]:
        """
        Read actual PV production for target_date from the HA recorder.
        Uses the recorder's executor to avoid blocking the event loop.
        """
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

            # Use timezone-aware datetimes so the recorder query covers the
            # correct local calendar day. A naive datetime would be interpreted
            # as UTC, shifting the window by the local UTC offset (e.g. +1/+2 h
            # in Central Europe) and causing yesterday's production to be missed
            # or read from the wrong day.
            start = dt_util.start_of_local_day(
                datetime(target_date.year, target_date.month, target_date.day)
            )
            end = start + timedelta(days=1)

            instance = get_instance(self.hass)
            states = await instance.async_add_executor_job(
                get_significant_states,
                self.hass,
                start,
                end,
                [entity_id],
            )

            entity_states = states.get(entity_id, [])
            if not entity_states:
                return None

            # HA recorder includes the last known state *before* the query window
            # (include_start_time_state=True by default).  For daily-reset sensors
            # that carryover filters into the next day, so strip any state whose
            # last_updated timestamp predates our window start.
            entity_states = [s for s in entity_states if s.last_updated >= start]
            if not entity_states:
                return None

            unit = entity_states[-1].attributes.get("unit_of_measurement", "kWh")
            state_class = entity_states[-1].attributes.get("state_class", "")
            values = []
            for s in entity_states:
                try:
                    values.append(float(s.state))
                except (ValueError, TypeError):
                    pass

            if not values:
                return None

            if "kWh" in unit:
                if state_class == "total_increasing":
                    production = max(values) - min(values)
                else:
                    production = max(values)
            else:
                production = sum(values) / len(values) * 24 / 1000

            return max(0.0, round(production, 3))

        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not read recorder history for %s: %s", entity_id, err)
            return None