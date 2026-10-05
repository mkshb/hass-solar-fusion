"""DataUpdateCoordinator for Solar Fusion."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, Dict, List, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import calc
from .const import (
    CONF_EXCLUSION_FACTOR,
    CONF_HORIZON_SOURCES,
    CONF_MIN_EVAL_DAYS,
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
    SHADING_RETENTION_DAYS,
    SOURCE_NAMES,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .fusion import FusionEngine
from .migration import migrate_storage
from .source_reader import (
    SourceReading,
    SourceUnavailable,
    hourly_from_attributes,
    read_source,
    today_entity_id,
)

_LOGGER = logging.getLogger(__name__)

# Hour at which the morning forecast snapshot is taken
_SNAPSHOT_HOUR = 6
# Rückwirkendes Lernen: spätester Zeitpunkt (Stunde), bis zu dem ein Zustand
# der Quellentität noch als Morgenprognose gilt
_RETRO_LATEST_HOUR = 9


class _SolarFusionStore(Store):
    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        return migrate_storage(old_major_version, old_data)


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
        self._store = _SolarFusionStore(hass, STORAGE_VERSION, STORAGE_KEY + "_" + entry.entry_id)
        self._history: List[Dict] = []
        self._fusion: Optional[FusionEngine] = None

        # {date_iso: {"daily": {source_id: kWh}, "daily_corrected": {source_id: kWh},
        #             "hourly": {source_id: {"HH": Wh}}}}  – persisted in storage
        # "HH" ist die lokale Stunde ohne UTC-Offset. Beim Ende der Sommerzeit
        # fallen beide 02-Uhr-Stunden in einen Slot (summiert), beim Beginn fehlt
        # 02. Beides liegt nachts ohne Ertrag; Lernen und Fusion überspringen
        # Stunden mit Sonne unter dem Horizont ohnehin.
        self._morning_snapshots: Dict[str, Dict[str, Dict]] = {}
        # Verschattung – persisted:
        #   days:  {date_iso: {"actual": {"HH": Wh}, "forecast": {source_id: {"HH": Wh}},
        #                      "origin": "snapshot" | "recorder"}}
        #   cells: Karte (calc.learn_shading_map), used_days, last_run
        self._shading: Dict[str, Any] = {"days": {}, "cells": {}, "used_days": 0, "last_run": None}
        # {date_iso: letzter Versuch} – Tage ohne Stunden-Istwerte (Statistik
        # wird erst nach Stundenende geschrieben); erneuter Versuch nach einer Stunde
        self._shading_attempted: Dict[str, datetime] = {}
        # {source_id: bool} – letzte Kalibrierungs-Entscheidung (Hysterese) – persisted
        self._calibration_state: Dict[str, bool] = {}

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
            if "calibration_state" in stored:
                self._calibration_state = stored["calibration_state"]
            if "morning_snapshots" in stored:
                self._morning_snapshots = stored["morning_snapshots"]
                _LOGGER.debug(
                    "Loaded morning snapshots for %d days",
                    len(self._morning_snapshots),
                )
            if "shading" in stored:
                self._shading.update(stored["shading"])
        self._fusion = FusionEngine(
            self._history,
            exclusion_factor=float(
                self._config.get(CONF_EXCLUSION_FACTOR, calc.DEFAULT_EXCLUSION_FACTOR)
            ),
            min_eval_days=int(
                self._config.get(CONF_MIN_EVAL_DAYS, calc.DEFAULT_MIN_EVAL_DAYS)
            ),
            calibration_state=self._calibration_state,
            shading_cells=self._shading.get("cells") or {},
            shading_apply=self._shading_apply,
            horizon_sources=self._config.get(CONF_HORIZON_SOURCES, []),
            location=(self.hass.config.latitude, self.hass.config.longitude),
        )
        # Karte mit den aktuellen Einstellungen (z. B. Horizont-Quellen) neu lernen
        if self._shading["days"]:
            await self._async_relearn_shading()

        # Reconcile history against the recorder (fixes legacy carryover corruption)
        if self._history:
            self.hass.async_create_task(self._async_reconcile_history_on_startup())

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
        if self._config.get(CONF_SHADING_LEARN, DEFAULT_SHADING_LEARN):
            await self._async_maybe_learn_shading()

        # ── 4. Fuse forecasts ──────────────────────────────────────────────
        today = dt_util.now().date()
        tomorrow = today + timedelta(days=1)

        fused_today, unc_today, weights = self._fusion.fuse(readings, today)
        fused_tomorrow, unc_tomorrow, _ = self._fusion.fuse(readings, tomorrow)

        # Uncertainty is None when fewer than two sources are present (no
        # cross-validation). Average only the values that are defined.
        _uncs = [u for u in (unc_today, unc_tomorrow) if u is not None]
        uncertainty_pct = round(sum(_uncs) / len(_uncs), 1) if _uncs else None

        # ── 5. Persist ─────────────────────────────────────────────────────
        await self._async_save()

        return {
            "fused_today": fused_today,
            "fused_tomorrow": fused_tomorrow,
            "fused_today_kwh": round(sum(fused_today.values()) / 1000, 3),
            "fused_tomorrow_kwh": round(sum(fused_tomorrow.values()) / 1000, 3),
            "uncertainty_pct": uncertainty_pct,
            "weights": weights,
            # Je Quelle: weight, excluded, exclusion_reason, rmse_calibrated, days_evaluated
            "weight_details": self._fusion.compute_weights([r.source_id for r in readings]),
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
            "morning_snapshot": self.morning_snapshots.get(dt_util.now().date().isoformat(), {}),
            "shading_ratios": {
                d: dict(r) for d, r in self._fusion.last_shading_ratios.items()
                if d in (today.isoformat(), tomorrow.isoformat())
            },
        }

    async def _async_save(self) -> None:
        await self._store.async_save({
            "history": self._history,
            "morning_snapshots": self._morning_snapshots,
            "calibration_state": self._calibration_state,
            "shading": self._shading,
        })

    @property
    def _shading_apply(self) -> bool:
        return bool(self._config.get(CONF_SHADING_APPLY, DEFAULT_SHADING_APPLY))

    # ──────────────────────────────────────────────────────────────────────────
    # Morning snapshot
    # ──────────────────────────────────────────────────────────────────────────

    def _take_morning_snapshot(self, readings: List[SourceReading]) -> None:
        """Store today's 06:00 forecast as reference for RMSE calculation.

        Neben den Tagessummen werden die Stundenwerte je Quelle gespeichert
        (für das Verschattungslernen) und – wenn die Verschattung angewendet
        wird – die korrigierten Tagessummen (für Kalibrierung und Gewichtung).
        """
        today = dt_util.now().date()
        today_str = today.isoformat()
        daily = {r.source_id: r.today_kwh for r in readings}
        snapshot: Dict[str, Dict] = {
            "daily": daily,
            "hourly": {
                r.source_id: _hourly_by_hour(r.hourly_today, today_str)
                for r in readings
                if r.hourly_today
            },
        }
        if self._shading_apply:
            ratios = self._fusion.shading_ratios(readings, today)
            snapshot["daily_corrected"] = {
                sid: round(kwh * ratios.get(sid, 1.0), 3) for sid, kwh in daily.items()
            }
        self._morning_snapshots[today_str] = snapshot
        _LOGGER.info(
            "Morning snapshot taken for %s: %s",
            today_str,
            {SOURCE_NAMES.get(k, k): f"{v:.2f} kWh" for k, v in daily.items()},
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
            for sid, kwh in morning.get("daily", {}).items()
        ]

        self._fusion.record_actual(
            yesterday, actual_kwh, reference_readings,
            corrected=morning.get("daily_corrected"),
        )
        _LOGGER.info("Recorded actual %.3f kWh for %s", actual_kwh, date_str)

    async def async_take_snapshot_now(self) -> None:
        """Manually trigger a morning snapshot on the next coordinator update."""
        _LOGGER.info("Manual snapshot requested – will be taken on next update")
        self._snapshot_pending = True
        await self.async_refresh()

    async def async_repair_history(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Dict[str, int]:
        """Re-read actual production for history records from the HA recorder.

        Re-fetches the real production value for every date in the history
        (optionally filtered by date_from/date_to) and updates the stored
        actual_kwh.  Useful to fix carryover-corrupted records left behind by
        the utility-meter timing bug.

        Returns a dict with keys "repaired", "skipped", "unchanged".
        """
        pv_entities: List[str] = self._config.get(CONF_PV_ENTITIES) or []
        if not pv_entities and self._config.get(CONF_PV_ENTITY):
            pv_entities = [self._config[CONF_PV_ENTITY]]
        if not pv_entities:
            _LOGGER.warning("repair_history: no PV entities configured")
            return {"repaired": 0, "skipped": 0, "unchanged": 0}

        all_dates = sorted({r["date"] for r in self._history})
        if date_from:
            all_dates = [d for d in all_dates if d >= date_from]
        if date_to:
            all_dates = [d for d in all_dates if d <= date_to]

        repaired = 0
        skipped = 0
        unchanged = 0
        daily_meter = self._find_daily_meter_entity()

        for date_str in all_dates:
            target = date.fromisoformat(date_str)

            if daily_meter:
                actual_kwh = await self._async_read_actual_from_history(daily_meter, target)
            else:
                actual_kwh = None

            if actual_kwh is None:
                total = 0.0
                any_found = False
                for entity_id in pv_entities:
                    kwh = await self._async_read_actual_from_history(entity_id, target)
                    if kwh is not None:
                        total += kwh
                        any_found = True
                actual_kwh = total if any_found else None

            if actual_kwh is None:
                _LOGGER.debug("repair_history: no recorder data for %s – skipping", date_str)
                skipped += 1
                continue

            date_changed = False
            for record in self._history:
                if record["date"] == date_str:
                    if abs(record["actual_kwh"] - actual_kwh) > 0.001:
                        record["actual_kwh"] = round(actual_kwh, 3)
                        date_changed = True

            if date_changed:
                _LOGGER.info(
                    "repair_history: corrected actual for %s → %.3f kWh", date_str, actual_kwh
                )
                repaired += 1
            else:
                unchanged += 1

        if repaired > 0:
            await self._async_save()
            if self._fusion:
                self._fusion._iso_cache.clear()
            # Rebuild the fused forecast so sensors reflect the corrected history
            # immediately instead of waiting for the next scheduled update.
            await self.async_request_refresh()

        _LOGGER.info(
            "repair_history: %d corrected, %d skipped (no recorder data), %d unchanged",
            repaired, skipped, unchanged,
        )
        return {"repaired": repaired, "skipped": skipped, "unchanged": unchanged}

    @property
    def history(self) -> List[Dict]:
        """Public read-only view of the history records."""
        return self._history

    @property
    def morning_snapshots(self) -> Dict[str, Dict[str, float]]:
        """Tagessummen der Morgen-Snapshots: {date: {source_id: kWh}}."""
        return {d: v.get("daily", {}) for d, v in self._morning_snapshots.items()}

    @property
    def morning_snapshots_full(self) -> Dict[str, Dict[str, Dict]]:
        """Morgen-Snapshots inklusive Stundenwerten und korrigierten Summen."""
        return self._morning_snapshots

    @property
    def shading(self) -> Dict[str, Any]:
        """Verschattungsdaten (Tage, Karte, Lernstand)."""
        return self._shading

    @property
    def shading_settings(self) -> Dict[str, Any]:
        return {
            "learn": bool(self._config.get(CONF_SHADING_LEARN, DEFAULT_SHADING_LEARN)),
            "apply": self._shading_apply,
            "horizon_sources": list(self._config.get(CONF_HORIZON_SOURCES, [])),
            "active": bool(self._fusion and self._fusion.shading_active),
        }

    async def _async_reconcile_history_on_startup(self) -> None:
        """Reconcile every history record against the recorder on startup.

        Earlier versions corrupted actual_kwh via a utility-meter carryover bug
        (yesterday's daily total leaking into today's recording). Rather than
        guess which dates are affected with a duplicate heuristic – which misses
        near-duplicates and one-day lags – we unconditionally re-read each stored
        date from the recorder. The read path is now carryover-safe, so this is
        idempotent: dates still in the recorder are corrected, purged dates
        return no data and are left untouched.
        """
        await self.async_repair_history()

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
                    # Sum positive deltas (carryover-safe); see calc for details.
                    production = calc.daily_total_from_increasing(values)
                else:
                    production = max(values)
            else:
                production = sum(values) / len(values) * 24 / 1000

            return max(0.0, round(production, 3))

        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not read recorder history for %s: %s", entity_id, err)
            return None
    # ──────────────────────────────────────────────────────────────────────────
    # Verschattung lernen
    # ──────────────────────────────────────────────────────────────────────────

    def _pv_entities(self) -> List[str]:
        pv_entities: List[str] = self._config.get(CONF_PV_ENTITIES) or []
        if not pv_entities and self._config.get(CONF_PV_ENTITY):
            pv_entities = [self._config[CONF_PV_ENTITY]]
        return pv_entities

    async def _async_read_hourly_actual(self, target_date: date) -> Optional[Dict[str, float]]:
        """Stündlicher Ist-Ertrag eines Tages aus der Recorder-Langzeitstatistik.

        {"HH": Wh}, HH = lokale Stunde des Periodenbeginns. Zuerst die Summe
        der konfigurierten PV-Sensoren, nur ohne deren Statistik der eigene
        Tageszähler: Dessen Statistik spiegelt die Sensoren, die damals
        eingestellt waren – nach einer Korrektur in den Optionen würde das
        rückwirkende Lernen sonst weiter die alten Werte lesen. Rücksetzungen
        behandelt ``change`` ohnehin. Energiezähler liefern ``change`` (kWh),
        Leistungssensoren ``mean`` (W ≙ Wh je Stunde).
        """
        pv_entities = self._pv_entities()
        if pv_entities:
            result = await self._async_hourly_statistics(pv_entities, target_date)
            if result:
                return result
        daily_meter = self._find_daily_meter_entity()
        if not daily_meter:
            return None
        return await self._async_hourly_statistics([daily_meter], target_date)

    async def _async_hourly_statistics(
        self, entity_ids: List[str], target_date: date
    ) -> Optional[Dict[str, float]]:
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )

            start = dt_util.start_of_local_day(
                datetime(target_date.year, target_date.month, target_date.day)
            )
            end = dt_util.start_of_local_day(
                datetime(target_date.year, target_date.month, target_date.day)
                + timedelta(days=1)
            )
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                end,
                set(entity_ids),
                "hour",
                {"energy": "kWh", "power": "W"},
                {"change", "mean"},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not read hourly statistics for %s: %s", entity_ids, err)
            return None

        total: Dict[str, float] = {}
        found = False
        for entity_id in entity_ids:
            for row in stats.get(entity_id, []):
                begin = row.get("start")
                if isinstance(begin, (int, float)):
                    begin = dt_util.utc_from_timestamp(begin)
                if begin is None:
                    continue
                local = dt_util.as_local(begin)
                if local.date() != target_date:
                    continue
                if row.get("change") is not None:
                    wh = max(0.0, float(row["change"])) * 1000.0
                elif row.get("mean") is not None:
                    wh = max(0.0, float(row["mean"]))
                else:
                    continue
                key = f"{local.hour:02d}"
                total[key] = total.get(key, 0.0) + wh
                found = True
        if not found:
            return None
        return {k: round(v, 1) for k, v in sorted(total.items())}

    def _night_production_wh(self, day: str, actual: Dict[str, float]) -> float:
        """Ertrag in Stunden, in denen die Sonne durchgehend unter dem Horizont steht."""
        tz = dt_util.get_default_time_zone()
        lat, lon = self.hass.config.latitude, self.hass.config.longitude
        total = 0.0
        for hh, wh in actual.items():
            start = datetime.fromisoformat(f"{day}T{hh}:00").replace(tzinfo=tz)
            positions = calc.slot_sun_positions(start, lat, lon, quarters=True)
            if all(el < calc.SHADING_NIGHT_ELEVATION for _, el in positions):
                total += wh
        return total

    def _check_night_production(self, day: str, actual: Dict[str, float]) -> Optional[str]:
        """Warnung, wenn der PV-Sensor nachts Ertrag meldet (z. B. Wechselrichter-AC inkl. Batterie)."""
        night = self._night_production_wh(day, actual)
        if night <= calc.SHADING_MAX_NIGHT_WH:
            return None
        msg = (
            f"PV sensor reports {night:.0f} Wh at night on {day} – is it the inverter "
            f"AC output including battery discharge? Shading cannot be learned from "
            f"such values; configure a PV production sensor ({self._pv_entities()})"
        )
        _LOGGER.warning("Shading: %s", msg)
        return msg

    async def _async_maybe_learn_shading(self) -> None:
        """Abgeschlossene Tage mit Stunden-Snapshot ins Verschattungslernen übernehmen."""
        now = dt_util.now()
        today_str = now.date().isoformat()
        retry_before = now - timedelta(hours=1)
        pending = [
            d for d, snap in sorted(self._morning_snapshots.items())
            if d < today_str
            and snap.get("hourly")
            and d not in self._shading["days"]
            and self._shading_attempted.get(d, retry_before) <= retry_before
        ]
        if not pending:
            return
        added = 0
        for d in pending:
            actual = await self._async_read_hourly_actual(date.fromisoformat(d))
            if not actual:
                self._shading_attempted[d] = now
                _LOGGER.debug("Shading: no hourly actuals for %s yet", d)
                continue
            self._check_night_production(d, actual)
            self._shading["days"][d] = {
                "actual": actual,
                "forecast": self._morning_snapshots[d]["hourly"],
                "origin": "snapshot",
            }
            added += 1
        if added:
            await self._async_relearn_shading()

    async def _async_relearn_shading(self) -> None:
        """Karte aus allen gespeicherten Tagen neu lernen und an die Fusion geben."""
        cutoff = (dt_util.now().date() - timedelta(days=SHADING_RETENTION_DAYS)).isoformat()
        self._shading["days"] = {
            d: v for d, v in self._shading["days"].items() if d >= cutoff
        }
        days = await self.hass.async_add_executor_job(
            _shading_learning_input,
            self._shading["days"],
            self.hass.config.latitude,
            self.hass.config.longitude,
            dt_util.get_default_time_zone(),
        )
        cells, used = calc.learn_shading_map(
            days, excluded_sources=self._config.get(CONF_HORIZON_SOURCES, [])
        )
        self._shading["cells"] = cells
        self._shading["used_days"] = used
        self._shading["last_run"] = dt_util.now().isoformat()
        if self._fusion:
            self._fusion.set_shading_cells(cells)
        _LOGGER.info(
            "Shading map learned from %d of %d days: %d cells learned, %d below 0.9",
            used,
            len(days),
            sum(1 for c in cells.values() if c.get("learned")),
            sum(1 for c in cells.values() if c.get("learned") and c["factor"] < 0.9),
        )

    async def async_learn_shading(self, days: int = 10) -> Dict[str, Any]:
        """Rückwirkend lernen: Morgenprognosen der letzten ``days`` Tage aus dem Recorder.

        Rekonstruiert je Tag die Stundenprognose aus dem Zustand der
        Heute-Entität jeder Quelle um 06:00 (oder dem ersten Zustand bis
        _RETRO_LATEST_HOUR Uhr) und liest den Stunden-Ist aus der
        Langzeitstatistik. Geht nur, solange die Zustands-Historie reicht
        (Standard 10 Tage) und nur für Quellen, deren Stundenattribut
        aufgezeichnet wird (Open-Meteo ja, Solcast nein). Tage mit
        Stunden-Snapshot werden nicht überschrieben.
        """
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import get_significant_states

        entity_map: Dict[str, Dict] = self._config.get("entity_map", {})
        entities = {
            sid: today_entity_id(self.hass, sid, entity_map.get(sid, {}))
            for sid in self._config.get(CONF_SOURCES, [])
        }
        entities = {sid: eid for sid, eid in entities.items() if eid}
        today = dt_util.now().date()
        summary: Dict[str, Any] = {"added": [], "skipped": {}, "warnings": []}

        for back in range(days, 0, -1):
            day = today - timedelta(days=back)
            d = day.isoformat()
            existing = self._shading["days"].get(d)
            if existing and existing.get("origin") == "snapshot":
                summary["skipped"][d] = "snapshot"
                continue
            day_start = dt_util.start_of_local_day(datetime(day.year, day.month, day.day))
            start = day_start + timedelta(hours=_SNAPSHOT_HOUR)
            end = day_start + timedelta(hours=_RETRO_LATEST_HOUR)
            try:
                states = await get_instance(self.hass).async_add_executor_job(
                    partial(
                        get_significant_states,
                        self.hass,
                        start,
                        end,
                        list(entities.values()),
                        include_start_time_state=True,
                        significant_changes_only=False,
                        minimal_response=False,
                        no_attributes=False,
                    )
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("learn_shading: recorder query for %s failed: %s", d, err)
                summary["skipped"][d] = "recorder error"
                continue

            forecast: Dict[str, Dict[str, float]] = {}
            for sid, eid in entities.items():
                for st in states.get(eid, []):
                    hourly = _hourly_by_hour(hourly_from_attributes(sid, st.attributes), d)
                    if any(v > 0 for v in hourly.values()):
                        forecast[sid] = hourly
                        break
            if not forecast:
                summary["skipped"][d] = "no hourly forecast in recorder"
                continue
            actual = await self._async_read_hourly_actual(day)
            if not actual:
                summary["skipped"][d] = "no hourly statistics"
                continue
            warning = self._check_night_production(d, actual)
            if warning:
                summary["warnings"].append(warning)
            self._shading["days"][d] = {
                "actual": actual,
                "forecast": forecast,
                "origin": "recorder",
            }
            summary["added"].append({"date": d, "sources": sorted(forecast)})

        await self._async_relearn_shading()
        await self._async_save()
        await self.async_request_refresh()
        cells = self._shading["cells"]
        summary["used_days"] = self._shading["used_days"]
        summary["learned_cells"] = sum(1 for c in cells.values() if c.get("learned"))
        _LOGGER.info("learn_shading: %s", summary)
        return summary


def _hourly_by_hour(hourly: Dict[str, float], date_str: str) -> Dict[str, float]:
    """{"YYYY-MM-DDTHH:00": Wh} eines Tages → {"HH": Wh} (kompakt für die Speicherung)."""
    return {
        slot[11:13]: round(float(wh), 1)
        for slot, wh in hourly.items()
        if slot.startswith(date_str)
    }


def _shading_learning_input(days: Dict[str, Dict], lat: float, lon: float, tz) -> List[List[Dict]]:
    """Gespeicherte Tage → Eingabe für calc.learn_shading_map (Sonnenstand zur Slotmitte)."""
    out: List[List[Dict]] = []
    for d, day in sorted(days.items()):
        actual = day.get("actual", {})
        forecast = day.get("forecast", {})
        hours_keys = sorted({h for fc in forecast.values() for h in fc})
        hours: List[Dict] = []
        for hh in hours_keys:
            if hh not in actual:
                continue
            start = datetime.fromisoformat(f"{d}T{hh}:00").replace(tzinfo=tz)
            (az, el), = calc.slot_sun_positions(start, lat, lon)
            if el <= 0:
                continue
            hours.append({
                "az": az,
                "el": el,
                "actual": actual[hh],
                "forecast": {sid: fc[hh] for sid, fc in forecast.items() if hh in fc},
            })
        if hours:
            out.append(hours)
    return out
