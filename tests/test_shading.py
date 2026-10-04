"""Tests für die Verschattung nach Sonnenstand (calc.py).

Läuft unter pytest und als Skript (``python3 tests/test_shading.py``), ohne
Home Assistant.
"""
import math
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "solar_fusion")
)
import calc  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")
# Neutraler Standort: Hamburg, Rathausmarkt
LAT, LON = 53.55, 9.99


# ── Sonnenstand ─────────────────────────────────────────────────────────────

# Referenz: pvlib.solarposition.spa_python (NREL SPA), apparent_elevation,
# für LAT/LON oben.
SPA_REFERENCE = [
    ("2026-09-22T17:37", 250.437, 14.120),
    ("2026-09-25T17:22", 246.770, 15.076),
    ("2026-09-27T17:22", 246.480, 14.333),
    ("2026-09-30T17:22", 246.043, 13.224),
    ("2026-10-04T17:07", 242.218, 13.754),
    ("2026-06-21T13:00", 170.060, 59.615),
    ("2026-12-21T09:00", 135.722, 2.233),
    ("2026-03-20T07:00", 96.626, 4.859),
]


def _at(local_iso):
    return datetime.fromisoformat(local_iso).replace(tzinfo=TZ)


def test_sun_position_matches_spa():
    for ts, az_ref, el_ref in SPA_REFERENCE:
        az, el = calc.sun_position(_at(ts), LAT, LON)
        assert abs(az - az_ref) < 0.02, (ts, az, az_ref)
        assert abs(el - el_ref) < 0.02, (ts, el, el_ref)


def test_sun_position_requires_aware_datetime():
    try:
        calc.sun_position(datetime(2026, 10, 4, 12), LAT, LON)
    except ValueError:
        return
    raise AssertionError("naive datetime accepted")


def test_sun_position_timezone_independent():
    a = calc.sun_position(_at("2026-10-04T17:07"), LAT, LON)
    b = calc.sun_position(datetime(2026, 10, 4, 15, 7, tzinfo=timezone.utc), LAT, LON)
    assert a == b


def test_slot_positions_midpoint_and_quarters():
    start = _at("2026-10-04T17:00")
    (mid,) = calc.slot_sun_positions(start, LAT, LON)
    assert mid == calc.sun_position(start + timedelta(minutes=30), LAT, LON)
    quarters = calc.slot_sun_positions(start, LAT, LON, quarters=True)
    assert len(quarters) == 4
    assert quarters[0][0] < mid[0] < quarters[-1][0]       # Azimut wächst am Nachmittag
    assert quarters[0][1] > mid[1] > quarters[-1][1]       # Höhe fällt


# ── Zellen ──────────────────────────────────────────────────────────────────

def test_shading_cell_keys():
    assert calc.shading_cell(247.9, 13.9) == "245:12"
    assert calc.shading_cell(245.0, 12.0) == "245:12"
    assert calc.shading_cell(359.9, 0.5) == "355:0"
    assert calc.shading_cell(-2.0, 3.0) == "355:2"


def _cell(factor, n=5, learned=True, key="245:12"):
    a, e = (int(x) for x in key.split(":"))
    return {key: {"az": a + 2.5, "el": e + 1.0, "factor": factor, "n": n, "learned": learned}}


def test_factor_at_cell_centre_and_unlearned_neighbours():
    cells = _cell(0.2)
    assert abs(calc.shading_factor(cells, 247.5, 13.0) - 0.2) < 1e-12
    # Zwischen gelerntem und ungelerntem Mittelpunkt: keine Mischung mit 1,0
    assert abs(calc.shading_factor(cells, 249.0, 13.5) - 0.2) < 1e-12
    # Weit weg: keine gelernten Nachbarn → 1,0
    assert calc.shading_factor(cells, 200.0, 13.0) == 1.0
    assert calc.shading_factor(cells, 247.5, 25.0) == 1.0


def test_factor_bilinear_between_learned_cells():
    cells = {**_cell(0.2, key="245:12"), **_cell(0.6, key="250:12")}
    # Halbe Strecke zwischen den Mittelpunkten 247,5° und 252,5°
    assert abs(calc.shading_factor(cells, 250.0, 13.0) - 0.4) < 1e-12


def test_factor_ignores_cells_below_min_samples():
    cells = _cell(0.2, n=2, learned=False)
    assert calc.shading_factor(cells, 247.5, 13.0) == 1.0


def test_factor_neutral_band():
    # 0,93 ist Rauschen der Normierung, keine Verschattung
    assert calc.shading_factor(_cell(0.93), 247.5, 13.0) == 1.0
    assert abs(calc.shading_factor(_cell(0.85), 247.5, 13.0) - 0.85) < 1e-12


def test_slot_factor_ignores_unknown_positions():
    # Drei Viertelstunden im gelernten Schatten, die letzte (tiefer) noch unbekannt:
    # sie darf den Schatten nicht mit 1,0 verwässern.
    cells = _cell(0.2)
    positions = [(247.5, 13.0), (247.5, 13.0), (247.5, 13.0), (300.0, 2.0)]
    assert abs(calc.shading_slot_factor(cells, positions) - 0.2) < 1e-12
    # Keine Position bekannt → 1,0
    assert calc.shading_slot_factor(cells, [(300.0, 2.0), (120.0, 30.0)]) == 1.0
    # Bekannt und neutral (≥ 0,9) zählt als 1,0 mit
    neutral = {**cells, **_cell(0.95, key="295:2")}
    assert abs(calc.shading_slot_factor(neutral, positions) - (0.2 * 3 + 1.0) / 4) < 1e-12


def test_factor_below_horizon_and_empty():
    assert calc.shading_factor(_cell(0.2), 247.5, -1.0) == 1.0
    assert calc.shading_factor({}, 247.5, 13.0) == 1.0
    assert calc.shading_slot_factor({}, [(247.5, 13.0)]) == 1.0


# ── Lernen: synthetische Anlage mit hartem Horizont ─────────────────────────
#
# Dach 40° Neigung, Azimut 205° (SSW). Horizont: zwischen 235° und 260° ist
# unter 14° Sonnenhöhe die Direktstrahlung weg, nur Diffuslicht bleibt. Die
# Prognose kennt den Horizont nicht. Ist und Prognose werden je Viertelstunde
# gerechnet und zur Stunde summiert, so wie es in der Wirklichkeit passiert.

HORIZON = (235.0, 260.0, 14.0)


def _clear_sky(az, el):
    if el <= 0:
        return 0.0, 0.0
    zen, tilt = math.radians(90 - el), math.radians(40)
    cos_i = (math.cos(zen) * math.cos(tilt)
             + math.sin(zen) * math.sin(tilt) * math.cos(math.radians(az - 205)))
    air_mass = 1 / max(math.sin(math.radians(el)), 0.05)
    beam = 1000 * 0.7 ** (air_mass ** 0.678) * max(cos_i, 0.0)
    diffuse = 120 * math.sin(math.radians(el)) ** 0.5
    return beam * 10.2, diffuse * 10.2


def _shaded(az, el):
    return HORIZON[0] < az < HORIZON[1] and el < HORIZON[2]


def _synthetic_day(day, rng, cloudy=False):
    """Eine Liste Stunden wie für calc.learn_shading_map plus Wahrheit/Startzeit."""
    day_cloud = rng.uniform(0.6, 1.0)
    hours = []
    for h in range(5, 21):
        start = datetime(day.year, day.month, day.day, h, tzinfo=TZ)
        forecast = actual = 0.0
        for az, el in calc.slot_sun_positions(start, LAT, LON, quarters=True):
            beam, diffuse = _clear_sky(az, el)
            forecast += (beam + diffuse) / 4
            actual += ((0.0 if _shaded(az, el) else beam) + diffuse) / 4
        (az, el), = calc.slot_sun_positions(start, LAT, LON)
        if el <= 0:
            continue
        hour_cloud = rng.uniform(0.15, 1.3) if cloudy else day_cloud * rng.uniform(0.97, 1.03)
        hours.append({
            "az": az, "el": el,
            "actual": actual * hour_cloud,
            "forecast": {"om": forecast * day_cloud, "sc": forecast * day_cloud * 1.08},
            "_true": actual * day_cloud,
            "_start": start,
        })
    return hours


def _season(n_days=50, first=date(2026, 9, 1), cloudy_every=0, seed=7):
    rng = random.Random(seed)
    out = []
    for i in range(n_days):
        cloudy = bool(cloudy_every) and i % cloudy_every == cloudy_every - 1
        out.append((cloudy, _synthetic_day(first + timedelta(days=i), rng, cloudy)))
    return out


def test_cloudy_day_is_rejected():
    rng = random.Random(3)
    cloudy = _synthetic_day(date(2026, 9, 20), rng, cloudy=True)
    clear = _synthetic_day(date(2026, 9, 21), rng)
    assert calc.shading_day_samples(cloudy) is None
    assert calc.shading_day_samples(clear) is not None


def test_day_without_reference_hours_is_rejected():
    # Ende November steht die Sonne nie über 25° → keine Referenzstunden
    day = _synthetic_day(date(2026, 11, 28), random.Random(1))
    assert max(h["el"] for h in day) < calc.SHADING_REF_ELEVATION
    assert calc.shading_day_samples(day) is None


def test_learned_map_shows_horizon():
    season = _season()
    cells, used = calc.learn_shading_map([d for _, d in season])
    # Ab Mitte Oktober gibt es keine drei Stunden über 25° mehr – diese Tage fehlen
    with_ref = [d for _, d in season
                if sum(h["el"] > calc.SHADING_REF_ELEVATION for h in d) >= calc.SHADING_MIN_REF_HOURS]
    assert used == len(with_ref) >= 40
    learned = {k: c for k, c in cells.items() if c["learned"]}
    assert learned

    # Slotmitten klar im Schatten (Azimut 240–255°, Höhe ≤ 12°): Faktor klein
    inside = [c for c in learned.values()
              if 240 <= c["az"] <= 255 and c["el"] <= 11]
    assert inside, "kein gelernter Schattenbereich"
    assert all(c["factor"] < 0.5 for c in inside), inside

    # Hohe Sonne und Vormittag: keine Verschattung
    open_sky = [c for c in learned.values() if c["el"] >= 18 or c["az"] < 200]
    assert open_sky
    assert all(c["factor"] >= calc.SHADING_NEUTRAL_ABOVE for c in open_sky), [
        c for c in open_sky if c["factor"] < calc.SHADING_NEUTRAL_ABOVE
    ]

    # Angewendet: 17-Uhr-Slot am 04.10. (Mitte 242°/11°) deutlich gedämpft
    f = calc.shading_factor(cells, *calc.sun_position(_at("2026-10-04T17:30"), LAT, LON))
    assert f < 0.5


def test_cloudy_days_do_not_enter_the_map():
    season = _season(n_days=60, cloudy_every=4)
    n_cloudy = sum(1 for cloudy, _ in season if cloudy)
    assert n_cloudy == 15
    assert all(calc.shading_day_samples(d) is None for c, d in season if c)
    cells_all, used_all = calc.learn_shading_map([d for _, d in season])
    cells_clear, used_clear = calc.learn_shading_map([d for c, d in season if not c])
    assert used_all == used_clear > 0
    assert cells_all == cells_clear


def test_factor_bounds():
    # Ist praktisch 0 in einer Stunde → Faktor nicht unter 0,05;
    # Ist deutlich über Prognose → nicht über 1,0
    days = []
    for _ in range(4):
        hours = [{"az": 180.0 + i, "el": 30.0, "actual": 2000.0, "forecast": {"x": 2000.0}}
                 for i in range(4)]
        hours.append({"az": 250.0, "el": 10.0, "actual": 0.0, "forecast": {"x": 1000.0}})
        hours.append({"az": 120.0, "el": 10.0, "actual": 1800.0, "forecast": {"x": 1000.0}})
        days.append(hours)
    cells, used = calc.learn_shading_map(days)
    assert used == 4
    assert cells[calc.shading_cell(250.0, 10.0)]["factor"] == calc.SHADING_FACTOR_MIN
    assert cells[calc.shading_cell(120.0, 10.0)]["factor"] == calc.SHADING_FACTOR_MAX


def test_min_samples_per_cell():
    hours = [{"az": 180.0 + i, "el": 30.0, "actual": 2000.0, "forecast": {"x": 2000.0}}
             for i in range(4)]
    hours.append({"az": 250.0, "el": 10.0, "actual": 200.0, "forecast": {"x": 1000.0}})
    key = calc.shading_cell(250.0, 10.0)
    cells, _ = calc.learn_shading_map([hours] * (calc.SHADING_MIN_SAMPLES - 1))
    assert cells[key]["learned"] is False
    assert calc.shading_factor(cells, 250.0, 10.0) == 1.0
    cells, _ = calc.learn_shading_map([hours] * calc.SHADING_MIN_SAMPLES)
    assert cells[key]["learned"] is True
    assert abs(calc.shading_factor(cells, 252.5, 11.0) - 0.2) < 1e-9


def test_excluded_source_is_not_learned():
    # Quelle "om" enthält schon den Horizont: ihre Stunden zeigen keinen Einbruch.
    hours = [{"az": 180.0 + i, "el": 30.0, "actual": 2000.0,
              "forecast": {"om": 2000.0, "sc": 2000.0}} for i in range(4)]
    hours.append({"az": 250.0, "el": 10.0, "actual": 200.0,
                  "forecast": {"om": 200.0, "sc": 1000.0}})
    cells, _ = calc.learn_shading_map([hours] * 3)
    assert abs(cells[calc.shading_cell(250, 10)]["factor"] - 0.6) < 1e-9  # Mittel aus 1,0 und 0,2
    cells, _ = calc.learn_shading_map([hours] * 3, excluded_sources=["om"])
    assert abs(cells[calc.shading_cell(250, 10)]["factor"] - 0.2) < 1e-9


def _apply_rmse(cells, test_days, mode):
    se = n = 0
    for hours in test_days:
        for h in hours:
            if mode == "none":
                f = 1.0
            elif mode == "mid":
                f = calc.shading_factor(cells, h["az"], h["el"])
            else:
                f = calc.shading_slot_factor(
                    cells, calc.slot_sun_positions(h["_start"], LAT, LON, quarters=True))
            se += (h["forecast"]["om"] * f - h["_true"]) ** 2
            n += 1
    return math.sqrt(se / n)


def test_quarter_hours_beat_midpoint_when_applying():
    """Vorgabe 6: Viertelstunden beim Anwenden gegen Slotmitte.

    Karte aus einer Saison mit 30 % klaren Tagen, angewendet auf klare Tage
    des Folgejahres. Gelernt wird immer mit der Slotmitte; beim Anwenden
    überbrücken die Viertelstunden Lücken der Karte entlang des Sonnenwegs.
    """
    rng = random.Random(0)
    year1 = [_synthetic_day(date(2026, 8, 1) + timedelta(days=i), rng, rng.random() > 0.3)
             for i in range(90)]
    rng = random.Random(100)
    year2 = [_synthetic_day(date(2027, 8, 1) + timedelta(days=i), rng) for i in range(90)]
    cells, _ = calc.learn_shading_map(year1)
    none = _apply_rmse(cells, year2, "none")
    mid = _apply_rmse(cells, year2, "mid")
    quarters = _apply_rmse(cells, year2, "quarters")
    assert mid < 0.5 * none
    assert quarters < 0.9 * mid, (none, mid, quarters)
    assert calc.SHADING_APPLY_QUARTERS is True


def test_neighbour_pooling_fills_sparse_map():
    """Ohne Nachbarschaft erreichen bei 40 % klaren Tagen kaum Zellen die Mindestzahl."""
    rng = random.Random(5)
    days = [_synthetic_day(date(2026, 8, 15) + timedelta(days=i), rng, rng.random() > 0.4)
            for i in range(40)]
    cells, used = calc.learn_shading_map(days)
    own_only = sum(1 for c in cells.values() if c["n"] >= calc.SHADING_MIN_SAMPLES)
    pooled = sum(1 for c in cells.values() if c["learned"])
    assert used >= 10
    assert pooled > 2 * own_only


# ── plain-script runner (no pytest needed) ──────────────────────────────────

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "->", e or "assertion")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, "->", repr(e))
    print("\n%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
