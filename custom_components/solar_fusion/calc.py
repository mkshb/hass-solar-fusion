"""Pure numeric helpers for Solar Fusion.

This module deliberately has **no Home Assistant imports** (only the stdlib),
so the core calibration / weighting / metric logic can be unit-tested
standalone without a running Home Assistant. See tests/test_calc.py.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Quality-label thresholds (percent of mean actual production)
SCATTER_BAD_PCT = 30.0      # irreducible scatter at/above this → "poor"
SCATTER_NOISY_PCT = 15.0    # scatter in [NOISY, BAD) → "noisy"
BIAS_SKEWED_PCT = 15.0      # |bias| at/above this (with low scatter) → "skewed"

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
    fully correctable by calibration, so it is labelled "skewed" (corrected)
    rather than hidden behind a green "accurate" – while a source whose error
    is irreducible scatter is labelled "noisy"/"poor". This keeps the label
    coherent with the weighting (which is driven by scatter, not raw RMSE) yet
    still surfaces a large raw deviation instead of masking it.

    Returns a language-neutral key; display text lives in translations/.
    """
    if scatter_pct is None:
        return None
    if scatter_pct >= SCATTER_BAD_PCT:
        return "poor"
    if scatter_pct >= SCATTER_NOISY_PCT:
        return "noisy"
    if (bias_pct or 0.0) >= BIAS_SKEWED_PCT:
        return "skewed"
    return "accurate"


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


# ──────────────────────────────────────────────────────────────────────────────
# Verschattung nach Sonnenstand
#
# Gelernt wird ein Faktor je Zelle aus Sonnen-Azimut × Sonnenhöhe, nicht je
# Uhrzeit: Ein Hindernis verschattet immer dieselbe Himmelsrichtung, die
# Uhrzeit des Einbruchs wandert dagegen mit der Jahreszeit.
#
# Lernen (learn_shading_map):
#   1. Je Tag, Quelle und Stunde das Verhältnis Ist / Morgenprognose.
#   2. Wolken herausrechnen: durch das Tagesverhältnis der sicher
#      unverschatteten Stunden (Sonnenhöhe > SHADING_REF_ELEVATION) teilen.
#      Nur Tage, an denen dieses Verhältnis über die Referenzstunden stabil
#      ist (Variationskoeffizient ≤ SHADING_MAX_REF_CV); an wechselhaften
#      Tagen ist das Stundenverhältnis Wolkenrauschen.
#   3. Je Stunde den Mittelwert über die stabilen Quellen, eingeordnet nach
#      dem Sonnenstand zur Slotmitte.
#   4. Je Zelle der Median über alle Stichproben der Zelle und ihrer
#      Nachbarn (3 × 3, SHADING_POOL_RADIUS), begrenzt auf
#      [SHADING_FACTOR_MIN, SHADING_FACTOR_MAX]. Erst ab SHADING_MIN_SAMPLES
#      Stichproben in dieser Nachbarschaft gilt eine Zelle als gelernt.
#
#      Warum Nachbarn: Die Slotmitte (hh:30) wandert je Tag um 0,5–0,7°
#      Höhe; eine 5° × 2°-Zelle wird von einem Slot nur an wenigen Tagen
#      getroffen, und nur ein Teil davon ist klar. Ohne Nachbarn erreicht
#      kaum eine Zelle die Mindestzahl, und der Slot von morgen liegt meist
#      in einer noch ungelernten Zelle. In der rollierenden Simulation
#      (lernen aus allen Vortagen, Prognose für den nächsten Tag, 40 % klare
#      Tage) sinkt der Stunden-RMSE gegenüber „ohne Korrektur“ nur Zelle
#      allein um 10 %, mit 3 × 3-Nachbarschaft um 49 %, mit groben Zellen
#      (10° × 4°) um 40 %. Das Stundenverhältnis ist ohnehin über ~12°
#      Sonnenweg gemittelt, die Glättung kostet daher kaum Schärfe.
#
# Anwenden (shading_factor): bilinear zwischen den Mittelpunkten gelernter
# Zellen; ungelernte Zellen zählen nicht mit, ohne gelernte Nachbarn gilt 1,0.
# Faktoren ab SHADING_NEUTRAL_ABOVE gelten als 1,0 (Rauschen, keine
# Verschattung).
#
# Gelernt wird mit dem Sonnenstand zur Slotmitte, angewendet als Mittel der
# Faktoren an den vier Viertelstunden-Mitten (SHADING_APPLY_QUARTERS).
# Vergleich (synthetische Anlage, Stunden-RMSE gegenüber der Wahrheit):
#   Karte aus einer Saison, angewendet im Folgejahr
#     30 % klare Tage:  Mitte 80 Wh, Viertelstunden 63 Wh (ohne Korrektur 222)
#     50 % klare Tage:  Mitte 63 Wh, Viertelstunden 55 Wh
#    100 % klare Tage:  Mitte 46 Wh, Viertelstunden 48 Wh
#   rollierend (lernen aus Vortagen), 25 % / 40 % / 70 % klare Tage:
#     Mitte 225 / 165 / 114 Wh, Viertelstunden 197 / 168 / 134 Wh
# Die Viertelstunden greifen entlang des Sonnenwegs auf Nachbarzellen zu und
# überbrücken so Lücken der Karte; nur bei fast nur klaren Tagen (dichte
# Karte) ist die Slotmitte etwas besser. Bei realistischem Wetter überwiegt
# der Vorteil der Viertelstunden (siehe tests/test_shading.py).
# ──────────────────────────────────────────────────────────────────────────────

SHADING_AZ_STEP = 5.0            # Zellbreite Azimut (°)
SHADING_EL_STEP = 2.0            # Zellhöhe Sonnenhöhe (°)
SHADING_REF_ELEVATION = 25.0     # Stunden darüber gelten als sicher unverschattet
SHADING_MIN_REF_HOURS = 3        # Mindestzahl Referenzstunden je Tag
SHADING_MAX_REF_CV = 0.15        # max. Streuung (std / mean) des Verhältnisses in den Referenzstunden
SHADING_MIN_FORECAST_WH = 150.0  # Stunden mit weniger Prognose sind zu verrauscht
SHADING_MIN_SAMPLES = 3          # Mindestzahl Stichproben (Tage) je Zelle inkl. Nachbarn
SHADING_POOL_RADIUS = 1          # Nachbarschaft beim Lernen: 1 → 3 × 3 Zellen
SHADING_FACTOR_MIN = 0.05
# Keine Faktoren über 1,0: Ein Mehrertrag durch Reflexion ließe sich vom
# Rauschen der Normierung nicht trennen.
SHADING_FACTOR_MAX = 1.0
SHADING_NEUTRAL_ABOVE = 0.9      # Faktoren ab hier gelten als 1,0
SHADING_APPLY_QUARTERS = True    # Anwenden: Mittel über vier Viertelstunden statt Slotmitte

ShadingCells = Dict[str, Dict]   # {"245:12": {"az", "el", "factor", "n", "n_pooled", "learned"}}


def sun_position(when: datetime, lat: float, lon: float) -> Tuple[float, float]:
    """Sonnenstand (Azimut, Höhe) in Grad nach der NOAA-Näherung.

    ``when`` muss zeitzonenbehaftet sein. Azimut im Uhrzeigersinn ab Nord
    (180° = Süd), Höhe inklusive atmosphärischer Refraktion. Genauigkeit
    besser als 0,1° für Sonnenhöhen über ein paar Grad.
    """
    if when.tzinfo is None:
        raise ValueError("when must be timezone-aware")
    utc = when.astimezone(timezone.utc)
    jd = utc.timestamp() / 86400.0 + 2440587.5
    jc = (jd - 2451545.0) / 36525.0

    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = math.radians(m)
    ctr = (
        math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + math.sin(2 * mr) * (0.019993 - 0.000101 * jc)
        + math.sin(3 * mr) * 0.000289
    )
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = l0 + ctr - 0.00569 - 0.00478 * math.sin(omega)
    obliq0 = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq = math.radians(obliq0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(math.radians(app_long)))

    y = math.tan(obliq / 2) ** 2
    l0r = math.radians(l0)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * ecc * math.sin(mr)
        + 4 * ecc * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * ecc * ecc * math.sin(2 * mr)
    )

    minutes = utc.hour * 60 + utc.minute + utc.second / 60.0 + utc.microsecond / 6e7
    true_solar = (minutes + eq_time + 4 * lon) % 1440.0
    hour_angle = math.radians(true_solar / 4.0 - 180.0)

    latr = math.radians(lat)
    cos_zen = (
        math.sin(latr) * math.sin(decl)
        + math.cos(latr) * math.cos(decl) * math.cos(hour_angle)
    )
    zen = math.acos(max(-1.0, min(1.0, cos_zen)))
    elevation = 90.0 - math.degrees(zen)

    sin_zen = math.sin(zen)
    if sin_zen < 1e-9:
        azimuth = 180.0
    else:
        cos_az = (math.sin(latr) * cos_zen - math.sin(decl)) / (math.cos(latr) * sin_zen)
        az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
        azimuth = (az + 180.0) % 360.0 if hour_angle > 0 else (540.0 - az) % 360.0

    return azimuth, elevation + _refraction(elevation)


def _refraction(elevation: float) -> float:
    """Atmosphärische Refraktion in Grad (NOAA)."""
    if elevation > 85.0:
        return 0.0
    te = math.tan(math.radians(elevation))
    if elevation > 5.0:
        arc_sec = 58.1 / te - 0.07 / te ** 3 + 0.000086 / te ** 5
    elif elevation > -0.575:
        arc_sec = 1735.0 + elevation * (
            -518.2 + elevation * (103.4 + elevation * (-12.79 + elevation * 0.711))
        )
    else:
        arc_sec = -20.772 / te
    return arc_sec / 3600.0


def slot_sun_positions(
    slot_start: datetime, lat: float, lon: float, quarters: bool = False
) -> List[Tuple[float, float]]:
    """Sonnenstand eines Stundenslots: zur Slotmitte oder zu vier Viertelstunden-Mitten."""
    offsets = (7.5, 22.5, 37.5, 52.5) if quarters else (30.0,)
    return [sun_position(slot_start + timedelta(minutes=o), lat, lon) for o in offsets]


def shading_cell(az: float, el: float) -> str:
    """Schlüssel der Zelle, in der (az, el) liegt: "<az_unten>:<el_unten>"."""
    a = int(math.floor((az % 360.0) / SHADING_AZ_STEP) * SHADING_AZ_STEP)
    e = int(math.floor(el / SHADING_EL_STEP) * SHADING_EL_STEP)
    return f"{a}:{e}"


def _neighbour_keys(key: str, radius: int) -> List[str]:
    a, e = (int(x) for x in key.split(":"))
    n_az = int(round(360.0 / SHADING_AZ_STEP))
    out = []
    for da in range(-radius, radius + 1):
        ai = (round(a / SHADING_AZ_STEP) + da) % n_az
        for de in range(-radius, radius + 1):
            out.append(f"{int(ai * SHADING_AZ_STEP)}:{int(e + de * SHADING_EL_STEP)}")
    return out


def shading_day_samples(
    hours: Sequence[Dict], excluded_sources: Iterable[str] = ()
) -> Optional[Dict[int, float]]:
    """Normierte Stundenverhältnisse eines Tages, gemittelt über stabile Quellen.

    ``hours``: je Stunde ``{"az", "el", "actual": Wh, "forecast": {source: Wh}}``.
    Quellen in ``excluded_sources`` (z. B. mit eigenem Horizontprofil) gehen
    nicht ein. Rückgabe ``{Index in hours: Verhältnis}`` oder ``None``, wenn
    keine Quelle an diesem Tag stabil ist.
    """
    excluded = set(excluded_sources)
    sources = sorted({
        s for h in hours for s in h.get("forecast", {}) if s not in excluded
    })
    per_hour: Dict[int, List[float]] = {}
    for sid in sources:
        ref_ratios: List[float] = []
        ref_actual = ref_fc = 0.0
        for h in hours:
            fc = h.get("forecast", {}).get(sid)
            if fc is None or fc < SHADING_MIN_FORECAST_WH or h["el"] <= SHADING_REF_ELEVATION:
                continue
            ref_ratios.append(h["actual"] / fc)
            ref_actual += h["actual"]
            ref_fc += fc
        if len(ref_ratios) < SHADING_MIN_REF_HOURS or ref_actual <= 0:
            continue
        mean = sum(ref_ratios) / len(ref_ratios)
        if statistics.pstdev(ref_ratios) / mean > SHADING_MAX_REF_CV:
            continue
        day_ratio = ref_actual / ref_fc
        for i, h in enumerate(hours):
            fc = h.get("forecast", {}).get(sid)
            if fc is None or fc < SHADING_MIN_FORECAST_WH or h["el"] <= 0:
                continue
            per_hour.setdefault(i, []).append(h["actual"] / fc / day_ratio)
    if not per_hour:
        return None
    return {i: sum(v) / len(v) for i, v in per_hour.items()}


def learn_shading_map(
    days: Sequence[Sequence[Dict]],
    excluded_sources: Iterable[str] = (),
    min_samples: int = SHADING_MIN_SAMPLES,
) -> Tuple[ShadingCells, int]:
    """Verschattungskarte aus mehreren Tagen (je Tag eine Liste wie bei shading_day_samples).

    Rückgabe ``(cells, used_days)``. ``cells`` enthält jede Zelle, in deren
    Nachbarschaft (SHADING_POOL_RADIUS) mindestens eine Stichprobe liegt:
    Mittelpunkt, Median-Faktor der Nachbarschaft (begrenzt), eigene
    Stichproben ``n`` und Stichproben der Nachbarschaft ``n_pooled``.
    Gelernt (also angewendet) wird sie ab ``n_pooled`` ≥ ``min_samples``.
    """
    excluded = tuple(excluded_sources)
    buckets: Dict[str, List[float]] = {}
    used = 0
    for hours in days:
        samples = shading_day_samples(hours, excluded)
        if samples is None:
            continue
        used += 1
        for i, ratio in samples.items():
            key = shading_cell(hours[i]["az"], hours[i]["el"])
            buckets.setdefault(key, []).append(ratio)

    keys = {k for key in buckets for k in _neighbour_keys(key, SHADING_POOL_RADIUS)}
    cells: ShadingCells = {}
    for key in sorted(keys):
        pooled = [
            r for k in _neighbour_keys(key, SHADING_POOL_RADIUS) for r in buckets.get(k, ())
        ]
        a, e = (int(x) for x in key.split(":"))
        factor = min(SHADING_FACTOR_MAX, max(SHADING_FACTOR_MIN, statistics.median(pooled)))
        cells[key] = {
            "az": a + SHADING_AZ_STEP / 2,
            "el": e + SHADING_EL_STEP / 2,
            "factor": round(factor, 3),
            "n": len(buckets.get(key, ())),
            "n_pooled": len(pooled),
            "learned": len(pooled) >= min_samples,
        }
    return cells, used


def _effective_factor(cell: Dict) -> float:
    f = cell["factor"]
    return 1.0 if f >= SHADING_NEUTRAL_ABOVE else f


def shading_factor(cells: ShadingCells, az: float, el: float) -> float:
    """Verschattungsfaktor für einen Sonnenstand (1,0 = keine Korrektur).

    Bilinear zwischen den vier umgebenden Zellmittelpunkten; berücksichtigt
    werden nur gelernte Zellen, ihre Gewichte werden neu normiert. Gibt es
    keinen gelernten Nachbarn, bleibt es bei 1,0.
    """
    if not cells or el <= 0:
        return 1.0
    fa = (az % 360.0) / SHADING_AZ_STEP - 0.5
    fe = el / SHADING_EL_STEP - 0.5
    a0, e0 = math.floor(fa), math.floor(fe)
    ta, te = fa - a0, fe - e0
    n_az = int(round(360.0 / SHADING_AZ_STEP))
    w_sum = acc = 0.0
    for da, wa in ((0, 1 - ta), (1, ta)):
        for de, we in ((0, 1 - te), (1, te)):
            w = wa * we
            if w <= 0:
                continue
            ai = (a0 + da) % n_az
            key = f"{int(ai * SHADING_AZ_STEP)}:{int((e0 + de) * SHADING_EL_STEP)}"
            cell = cells.get(key)
            if cell is None or not cell.get("learned"):
                continue
            w_sum += w
            acc += w * _effective_factor(cell)
    if w_sum <= 0:
        return 1.0
    return acc / w_sum


def shading_slot_factor(
    cells: ShadingCells, positions: Sequence[Tuple[float, float]]
) -> float:
    """Mittlerer Faktor über die Sonnenstände eines Slots (siehe slot_sun_positions)."""
    if not cells or not positions:
        return 1.0
    return sum(shading_factor(cells, az, el) for az, el in positions) / len(positions)
