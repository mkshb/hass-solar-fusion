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


def inverse_spread_weights(
    spread_map: Dict[str, Optional[float]]
) -> Dict[str, float]:
    """Normalised 1/spread weights.

    Sources lacking sufficient history (value ``None``) are assigned the *mean*
    spread of the sources that do have data, so adding a new source no longer
    collapses every source to an equal weight – the established sources keep
    their learned weighting and the newcomer starts mid-pack. If no source has
    data at all, falls back to equal weights.
    """
    if not spread_map:
        return {}
    known = [v for v in spread_map.values() if v is not None]
    n = len(spread_map)
    if not known:
        return {s: 1.0 / n for s in spread_map}
    fill = sum(known) / len(known)
    inv = {
        s: 1.0 / max(v if v is not None else fill, 0.01)
        for s, v in spread_map.items()
    }
    total = sum(inv.values())
    return {s: round(v / total, 4) for s, v in inv.items()}


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
