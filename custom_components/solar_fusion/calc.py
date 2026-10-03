"""Pure numeric helpers for Solar Fusion.

This module deliberately has **no Home Assistant imports** (only the stdlib),
so the core calibration / weighting / metric logic can be unit-tested
standalone without a running Home Assistant. See tests/test_calc.py.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

# Quality-label thresholds (percent of mean actual production)
SCATTER_BAD_PCT = 30.0      # irreducible scatter at/above this → "Schlecht"
SCATTER_NOISY_PCT = 15.0    # scatter in [NOISY, BAD) → "Unruhig"
BIAS_SKEWED_PCT = 15.0      # |bias| at/above this (with low scatter) → "Verzerrt"

# Linear bias-correction clamp (multiplicative factor bounds)
MIN_BIAS_FACTOR = 0.5
MAX_BIAS_FACTOR = 2.0

# Gewichtung: Ausschluss ab k × RMSE der besten Quelle, Mindestzahl ausgewerteter
# Tage, Untergrenze für den RMSE (gegen Division durch fast 0)
DEFAULT_EXCLUSION_FACTOR = 2.0
DEFAULT_MIN_EVAL_DAYS = 7
RMSE_FLOOR_KWH = 0.5

# Hysterese beim Kalibrierungs-Gating: gewechselt wird erst, wenn die andere
# Variante (roh / kalibriert) um mindestens diesen Anteil besser ist
CALIBRATION_HYSTERESIS = 0.10


def daily_total_from_increasing(values: List[float]) -> float:
    """Daily production from a ``total_increasing`` meter's in-window samples.

    Sums the positive deltas between consecutive readings. This is robust to:
      * a carryover sample at the window start (the previous day's total, which
        the recorder synthesises at exactly midnight) – the following reset to 0
        is a negative delta and is ignored;
      * the daily midnight reset itself;
      * lifetime cumulative counters that never reset (reduces to last − first).

    Using ``max(values) - min(values)`` instead would return the previous day's
    total whenever it exceeded today's, because the carryover sample becomes the
    maximum.
    """
    total = 0.0
    for prev, cur in zip(values, values[1:]):
        if cur > prev:
            total += cur - prev
    return total


def isotonic_fit(x: List[float], y: List[float]) -> Tuple[List[float], List[float]]:
    """Fit a monotone non-decreasing step function via pool-adjacent-violators.

    Returns (knots_x, knots_y). No external dependencies.
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
        if _mean(blocks[i]) > _mean(blocks[i + 1]):
            blocks[i] = blocks[i] + blocks[i + 1]
            block_xs[i] = block_xs[i] + block_xs[i + 1]
            blocks.pop(i + 1)
            block_xs.pop(i + 1)
            if i > 0:
                i -= 1
        else:
            i += 1

    knots_x = [sum(bx) / len(bx) for bx in block_xs]
    knots_y = [_mean(b) for b in blocks]
    return knots_x, knots_y


def isotonic_predict(knots_x: List[float], knots_y: List[float], value: float) -> float:
    """Interpolate between isotonic knots; extrapolate flat outside the range."""
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


def linear_bias_factor(mean_forecast: float, mean_actual: float) -> float:
    """Multiplicative bias-correction factor, clamped to [MIN, MAX]_BIAS_FACTOR."""
    if mean_forecast <= 0:
        return 1.0
    return max(MIN_BIAS_FACTOR, min(MAX_BIAS_FACTOR, mean_actual / mean_forecast))


def quality_label(scatter_pct: Optional[float], bias_pct: Optional[float]) -> Optional[str]:
    """Categorical source-quality label that separates bias from scatter.

    A source with a large but *consistent* offset (high bias, low scatter) is
    fully correctable by calibration, so it is labelled "Verzerrt (korrigiert)"
    rather than hidden behind a green "good" – while a source whose error is
    irreducible scatter is labelled "Unruhig"/"Schlecht". This keeps the label
    coherent with the weighting (which is driven by scatter, not raw RMSE) yet
    still surfaces a large raw deviation instead of masking it.
    """
    if scatter_pct is None:
        return None
    if scatter_pct >= SCATTER_BAD_PCT:
        return "Schlecht"
    if scatter_pct >= SCATTER_NOISY_PCT:
        return "Unruhig"
    if (bias_pct or 0.0) >= BIAS_SKEWED_PCT:
        return "Verzerrt (korrigiert)"
    return "Genau"


def rmse(errors: List[float]) -> Optional[float]:
    """Root mean square of ``errors``; ``None`` for an empty list."""
    if not errors:
        return None
    return math.sqrt(sum(e * e for e in errors) / len(errors))


def use_calibration(
    rmse_raw: Optional[float],
    rmse_calibrated: Optional[float],
    days: int,
    min_days: int = DEFAULT_MIN_EVAL_DAYS,
    previous: Optional[bool] = None,
    hysteresis: float = CALIBRATION_HYSTERESIS,
) -> bool:
    """Ob eine Quelle kalibriert in die Fusion eingeht.

    Kalibrierung lohnt sich nur, wenn sie den Fehler nachweislich senkt: Über
    dieselben ausgewerteten Tage muss der RMSE des kalibrierten Wertes (ohne
    Kenntnis des jeweiligen Tages ermittelt) kleiner sein als der des Rohwertes.
    Eine isotonische Kurve aus wenigen Dutzend Punkten kann eine gute Quelle
    sonst verschlechtern (Regression zur Mitte, flache Extrapolation).

    Unter ``min_days`` ausgewerteten Tagen ist der Vergleich zu unsicher; dann
    bleibt es beim bisherigen Verhalten (kalibrieren).

    Hysterese: Mit ``previous`` (letzte Entscheidung) wird nur gewechselt, wenn
    die andere Variante um mindestens ``hysteresis`` (Anteil) besser ist. Liegen
    beide RMSE nah beieinander, pendelt die Quelle so nicht täglich zwischen roh
    und kalibriert. Ohne ``previous`` entscheidet der einfache Vergleich.
    """
    if rmse_raw is None or rmse_calibrated is None or days < min_days:
        return True
    if previous is True:
        return rmse_calibrated <= rmse_raw * (1 + hysteresis)
    if previous is False:
        return rmse_calibrated < rmse_raw * (1 - hysteresis)
    return rmse_calibrated < rmse_raw


def inverse_variance_weights(
    rmse_map: Dict[str, Optional[float]],
    days_map: Dict[str, int],
    exclusion_factor: float = DEFAULT_EXCLUSION_FACTOR,
    min_days: int = DEFAULT_MIN_EVAL_DAYS,
    rmse_floor: float = RMSE_FLOOR_KWH,
) -> Dict[str, Dict]:
    """Gewichte nach inverser Fehlervarianz mit Ausschlussschwelle.

    ``rmse_map`` enthält je aktiver Quelle den RMSE (kWh) des Wertes, der
    tatsächlich in die Fusion eingeht, ``days_map`` die Zahl der ausgewerteten
    Tage. Ablauf:

    * Quellen mit weniger als ``min_days`` Tagen (oder ohne RMSE) sind in der
      Anlaufphase. Haben *alle* Quellen zu wenig Tage, gibt es gleiche Gewichte.
      Sonst bekommt eine Quelle in der Anlaufphase die mittlere inverse Varianz
      der nicht ausgeschlossenen Quellen – eine neu hinzugefügte Quelle setzt
      die gelernte Gewichtung der übrigen also nicht zurück.
    * Der RMSE wird nach unten auf ``rmse_floor`` begrenzt (keine Division
      durch fast 0).
    * Liegt der RMSE einer Quelle über ``exclusion_factor`` × RMSE der besten
      Quelle, bekommt sie Gewicht 0. Sie wird weiter bewertet und kommt von
      selbst zurück, sobald ihr Fehler wieder unter die Schwelle fällt.
    * Alle übrigen: w_i ∝ 1 / RMSE_i², normiert auf Summe 1.
    * Bleibt keine Quelle übrig (nur bei ``exclusion_factor`` < 1 möglich),
      zählt allein die beste Quelle.

    Rückgabe je Quelle: ``{"weight", "excluded", "exclusion_reason"}``.
    """
    if not rmse_map:
        return {}

    def _info(weight: float, reason: Optional[str] = None) -> Dict:
        return {"weight": round(weight, 4), "excluded": reason is not None,
                "exclusion_reason": reason}

    evaluated = {
        s: max(v, rmse_floor)
        for s, v in rmse_map.items()
        if v is not None and days_map.get(s, 0) >= min_days
    }
    if not evaluated:
        n = len(rmse_map)
        return {s: _info(1.0 / n) for s in rmse_map}

    best_sid = min(evaluated, key=evaluated.get)
    best = evaluated[best_sid]
    limit = exclusion_factor * best
    kept = {s: v for s, v in evaluated.items() if v <= limit}
    if not kept:
        kept = {best_sid: best}

    inv = {s: 1.0 / (v * v) for s, v in kept.items()}
    fill = sum(inv.values()) / len(inv)
    for s in rmse_map:
        if s not in evaluated:
            inv[s] = fill
    total = sum(inv.values())

    result: Dict[str, Dict] = {}
    for s in rmse_map:
        if s in inv:
            result[s] = _info(inv[s] / total)
        elif evaluated[s] > limit:
            result[s] = _info(0.0, (
                f"RMSE {evaluated[s]:.1f} kWh > {exclusion_factor:g} × "
                f"beste Quelle ({best:.1f} kWh)"
            ))
        else:
            result[s] = _info(0.0, (
                f"Schwelle {exclusion_factor:g} < 1 schließt alle Quellen aus – "
                f"nur beste Quelle aktiv"
            ))
    return result


def weighted_spread_pct(
    values: Dict[str, float],
    weights: Dict[str, float],
    ref: Optional[float],
) -> Optional[float]:
    """Weighted standard deviation of per-source values, as a percent of ``ref``.

    Returns ``None`` when fewer than two sources are present – with a single
    source there is no cross-validation, so a "0 % / sources agree" reading
    would be misleading.
    """
    items = [(s, v) for s, v in values.items() if s in weights]
    if len(items) < 2:
        return None
    w_sum = sum(weights.get(s, 0.0) for s, _ in items)
    if w_sum <= 0:
        return None
    mean = sum(weights[s] * v for s, v in items) / w_sum
    variance = sum(weights[s] * (v - mean) ** 2 for s, v in items) / w_sum
    std = math.sqrt(variance)
    base = ref if (ref and ref > 0) else mean
    if base <= 0:
        return None
    return round(min(std / base * 100, 100.0), 1)


def _mean(block: List[float]) -> float:
    return sum(block) / len(block)
