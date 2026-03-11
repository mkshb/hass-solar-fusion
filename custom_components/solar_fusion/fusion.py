"""
Forecast Fusion Engine for Solar Fusion.

Combines multiple SourceReadings using adaptive weighted averaging,
bias correction and historical RMSE analysis.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from .const import (
    ALL_SOURCES,
    HISTORY_WINDOW_DAYS,
    MIN_HISTORY_DAYS,
    SOURCE_NAMES,
)
from .source_reader import HourlyWh, SourceReading

_LOGGER = logging.getLogger(__name__)

# One history record per (date, source):
# {"date": "YYYY-MM-DD", "source": str, "forecast_kwh": float, "actual_kwh": float}
HistoryRecord = Dict


class FusionEngine:
    """
    Adaptive weighted ensemble of PV forecast sources.

    Algorithm
    ---------
    1. Collect past daily totals per source and compare to actual production.
    2. Compute RMSE per source over the last HISTORY_WINDOW_DAYS days.
    3. Weight = 1 / RMSE  (lower error → higher weight).
    4. Normalise weights to sum to 1; fall back to equal weights when
       fewer than MIN_HISTORY_DAYS records exist for any source.
    5. Apply multiplicative bias correction per source before combining.
    6. Fuse hourly Wh using weighted average.
    7. Return fused forecast + uncertainty (weighted spread of sources as %).
    """

    def __init__(self, history: List[HistoryRecord]) -> None:
        self._history = history  # mutated in-place by coordinator

    # ──────────────────────────────────────────────────────────────────────────
    # Public
    # ──────────────────────────────────────────────────────────────────────────

    def fuse(
        self,
        readings: List[SourceReading],
        target_date: date,
    ) -> Tuple[HourlyWh, float, Dict[str, float]]:
        """
        Fuse SourceReadings for target_date.

        Returns
        -------
        fused_hourly    – {ISO-hour-str: Wh}
        uncertainty_pct – weighted spread of sources as % of fused daily total
        weights         – {source_id: normalised weight} actually applied
        """
        if not readings:
            return {}, 0.0, {}

        source_ids = [r.source_id for r in readings]
        weights = self._compute_weights(source_ids)
        bias = self._compute_bias_corrections(source_ids)

        # Collect hour slots: slot → {source_id: bias-corrected Wh}
        date_str = target_date.isoformat()
        slots: Dict[str, Dict[str, float]] = {}

        for reading in readings:
            b = bias.get(reading.source_id, 1.0)
            hourly = reading.hourly_today if target_date == date.today() else reading.hourly_tomorrow
            for slot, wh in hourly.items():
                if not slot.startswith(date_str):
                    continue
                slots.setdefault(slot, {})[reading.source_id] = max(0.0, wh * b)

        # Fallback: if no source provided hourly data, synthesise a single
        # daily-total slot from the weighted daily kWh values.
        # This ensures fused_today_kwh / fused_tomorrow_kwh are never 0.0
        # just because the upstream integration only exposes a daily sensor.
        if not slots:
            _LOGGER.debug(
                "No hourly data available for %s – using daily totals as fallback", date_str
            )
            daily_slot = f"{date_str}T00:00"
            for reading in readings:
                b = bias.get(reading.source_id, 1.0)
                daily_kwh = (
                    reading.today_kwh if target_date == date.today() else reading.tomorrow_kwh
                )
                slots[daily_slot] = slots.get(daily_slot, {})
                slots[daily_slot][reading.source_id] = max(0.0, daily_kwh * 1000.0 * b)

        # Weighted average per slot
        fused: HourlyWh = {}
        for slot, source_vals in slots.items():
            w_sum = sum(weights.get(s, 0.0) for s in source_vals)
            if w_sum == 0:
                fused[slot] = 0.0
                continue
            fused[slot] = round(
                sum(source_vals[s] * weights.get(s, 0.0) / w_sum for s in source_vals),
                1,
            )

        # Normalise fused hourly total to match weighted average of daily totals.
        #
        # Problem: if sources have different hourly coverage (e.g. Solcast covers
        # more hours than Forecast.Solar), slots with only one source get weight=1.0
        # instead of being averaged down, inflating the total.
        #
        # Fix: compute the weighted average of daily totals from the source readings,
        # then scale the fused hourly values proportionally so they sum to that target.
        target_wh = sum(
            (r.today_kwh if target_date == date.today() else r.tomorrow_kwh)
            * weights.get(r.source_id, 0.0)
            * 1000.0
            for r in readings
        )
        fused_total = sum(fused.values())
        if fused_total > 0 and target_wh > 0:
            scale = target_wh / fused_total
            fused = {slot: round(wh * scale, 1) for slot, wh in fused.items()}

        uncertainty_pct = self._compute_uncertainty(slots, weights, fused)
        return fused, uncertainty_pct, weights

    def record_actual(
        self,
        target_date: date,
        actual_kwh: float,
        readings: List[SourceReading],
    ) -> None:
        """
        Store actual production alongside each source's daily total for target_date.
        Called by the coordinator after midnight once yesterday's actual is available.
        """
        date_str = target_date.isoformat()
        # Remove stale records for this date to avoid duplicates
        self._history[:] = [r for r in self._history if r["date"] != date_str]

        for reading in readings:
            # Use the daily total directly from the reading (state value)
            forecast_kwh = (
                reading.today_kwh if target_date == date.today() else reading.tomorrow_kwh
            )
            self._history.append({
                "date": date_str,
                "source": reading.source_id,
                "forecast_kwh": round(float(forecast_kwh), 3),
                "actual_kwh": round(float(actual_kwh), 3),
            })

        _LOGGER.debug(
            "Recorded actual %.3f kWh for %s (%d sources)", actual_kwh, date_str, len(readings)
        )

    def source_quality(self) -> Dict[str, Dict]:
        """
        Per-source quality metrics for sensor exposure.

        Returns {source_id: {rmse, mae, bias, days_evaluated}}
        """
        cutoff = (date.today() - timedelta(days=HISTORY_WINDOW_DAYS)).isoformat()
        result = {}
        for source_id in ALL_SOURCES:
            records = [
                r for r in self._history
                if r["source"] == source_id and r["date"] >= cutoff
            ]
            if not records:
                result[source_id] = {
                    "rmse": None, "mae": None, "bias": None, "days_evaluated": 0
                }
                continue
            errors = [r["forecast_kwh"] - r["actual_kwh"] for r in records]
            rmse = math.sqrt(sum(e ** 2 for e in errors) / len(errors))
            mae = sum(abs(e) for e in errors) / len(errors)
            mean_bias = sum(errors) / len(errors)
            result[source_id] = {
                "rmse": round(rmse, 3),
                "mae": round(mae, 3),
                "bias": round(mean_bias, 3),
                "days_evaluated": len(records),
            }
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _compute_weights(self, source_ids: List[str]) -> Dict[str, float]:
        cutoff = (date.today() - timedelta(days=HISTORY_WINDOW_DAYS)).isoformat()
        rmse_map: Dict[str, Optional[float]] = {}

        for sid in source_ids:
            records = [
                r for r in self._history
                if r["source"] == sid and r["date"] >= cutoff
            ]
            if len(records) < MIN_HISTORY_DAYS:
                rmse_map[sid] = None
                continue
            errors = [r["forecast_kwh"] - r["actual_kwh"] for r in records]
            rmse_map[sid] = max(math.sqrt(sum(e ** 2 for e in errors) / len(errors)), 0.01)

        # Fall back to equal weights if any source lacks sufficient history
        if any(v is None for v in rmse_map.values()):
            n = len(source_ids)
            return {s: 1.0 / n for s in source_ids}

        inv = {s: 1.0 / v for s, v in rmse_map.items()}  # type: ignore[operator]
        total = sum(inv.values())
        return {s: v / total for s, v in inv.items()}

    def _compute_bias_corrections(self, source_ids: List[str]) -> Dict[str, float]:
        cutoff = (date.today() - timedelta(days=HISTORY_WINDOW_DAYS)).isoformat()
        corrections: Dict[str, float] = {}

        for sid in source_ids:
            records = [
                r for r in self._history
                if r["source"] == sid and r["date"] >= cutoff and r["forecast_kwh"] > 0
            ]
            if len(records) < MIN_HISTORY_DAYS:
                corrections[sid] = 1.0
                continue
            mean_fc = sum(r["forecast_kwh"] for r in records) / len(records)
            mean_ac = sum(r["actual_kwh"] for r in records) / len(records)
            factor = (mean_ac / mean_fc) if mean_fc > 0 else 1.0
            corrections[sid] = max(0.6, min(1.4, factor))  # cap ±40 %

        return corrections

    def _compute_uncertainty(
        self,
        slots: Dict[str, Dict[str, float]],
        weights: Dict[str, float],
        fused: HourlyWh,
    ) -> float:
        fused_total = sum(fused.values())
        if fused_total == 0 or not fused:
            return 0.0

        variances = []
        for slot, source_vals in slots.items():
            if slot not in fused:
                continue
            fused_val = fused[slot]
            w_sum = sum(weights.get(s, 0.0) for s in source_vals)
            if w_sum == 0:
                continue
            var = sum(
                weights.get(s, 0.0) / w_sum * (wh - fused_val) ** 2
                for s, wh in source_vals.items()
            )
            variances.append(var)

        if not variances:
            return 0.0

        mean_fused = fused_total / len(fused)
        std = math.sqrt(sum(variances) / len(variances))
        return round(min(std / mean_fused * 100, 100.0), 1) if mean_fused > 0 else 0.0
