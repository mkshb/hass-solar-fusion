"""
Forecast Fusion Engine for Solar Fusion.

Combines multiple SourceReadings using adaptive weighted averaging with:
  - Seasonal bias segmentation  (per-month bias/RMSE windows)
  - Isotonic regression calibration  (monotone, non-linear correction)
  - Adaptive RMSE weighting
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

# Minimum records needed to fit isotonic regression (otherwise linear bias)
MIN_ISO_POINTS = 20

# Seasonal window: how many calendar months either side to include
SEASONAL_MONTH_RADIUS = 1

# One history record per (date, source):
# {"date": "YYYY-MM-DD", "source": str, "forecast_kwh": float, "actual_kwh": float}
HistoryRecord = Dict


# ──────────────────────────────────────────────────────────────────────────────
# Isotonic regression (pool-adjacent-violators algorithm)
# ──────────────────────────────────────────────────────────────────────────────

def _isotonic_regression(x: List[float], y: List[float]) -> Tuple[List[float], List[float]]:
    """
    Fit a monotone non-decreasing step function to (x, y) pairs.

    Returns (knots_x, knots_y) – the fitted step function.
    Uses the pool-adjacent-violators algorithm; no external dependencies.
    """
    if not x:
        return [], []

    pairs = sorted(zip(x, y), key=lambda p: p[0])
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    blocks: List[List[float]] = [[v] for v in ys]
    block_xs: List[List[float]] = [[v] for v in xs]

    i = 0
    while i < len(blocks) - 1:
        if _block_mean(blocks[i]) > _block_mean(blocks[i + 1]):
            blocks[i] = blocks[i] + blocks[i + 1]
            block_xs[i] = block_xs[i] + block_xs[i + 1]
            blocks.pop(i + 1)
            block_xs.pop(i + 1)
            if i > 0:
                i -= 1
        else:
            i += 1

    knots_x = [sum(bx) / len(bx) for bx in block_xs]
    knots_y = [_block_mean(b) for b in blocks]
    return knots_x, knots_y


def _block_mean(block: List[float]) -> float:
    return sum(block) / len(block)


def _isotonic_predict(knots_x: List[float], knots_y: List[float], value: float) -> float:
    """
    Predict calibrated value by interpolating between isotonic knots.
    Extrapolates flat outside the knot range.
    """
    if not knots_x:
        return value
    if value <= knots_x[0]:
        return knots_y[0]
    if value >= knots_x[-1]:
        return knots_y[-1]
    for i in range(len(knots_x) - 1):
        if knots_x[i] <= value <= knots_x[i + 1]:
            t = (value - knots_x[i]) / (knots_x[i + 1] - knots_x[i])
            return knots_y[i] + t * (knots_y[i + 1] - knots_y[i])
    return knots_y[-1]


# ──────────────────────────────────────────────────────────────────────────────
# Seasonal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _seasonal_records(
    history: List[HistoryRecord],
    source_id: str,
    ref_month: int,
    radius: int = SEASONAL_MONTH_RADIUS,
) -> List[HistoryRecord]:
    """
    Return history records for source_id whose calendar month is within
    `radius` months of ref_month (wraps around year boundaries).
    Uses all available history for seasonal calibration (no recency cutoff).
    """
    months = {((ref_month - 1 + delta) % 12) + 1 for delta in range(-radius, radius + 1)}
    return [
        r for r in history
        if r["source"] == source_id and int(r["date"][5:7]) in months
    ]


def _recent_records(
    history: List[HistoryRecord],
    source_id: str,
    window_days: int = HISTORY_WINDOW_DAYS,
) -> List[HistoryRecord]:
    """Return the most recent `window_days` records for source_id."""
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    return [r for r in history if r["source"] == source_id and r["date"] >= cutoff]


# ──────────────────────────────────────────────────────────────────────────────
# Solar profile helper
# ──────────────────────────────────────────────────────────────────────────────

def _build_solar_profile(
    readings: List[SourceReading], today: date
) -> Dict[str, float]:
    """
    Build a normalised hourly shape (fractions summing to 1.0) for a day.

    Priority:
    1. Average of all available hourly_today profiles from readings.
    2. Generic Gaussian bell-curve (peak at 13:00, sigma ~3h).
    """
    today_str = today.isoformat()
    combined: Dict[str, float] = {}
    contributing_sources = 0

    for reading in readings:
        hourly = reading.hourly_today or {}
        day_slots = {k: v for k, v in hourly.items() if k.startswith(today_str)}
        if not day_slots:
            continue
        total = sum(day_slots.values())
        if total <= 0:
            continue
        contributing_sources += 1
        for slot, wh in day_slots.items():
            combined[slot] = combined.get(slot, 0.0) + wh / total

    if combined and contributing_sources > 0:
        # Average across contributing sources, then normalise
        total = sum(combined.values())
        if total > 0:
            return {slot: v / total for slot, v in combined.items()}

    # Fallback: generic Gaussian bell curve (peak ~13:00 local, sigma 3h)
    peak_h = 13.0
    sigma = 3.0
    profile: Dict[str, float] = {}
    for h in range(24):
        slot = f"{today_str}T{h:02d}:00"
        profile[slot] = math.exp(-0.5 * ((h - peak_h) / sigma) ** 2)
    total = sum(profile.values())
    return {slot: v / total for slot, v in profile.items()}


# ──────────────────────────────────────────────────────────────────────────────
# FusionEngine
# ──────────────────────────────────────────────────────────────────────────────

class FusionEngine:
    """
    Adaptive weighted ensemble of PV forecast sources.

    Algorithm
    ---------
    1. Collect past daily totals per source and compare to actual production.
    2. Compute seasonal RMSE per source (months within +/-1 of current month).
    3. Weight = 1 / seasonal_RMSE  (lower error -> higher weight).
    4. Normalise weights to sum to 1; fall back to equal weights when
       fewer than MIN_HISTORY_DAYS records exist.
    5. Apply isotonic regression calibration per source before combining:
       - If >= MIN_ISO_POINTS seasonal records exist: isotonic curve
       - Else if >= MIN_HISTORY_DAYS records: linear multiplicative bias
       - Else: no correction (factor 1.0)
    6. Fuse hourly Wh using weighted average.
    7. Normalise fused hourly total to weighted average of calibrated daily totals.
    8. Return fused forecast + uncertainty (weighted spread of sources as %).
    """

    def __init__(self, history: List[HistoryRecord]) -> None:
        self._history = history  # mutated in-place by coordinator
        # Cache: {source_id: (knots_x, knots_y, fitted_month)}
        self._iso_cache: Dict[str, Tuple[List[float], List[float], int]] = {}

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
        fused_hourly    -- {ISO-hour-str: Wh}
        uncertainty_pct -- weighted spread of sources as % of fused daily total
        weights         -- {source_id: normalised weight} actually applied
        """
        if not readings:
            return {}, 0.0, {}

        current_month = target_date.month
        source_ids = [r.source_id for r in readings]
        weights = self._compute_weights(source_ids, current_month)

        date_str = target_date.isoformat()
        slots: Dict[str, Dict[str, float]] = {}

        for reading in readings:
            raw_daily = (
                reading.today_kwh if target_date == date.today() else reading.tomorrow_kwh
            )
            calibrated_daily = self._calibrate(reading.source_id, raw_daily, current_month)
            hourly_scale = (calibrated_daily / raw_daily) if raw_daily > 0 else 1.0

            hourly = (
                reading.hourly_today if target_date == date.today() else reading.hourly_tomorrow
            )
            for slot, wh in hourly.items():
                if not slot.startswith(date_str):
                    continue
                slots.setdefault(slot, {})[reading.source_id] = max(0.0, wh * hourly_scale)

        if not slots:
            _LOGGER.debug(
                "No hourly data for %s – using calibrated daily totals as fallback", date_str
            )
            # Build a solar-shape profile from today's data (or generic bell curve)
            # and distribute the daily total across 24 hourly slots.
            profile = _build_solar_profile(readings, date.today())

            for reading in readings:
                raw_kwh = (
                    reading.today_kwh if target_date == date.today() else reading.tomorrow_kwh
                )
                calibrated_kwh = self._calibrate(reading.source_id, raw_kwh, current_month)
                target_wh = max(0.0, calibrated_kwh * 1000.0)
                for slot, fraction in profile.items():
                    # Rewrite the date part from today to target_date
                    day_slot = slot.replace(date.today().isoformat(), date_str)
                    slots.setdefault(day_slot, {})[reading.source_id] = round(
                        target_wh * fraction, 1
                    )

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

        target_wh = sum(
            self._calibrate(
                r.source_id,
                r.today_kwh if target_date == date.today() else r.tomorrow_kwh,
                current_month,
            )
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
        Invalidates the isotonic cache for the affected month.
        """
        date_str = target_date.isoformat()
        month = target_date.month

        self._history[:] = [r for r in self._history if r["date"] != date_str]

        for reading in readings:
            # When called from _async_maybe_record_yesterday, readings come from
            # the morning snapshot where today_kwh holds the forecast for target_date
            # and tomorrow_kwh is always 0.0. Using today_kwh is always correct here.
            forecast_kwh = reading.today_kwh
            self._history.append({
                "date": date_str,
                "source": reading.source_id,
                "forecast_kwh": round(float(forecast_kwh), 3),
                "actual_kwh": round(float(actual_kwh), 3),
            })

        # Invalidate isotonic cache for sources whose seasonal window includes this month
        for sid in {r.source_id for r in readings}:
            cached = self._iso_cache.get(sid)
            if cached:
                cached_month = cached[2]
                affected = {
                    ((cached_month - 1 + d) % 12) + 1
                    for d in range(-SEASONAL_MONTH_RADIUS, SEASONAL_MONTH_RADIUS + 1)
                }
                if month in affected:
                    del self._iso_cache[sid]
                    _LOGGER.debug(
                        "Invalidated isotonic cache for %s (month %d)", sid, month
                    )

        _LOGGER.debug(
            "Recorded actual %.3f kWh for %s (%d sources)", actual_kwh, date_str, len(readings)
        )

    def source_quality(self) -> Dict[str, Dict]:
        """
        Per-source quality metrics for sensor exposure.
        Uses seasonal window for the current month.

        Returns {source_id: {rmse, mae, bias, days_evaluated, calibration_mode}}
        """
        current_month = date.today().month
        result = {}

        for source_id in ALL_SOURCES:
            seasonal = _seasonal_records(self._history, source_id, current_month)
            recent = _recent_records(self._history, source_id)

            if not recent:
                result[source_id] = {
                    "rmse": None, "mae": None, "bias": None,
                    "days_evaluated": 0, "calibration_mode": "none",
                }
                continue

            errors = [r["forecast_kwh"] - r["actual_kwh"] for r in recent]
            rmse = math.sqrt(sum(e ** 2 for e in errors) / len(errors))
            mae = sum(abs(e) for e in errors) / len(errors)
            mean_bias = sum(errors) / len(errors)

            if len(seasonal) >= MIN_ISO_POINTS:
                cal_mode = f"isotonic ({len(seasonal)} seasonal pts)"
            elif len(recent) >= MIN_HISTORY_DAYS:
                cal_mode = f"linear_bias ({len(recent)} recent pts)"
            else:
                cal_mode = "none (insufficient data)"

            result[source_id] = {
                "rmse": round(rmse, 3),
                "mae": round(mae, 3),
                "bias": round(mean_bias, 3),
                "days_evaluated": len(recent),
                "calibration_mode": cal_mode,
            }

        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Calibration
    # ──────────────────────────────────────────────────────────────────────────

    def _calibrate(self, source_id: str, raw_kwh: float, month: int) -> float:
        """
        Return calibrated kWh for a raw forecast value.

        Priority:
        1. Isotonic regression (seasonal, >= MIN_ISO_POINTS records)
        2. Linear multiplicative bias (recent, >= MIN_HISTORY_DAYS records)
        3. Identity (no correction)
        """
        seasonal = _seasonal_records(self._history, source_id, month)

        if len(seasonal) >= MIN_ISO_POINTS:
            return self._calibrate_isotonic(source_id, raw_kwh, seasonal, month)

        recent = _recent_records(self._history, source_id)
        if len(recent) >= MIN_HISTORY_DAYS:
            return self._calibrate_linear(raw_kwh, recent)

        return max(0.0, raw_kwh)

    def _calibrate_isotonic(
        self,
        source_id: str,
        raw_kwh: float,
        records: List[HistoryRecord],
        month: int,
    ) -> float:
        """Apply isotonic regression calibration, using a per-source cache."""
        cached = self._iso_cache.get(source_id)
        if cached is None or cached[2] != month:
            xs = [r["forecast_kwh"] for r in records]
            ys = [r["actual_kwh"] for r in records]
            knots_x, knots_y = _isotonic_regression(xs, ys)
            self._iso_cache[source_id] = (knots_x, knots_y, month)
            _LOGGER.debug(
                "Fitted isotonic regression for %s month=%d: %d knots from %d points",
                source_id, month, len(knots_x), len(records),
            )
        else:
            knots_x, knots_y, _ = cached

        return max(0.0, _isotonic_predict(knots_x, knots_y, raw_kwh))

    def _calibrate_linear(self, raw_kwh: float, records: List[HistoryRecord]) -> float:
        """Apply multiplicative linear bias correction, capped at +/-40%."""
        valid = [r for r in records if r["forecast_kwh"] > 0]
        if not valid:
            return max(0.0, raw_kwh)
        mean_fc = sum(r["forecast_kwh"] for r in valid) / len(valid)
        mean_ac = sum(r["actual_kwh"] for r in valid) / len(valid)
        factor = max(0.6, min(1.4, mean_ac / mean_fc if mean_fc > 0 else 1.0))
        return max(0.0, raw_kwh * factor)

    # ──────────────────────────────────────────────────────────────────────────
    # Weights
    # ──────────────────────────────────────────────────────────────────────────

    def _compute_weights(self, source_ids: List[str], month: int) -> Dict[str, float]:
        """
        Compute normalised weights using seasonal RMSE.
        Falls back to equal weights when any source lacks sufficient data.
        """
        rmse_map: Dict[str, Optional[float]] = {}

        for sid in source_ids:
            seasonal = _seasonal_records(self._history, sid, month)
            recent = _recent_records(self._history, sid)
            records = seasonal if len(seasonal) >= MIN_HISTORY_DAYS else recent

            if len(records) < MIN_HISTORY_DAYS:
                rmse_map[sid] = None
                continue

            errors = [r["forecast_kwh"] - r["actual_kwh"] for r in records]
            rmse = math.sqrt(sum(e ** 2 for e in errors) / len(errors))
            rmse_map[sid] = max(rmse, 0.01)

        if any(v is None for v in rmse_map.values()):
            n = len(source_ids)
            return {s: 1.0 / n for s in source_ids}

        inv = {s: 1.0 / v for s, v in rmse_map.items()}  # type: ignore[operator]
        total = sum(inv.values())
        return {s: round(v / total, 4) for s, v in inv.items()}

    # ──────────────────────────────────────────────────────────────────────────
    # Uncertainty
    # ──────────────────────────────────────────────────────────────────────────

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