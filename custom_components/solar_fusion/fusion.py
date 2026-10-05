"""
Forecast Fusion Engine for Solar Fusion.

Combines multiple SourceReadings using adaptive weighted averaging with:
  - Seasonal bias segmentation  (per-month bias/RMSE windows)
  - Isotonic regression calibration  (monotone, non-linear correction)
  - Gewichtung nach inverser Fehlervarianz mit Ausschlussschwelle
  - Verschattung nach Sonnenstand (gelernt, je Quelle vor dem Gewichten)
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from homeassistant.util import dt as dt_util

from . import calc
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
# {"date": "YYYY-MM-DD", "source": str, "forecast_kwh": float, "actual_kwh": float,
#  "forecast_corrected_kwh": float (optional, Morgenprognose nach Verschattungskorrektur)}
HistoryRecord = Dict


# Isotonic regression and other dependency-free numerics live in calc.py
# (see calc.isotonic_fit / calc.isotonic_predict) so they can be unit-tested
# without a running Home Assistant.


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
    today: Optional[date] = None,
) -> List[HistoryRecord]:
    """Return the most recent `window_days` records for source_id.

    ``today`` legt den Stichtag fest (Standard: heute); damit lässt sich das
    Fenster so bilden, wie es an einem früheren Tag ausgesehen hätte.
    """
    ref = today or dt_util.now().date()
    cutoff = (ref - timedelta(days=window_days)).isoformat()
    return [r for r in history if r["source"] == source_id and r["date"] >= cutoff]


def _round_or_none(value: Optional[float]) -> Optional[float]:
    return round(value, 3) if value is not None else None


def _shading_ratio(hourly: HourlyWh, factors: Dict[str, float]) -> float:
    """Anteil der Tagesenergie, der nach der Verschattungskorrektur übrig bleibt."""
    if not factors:
        return 1.0
    total = sum(max(0.0, wh) for wh in hourly.values())
    if total <= 0:
        return 1.0
    return sum(max(0.0, wh) * factors.get(slot, 1.0) for slot, wh in hourly.items()) / total


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
    2. RMSE je Quelle über die letzten HISTORY_WINDOW_DAYS Tage, roh und
       kalibriert (so wie die Engine den Wert am jeweiligen Morgen erzeugt hätte,
       nur mit damals vorhandener Historie). Kalibriert fusioniert wird eine
       Quelle nur, wenn das den RMSE senkt; ihr Gewicht kommt aus dem RMSE des
       Wertes, der tatsächlich fusioniert wird.
    3. Gewicht ∝ 1 / RMSE²; Quellen mit RMSE > k × RMSE der besten Quelle
       bekommen Gewicht 0 (siehe calc.inverse_variance_weights).
    4. Gleiche Gewichte, solange keine Quelle die Mindestzahl ausgewerteter
       Tage erreicht.
    5. Apply isotonic regression calibration per source before combining
       (only where step 2 found it helpful):
       - If >= MIN_ISO_POINTS seasonal records exist: isotonic curve
       - Else if >= MIN_HISTORY_DAYS records: linear multiplicative bias
       - Else: no correction (factor 1.0)
    6. Fuse hourly Wh using weighted average.
    7. Normalise fused hourly total to weighted average of calibrated daily totals.
    8. Return fused forecast + uncertainty (weighted spread of sources as %).

    Verschattung (nur mit ``shading_apply`` und gelernter Karte)
    ------------------------------------------------------------
    Jede Stunde einer Quelle wird vor dem Gewichten mit dem Faktor ihres
    Sonnenstands (Mittel über die vier Viertelstunden) multipliziert; Quellen in ``horizon_sources``
    bleiben unverändert. Der Tageswert der Quelle sinkt im selben Verhältnis
    (``ratio`` = korrigierte / rohe Stundensumme), damit Schritt 7 die
    Abschwächung nicht wieder aufhebt. Reihenfolge gegenüber der Kalibrierung:

    * Modus „korrigiert“ (sobald je Quelle ``min_eval_days`` Historientage mit
      ``forecast_corrected_kwh`` im Fenster liegen): Kalibrierung und Gewichte
      lernen auf der korrigierten Morgenprognose. Tageswert =
      kalibriert(roh × ratio).
    * Modus „roh“ (Anlaufphase): Kalibrierung und Gewichte auf der rohen
      Prognose. Ist die Kalibrierung aktiv und hat sie schon gelernt, steckt
      der mittlere Verschattungsverlust in ihrem Bias – der Tageswert bleibt
      kalibriert(roh), die Verschattung verschiebt nur Energie zwischen den
      Stunden. Sonst (Gating aus oder noch keine Historie) Tageswert =
      roh × ratio.
    """

    def __init__(
        self,
        history: List[HistoryRecord],
        exclusion_factor: float = calc.DEFAULT_EXCLUSION_FACTOR,
        min_eval_days: int = calc.DEFAULT_MIN_EVAL_DAYS,
        calibration_state: Optional[Dict[str, bool]] = None,
        shading_cells: Optional[calc.ShadingCells] = None,
        shading_apply: bool = False,
        horizon_sources: Iterable[str] = (),
        location: Optional[Tuple[float, float]] = None,
    ) -> None:
        self._history = history  # mutated in-place by coordinator
        # Letzte Gating-Entscheidung je Quelle (für die Hysterese); wird hier
        # aktualisiert und vom Coordinator mit der Historie gespeichert.
        self._calibration_state = calibration_state if calibration_state is not None else {}
        self._exclusion_factor = exclusion_factor
        self._min_eval_days = min_eval_days
        # Cache: {source_id: (knots_x, knots_y, fitted_month, corrected_mode)}
        self._iso_cache: Dict[str, Tuple[List[float], List[float], int, bool]] = {}
        # Gewichte hängen nur von Historie, Quellen und Stichtag ab; pro Update
        # werden sie mehrfach abgefragt, die Rückrechnung soll nur einmal laufen.
        self._weight_cache: Optional[Tuple[tuple, Dict[str, Dict]]] = None
        self._shading_cells: calc.ShadingCells = shading_cells or {}
        self._shading_apply = shading_apply
        self._horizon_sources = set(horizon_sources)
        self._location = location
        # Letztes Verhältnis korrigiert/roh je Datum und Quelle (Diagnose)
        self.last_shading_ratios: Dict[str, Dict[str, float]] = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Shading
    # ──────────────────────────────────────────────────────────────────────────

    def set_shading_cells(self, cells: calc.ShadingCells) -> None:
        """Neue Verschattungskarte übernehmen (nach einem Lernlauf)."""
        self._shading_cells = cells or {}
        self._iso_cache.clear()
        self._weight_cache = None

    @property
    def shading_active(self) -> bool:
        return (
            self._shading_apply
            and self._location is not None
            and any(c.get("learned") for c in self._shading_cells.values())
        )

    def slot_factors(self, slots: Iterable[str]) -> Dict[str, float]:
        """Verschattungsfaktoren je Stundenslot ("YYYY-MM-DDTHH:00", lokal); nur Werte ≠ 1."""
        if not self.shading_active:
            return {}
        lat, lon = self._location
        tz = dt_util.get_default_time_zone()
        out: Dict[str, float] = {}
        for slot in slots:
            try:
                start = datetime.fromisoformat(slot).replace(tzinfo=tz)
            except ValueError:
                continue
            factor = calc.shading_slot_factor(
                self._shading_cells,
                calc.slot_sun_positions(
                    start, lat, lon, quarters=calc.SHADING_APPLY_QUARTERS
                ),
            )
            if factor != 1.0:
                out[slot] = factor
        return out

    def shading_ratios(
        self, readings: List[SourceReading], target_date: date
    ) -> Dict[str, float]:
        """Je Quelle korrigierte / rohe Tagesenergie für ``target_date`` (1,0 ohne Korrektur).

        Quellen ohne Stundenwerte für den Tag bekommen das mittlere Verhältnis
        der übrigen korrigierten Quellen.
        """
        is_today = target_date == dt_util.now().date()
        date_str = target_date.isoformat()
        shapes = {
            r.source_id: {
                s: wh
                for s, wh in (r.hourly_today if is_today else r.hourly_tomorrow).items()
                if s.startswith(date_str)
            }
            for r in readings
        }
        return self._ratios_for_shapes(shapes, self.slot_factors(
            {slot for sh in shapes.values() for slot in sh}
        ))

    def _ratios_for_shapes(
        self, shapes: Dict[str, HourlyWh], factors: Dict[str, float]
    ) -> Dict[str, float]:
        if not factors:
            return {sid: 1.0 for sid in shapes}
        ratios: Dict[str, Optional[float]] = {}
        for sid, shape in shapes.items():
            if sid in self._horizon_sources:
                ratios[sid] = 1.0
            elif shape:
                ratios[sid] = _shading_ratio(shape, factors)
            else:
                ratios[sid] = None
        known = [
            v for sid, v in ratios.items()
            if v is not None and sid not in self._horizon_sources
        ]
        estimate = sum(known) / len(known) if known else 1.0
        return {sid: (estimate if v is None else v) for sid, v in ratios.items()}

    def _corrected_mode(self, source_id: str) -> bool:
        """Kalibrierung/Gewichtung dieser Quelle auf der korrigierten Historie?"""
        if not self._shading_apply:
            return False
        recent = _recent_records(self._history, source_id)
        n = sum(1 for r in recent if "forecast_corrected_kwh" in r)
        return n >= self._min_eval_days

    def _source_history(self, source_id: str) -> List[HistoryRecord]:
        """Historie, auf der Kalibrierung und Gewichtung dieser Quelle laufen.

        Im Modus „roh“ die unveränderte Historie; im Modus „korrigiert“ nur
        Tage mit korrigierter Morgenprognose, die dann als forecast_kwh gilt.
        """
        if not self._corrected_mode(source_id):
            return self._history
        return [
            {**r, "forecast_kwh": r["forecast_corrected_kwh"]}
            for r in self._history
            if r["source"] == source_id and "forecast_corrected_kwh" in r
        ]

    def _source_daily_target(
        self, source_id: str, raw_kwh: float, ratio: float, month: int, calibrate: bool
    ) -> float:
        """Tageswert der Quelle nach Verschattung und Kalibrierung (siehe Klassendoku)."""
        if self._corrected_mode(source_id):
            return self._fusion_kwh(source_id, raw_kwh * ratio, month, calibrate)
        if calibrate and self._has_calibration_data(source_id, month):
            return self._fusion_kwh(source_id, raw_kwh, month, True)
        return self._fusion_kwh(source_id, raw_kwh * ratio, month, calibrate)

    def _has_calibration_data(self, source_id: str, month: int) -> bool:
        """Ob _calibrate eine gelernte Korrektur anwendet (sonst ist sie die Identität)."""
        history = self._source_history(source_id)
        return (
            len(_seasonal_records(history, source_id, month)) >= MIN_ISO_POINTS
            or len(_recent_records(history, source_id)) >= MIN_HISTORY_DAYS
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Public
    # ──────────────────────────────────────────────────────────────────────────

    def fuse(
        self,
        readings: List[SourceReading],
        target_date: date,
    ) -> Tuple[HourlyWh, Optional[float], Dict[str, float]]:
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
        _today = dt_util.now().date()
        details = self.compute_weights(source_ids)
        weights = {sid: info["weight"] for sid, info in details.items()}
        # Kalibrierung nur für Quellen, bei denen sie den Fehler senkt (siehe compute_weights)
        use_cal = {sid: info["calibration_active"] for sid, info in details.items()}

        date_str = target_date.isoformat()
        is_today = target_date == _today
        raw_by_sid = {
            r.source_id: (r.today_kwh if is_today else r.tomorrow_kwh) for r in readings
        }
        day_hourly = {
            r.source_id: {
                slot: wh
                for slot, wh in (r.hourly_today if is_today else r.hourly_tomorrow).items()
                if slot.startswith(date_str)
            }
            for r in readings
        }
        use_profile = not any(day_hourly.values())
        if use_profile:
            _LOGGER.debug(
                "No hourly data for %s – using calibrated daily totals as fallback", date_str
            )
            # Build a solar-shape profile from today's data (or generic bell curve)
            # and distribute the daily total across 24 hourly slots.
            profile = _build_solar_profile(readings, _today)
            # Rewrite the date part from today to target_date
            shape = {
                slot.replace(_today.isoformat(), date_str): fraction
                for slot, fraction in profile.items()
            }
            shapes = {sid: shape for sid in raw_by_sid}
        else:
            shapes = day_hourly

        # Verschattung: Faktor je Slot, Verhältnis korrigiert/roh je Quelle
        factors = self.slot_factors({slot for sh in shapes.values() for slot in sh})
        ratios = self._ratios_for_shapes(shapes, factors)
        self.last_shading_ratios[date_str] = ratios
        targets = {
            sid: self._source_daily_target(
                sid, raw_by_sid[sid], ratios[sid], current_month, use_cal[sid]
            )
            for sid in raw_by_sid
        }

        slots: Dict[str, Dict[str, float]] = {}
        for reading in readings:
            sid = reading.source_id
            f = {} if sid in self._horizon_sources else factors
            ratio = ratios[sid]
            if use_profile:
                target_wh = max(0.0, targets[sid] * 1000.0)
                for slot, fraction in shapes[sid].items():
                    slots.setdefault(slot, {})[sid] = round(
                        target_wh * fraction * f.get(slot, 1.0) / ratio, 1
                    )
                continue
            denom = raw_by_sid[sid] * ratio
            hourly_scale = (targets[sid] / denom) if denom > 0 else 1.0
            for slot, wh in day_hourly[sid].items():
                slots.setdefault(slot, {})[sid] = max(
                    0.0, wh * f.get(slot, 1.0) * hourly_scale
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

        # Schritt 7: Zielsumme aus den Tageswerten nach Verschattung, damit die
        # Skalierung die stündliche Abschwächung nicht wieder aufhebt.
        target_wh = sum(
            targets[r.source_id] * weights.get(r.source_id, 0.0) * 1000.0
            for r in readings
        )
        fused_total = sum(fused.values())
        if fused_total > 0 and target_wh > 0:
            scale = target_wh / fused_total
            fused = {slot: round(wh * scale, 1) for slot, wh in fused.items()}

        shaded_raw = {sid: raw_by_sid[sid] * ratios[sid] for sid in raw_by_sid}
        uncertainty_pct = self._compute_uncertainty(shaded_raw, weights, fused)
        return fused, uncertainty_pct, weights

    def record_actual(
        self,
        target_date: date,
        actual_kwh: float,
        readings: List[SourceReading],
        corrected: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Store actual production alongside each source's daily total for target_date.
        Invalidates the isotonic cache for the affected month.

        ``corrected``: Morgenprognose je Quelle nach Verschattungskorrektur;
        wird als ``forecast_corrected_kwh`` mitgespeichert.
        """
        date_str = target_date.isoformat()
        month = target_date.month

        self._history[:] = [r for r in self._history if r["date"] != date_str]

        for reading in readings:
            # When called from _async_maybe_record_yesterday, readings come from
            # the morning snapshot where today_kwh holds the forecast for target_date
            # and tomorrow_kwh is always 0.0. Using today_kwh is always correct here.
            forecast_kwh = reading.today_kwh
            record = {
                "date": date_str,
                "source": reading.source_id,
                "forecast_kwh": round(float(forecast_kwh), 3),
                "actual_kwh": round(float(actual_kwh), 3),
            }
            if corrected and reading.source_id in corrected:
                record["forecast_corrected_kwh"] = round(float(corrected[reading.source_id]), 3)
            self._history.append(record)

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
        current_month = dt_util.now().date().month
        result = {}

        for source_id in ALL_SOURCES:
            seasonal = _seasonal_records(self._history, source_id, current_month)
            recent = _recent_records(self._history, source_id)

            if not recent:
                result[source_id] = {
                    "rmse": None, "rmse_pct": None, "mae": None, "bias": None,
                    "std": None, "std_pct": None, "bias_pct": None,
                    "days_evaluated": 0, "calibration_mode": "none",
                }
                continue

            errors = [r["forecast_kwh"] - r["actual_kwh"] for r in recent]
            rmse = math.sqrt(sum(e ** 2 for e in errors) / len(errors))
            mae = sum(abs(e) for e in errors) / len(errors)
            mean_bias = sum(errors) / len(errors)
            # Scatter = error spread with the systematic bias removed. Drives the
            # quality label; the weight comes from the calibrated RMSE instead
            # (see compute_weights).
            std = math.sqrt(sum((e - mean_bias) ** 2 for e in errors) / len(errors))
            mean_actual = sum(r["actual_kwh"] for r in recent) / len(recent)
            rmse_pct = round(rmse / mean_actual * 100, 1) if mean_actual > 0 else None
            std_pct = round(std / mean_actual * 100, 1) if mean_actual > 0 else None
            bias_pct = (
                round(abs(mean_bias) / mean_actual * 100, 1) if mean_actual > 0 else None
            )

            if len(seasonal) >= MIN_ISO_POINTS:
                cal_mode = f"isotonic ({len(seasonal)} seasonal pts)"
            elif len(recent) >= MIN_HISTORY_DAYS:
                cal_mode = f"linear_bias ({len(recent)} recent pts)"
            else:
                cal_mode = "none (insufficient data)"

            result[source_id] = {
                "rmse": round(rmse, 3),
                "rmse_pct": rmse_pct,
                "mae": round(mae, 3),
                "bias": round(mean_bias, 3),
                "std": round(std, 3),
                "std_pct": std_pct,
                "bias_pct": bias_pct,
                "days_evaluated": len(recent),
                "calibration_mode": cal_mode,
            }

        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Calibration
    # ──────────────────────────────────────────────────────────────────────────

    def _fusion_kwh(
        self, source_id: str, raw_kwh: float, month: int, calibrate: bool
    ) -> float:
        """Tageswert, der in die Fusion eingeht: kalibriert oder roh (Gating)."""
        if calibrate:
            return self._calibrate(source_id, raw_kwh, month)
        return max(0.0, raw_kwh)

    def _calibrate(self, source_id: str, raw_kwh: float, month: int) -> float:
        """
        Return calibrated kWh for a raw forecast value.

        Priority:
        1. Isotonic regression (seasonal, >= MIN_ISO_POINTS records)
        2. Linear multiplicative bias (recent, >= MIN_HISTORY_DAYS records)
        3. Identity (no correction)
        """
        history = self._source_history(source_id)
        seasonal = _seasonal_records(history, source_id, month)

        if len(seasonal) >= MIN_ISO_POINTS:
            return self._calibrate_isotonic(source_id, raw_kwh, seasonal, month)

        recent = _recent_records(history, source_id)
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
        corrected = self._corrected_mode(source_id)
        if cached is None or cached[2] != month or cached[3] != corrected:
            xs = [r["forecast_kwh"] for r in records]
            ys = [r["actual_kwh"] for r in records]
            knots_x, knots_y = calc.isotonic_fit(xs, ys)
            self._iso_cache[source_id] = (knots_x, knots_y, month, corrected)
            _LOGGER.debug(
                "Fitted isotonic regression for %s month=%d: %d knots from %d points",
                source_id, month, len(knots_x), len(records),
            )
        else:
            knots_x, knots_y, _, _ = cached

        return max(0.0, calc.isotonic_predict(knots_x, knots_y, raw_kwh))

    def _calibrate_linear(self, raw_kwh: float, records: List[HistoryRecord]) -> float:
        """Apply multiplicative linear bias correction.

        The factor is clamped to [calc.MIN_BIAS_FACTOR, calc.MAX_BIAS_FACTOR]
        (0.5–2.0). The previous ±40 % clamp could not correct strongly biased
        sources (e.g. a Forecast.Solar instance configured at half capacity)
        during the early 3–19 day window before isotonic calibration applies.
        """
        valid = [r for r in records if r["forecast_kwh"] > 0]
        if not valid:
            return max(0.0, raw_kwh)
        mean_fc = sum(r["forecast_kwh"] for r in valid) / len(valid)
        mean_ac = sum(r["actual_kwh"] for r in valid) / len(valid)
        factor = calc.linear_bias_factor(mean_fc, mean_ac)
        return max(0.0, raw_kwh * factor)

    def _calibrate_as_of(self, source_id: str, raw_kwh: float, as_of: date) -> float:
        """Kalibrierter Wert, wie ihn _calibrate am Morgen von ``as_of`` geliefert hätte.

        Nutzt nur Historie *vor* ``as_of`` und dieselbe Rangfolge wie _calibrate
        (isotonisch → linear → unverändert), aber ohne Cache. Ein In-Sample-Fehler
        wäre zu optimistisch, weil die isotonische Kurve auf genau diesen Punkten
        angepasst wurde.
        """
        prior = [r for r in self._source_history(source_id) if r["date"] < as_of.isoformat()]

        seasonal = _seasonal_records(prior, source_id, as_of.month)
        if len(seasonal) >= MIN_ISO_POINTS:
            knots_x, knots_y = calc.isotonic_fit(
                [r["forecast_kwh"] for r in seasonal],
                [r["actual_kwh"] for r in seasonal],
            )
            return max(0.0, calc.isotonic_predict(knots_x, knots_y, raw_kwh))

        recent = _recent_records(prior, source_id, today=as_of)
        if len(recent) >= MIN_HISTORY_DAYS:
            return self._calibrate_linear(raw_kwh, recent)

        return max(0.0, raw_kwh)

    # ──────────────────────────────────────────────────────────────────────────
    # Weights
    # ──────────────────────────────────────────────────────────────────────────

    def compute_weights(self, source_ids: List[str]) -> Dict[str, Dict]:
        """Gewichte der aktiven Quellen samt Ausschlussinformation.

        Je Quelle werden über die letzten HISTORY_WINDOW_DAYS Tage (dieselben Tage,
        über die source_quality die Rohfehler berichtet) zwei Fehler bestimmt:
        der des Rohwertes und der des kalibrierten Wertes (siehe
        _calibrate_as_of). Kalibriert wird nur, wenn das den Fehler senkt
        (calc.use_calibration, mit Hysterese gegenüber der letzten Entscheidung).
        Das Gewicht kommt aus dem Fehler des Wertes, der tatsächlich in die
        Fusion eingeht.

        Rückgabe je Quelle: ``{"weight", "excluded", "exclusion_reason",
        "calibration_active", "rmse_raw", "rmse_calibrated", "days_evaluated"}``.
        Tages- und Stundenprognose nutzen dieselben Gewichte.
        """
        today = dt_util.now().date()
        key = (
            today,
            tuple(source_ids),
            tuple(
                (r["date"], r["source"], r["forecast_kwh"], r["actual_kwh"],
                 r.get("forecast_corrected_kwh"))
                for r in self._history
            ),
            tuple(self._corrected_mode(sid) for sid in source_ids),
        )
        if self._weight_cache is not None and self._weight_cache[0] == key:
            return self._weight_cache[1]

        rmse_map: Dict[str, Optional[float]] = {}
        days_map: Dict[str, int] = {}
        raw_map: Dict[str, Optional[float]] = {}
        cal_map: Dict[str, Optional[float]] = {}
        active_map: Dict[str, bool] = {}
        for sid in source_ids:
            records = _recent_records(self._source_history(sid), sid, today=today)
            raw_map[sid] = calc.rmse([r["forecast_kwh"] - r["actual_kwh"] for r in records])
            cal_map[sid] = calc.rmse([
                self._calibrate_as_of(sid, r["forecast_kwh"], date.fromisoformat(r["date"]))
                - r["actual_kwh"]
                for r in records
            ])
            days_map[sid] = len(records)
            active_map[sid] = calc.use_calibration(
                raw_map[sid], cal_map[sid], days_map[sid], self._min_eval_days,
                previous=self._calibration_state.get(sid),
            )
            if days_map[sid] >= self._min_eval_days:
                if self._calibration_state.get(sid) not in (None, active_map[sid]):
                    _LOGGER.info(
                        "%s: Kalibrierung %s (RMSE roh %.2f, kalibriert %.2f kWh)",
                        SOURCE_NAMES.get(sid, sid),
                        "aktiviert" if active_map[sid] else "deaktiviert",
                        raw_map[sid], cal_map[sid],
                    )
                self._calibration_state[sid] = active_map[sid]
            rmse_map[sid] = cal_map[sid] if active_map[sid] else raw_map[sid]

        result = calc.inverse_variance_weights(
            rmse_map,
            days_map,
            exclusion_factor=self._exclusion_factor,
            min_days=self._min_eval_days,
        )
        for sid, info in result.items():
            info["calibration_active"] = active_map[sid]
            info["rmse_raw"] = _round_or_none(raw_map[sid])
            info["rmse_calibrated"] = _round_or_none(cal_map[sid])
            info["days_evaluated"] = days_map[sid]
            info["shading_corrected_history"] = self._corrected_mode(sid)
            if info["excluded"]:
                _LOGGER.debug(
                    "%s ausgeschlossen: %s",
                    SOURCE_NAMES.get(sid, sid), info["exclusion_reason"],
                )

        self._weight_cache = (key, result)
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Uncertainty
    # ──────────────────────────────────────────────────────────────────────────

    def _compute_uncertainty(
        self,
        raw_daily: Dict[str, float],
        weights: Dict[str, float],
        fused: HourlyWh,
    ) -> Optional[float]:
        """
        Uncertainty = weighted spread of the *raw* per-source daily forecasts,
        as a percentage of the fused daily total.

        Using the raw daily totals (e.g. 38 / 67 / 59 kWh) reflects the genuine
        disagreement between sources. Computing it on the calibrated/scaled
        hourly slots instead collapses the spread – calibration pulls every
        source onto roughly the same daily total – which made the figure
        misleadingly low (e.g. 0.6 %).

        With shading applied, ``raw_daily`` holds each raw total times the
        source's kept share, the same basis as the fused total; otherwise the
        spread would grow by 1 / share.

        Returns ``None`` when fewer than two sources are available (no
        cross-validation possible), so the sensor can show "unknown" instead of
        a misleading 0 %.
        """
        fused_total_kwh = sum(fused.values()) / 1000.0
        return calc.weighted_spread_pct(raw_daily, weights, fused_total_kwh)