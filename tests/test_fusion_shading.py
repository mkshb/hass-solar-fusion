"""FusionEngine mit Verschattung: Anwenden je Quelle, Schritt 7, Kalibrierungsreihenfolge.

Läuft unter pytest und als Skript, ohne Home Assistant (siehe ha_stub.py).
"""
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import ha_stub  # noqa: E402
from test_fusion_regression import PKG, TODAY, TOMORROW, _bell  # noqa: E402

fusion = ha_stub.load_fusion(PKG, "sf_shading")
calc = sys.modules["sf_shading.calc"]
LOCATION = (53.55, 9.99)   # Hamburg, Rathausmarkt


def _cells_around(slot_start: datetime, factor: float):
    """Gelernte Zellen um die Slotmitte, sodass dort genau ``factor`` gilt."""
    (az, el), = calc.slot_sun_positions(slot_start, *LOCATION)
    cells = {}
    # ±2 Zellen: deckt auch die Viertelstunden-Positionen des Slots ab
    for da in [i * calc.SHADING_AZ_STEP for i in range(-2, 3)]:
        for de in [i * calc.SHADING_EL_STEP for i in range(-2, 3)]:
            key = calc.shading_cell(az + da, el + de)
            a, e = (int(x) for x in key.split(":"))
            cells[key] = {"az": a + 2.5, "el": e + 1.0, "factor": factor, "n": 5,
                          "n_pooled": 45, "learned": True}
    return cells


CELLS = {
    **_cells_around(datetime(2026, 10, 4, 17, tzinfo=ha_stub.TZ), 0.2),
    **_cells_around(datetime(2026, 10, 5, 17, tzinfo=ha_stub.TZ), 0.2),
}
SLOT17 = f"{TODAY.isoformat()}T17:00"


def _reading(sid, today_kwh, tomorrow_kwh=30.0, hourly=True):
    return fusion.SourceReading(
        source_id=sid,
        today_kwh=today_kwh,
        tomorrow_kwh=tomorrow_kwh,
        hourly_today=_bell(TODAY, today_kwh, peak=14.5, sigma=3.0) if hourly else {},
        hourly_tomorrow=_bell(TOMORROW, tomorrow_kwh, peak=14.5, sigma=3.0) if hourly else {},
    )


def _engine(history=None, **kw):
    ha_stub.set_now(datetime(2026, 10, 4, 12, 0, tzinfo=ha_stub.TZ))
    kw.setdefault("location", LOCATION)
    kw.setdefault("shading_apply", True)
    kw.setdefault("shading_cells", CELLS)
    return fusion.FusionEngine(list(history or []), calibration_state={}, **kw)


def _history(days, sid, raw_over_actual, corrected_over_actual=None):
    out = []
    for i in range(days, 0, -1):
        actual = 20.0 + i % 7
        rec = {"date": (TODAY - timedelta(days=i)).isoformat(), "source": sid,
               "forecast_kwh": round(actual * raw_over_actual, 3), "actual_kwh": actual}
        if corrected_over_actual is not None:
            rec["forecast_corrected_kwh"] = round(actual * corrected_over_actual, 3)
        out.append(rec)
    return out


def test_slot_factor_applied_and_daily_total_follows():
    readings = [_reading("open_meteo_solar_forecast", 38.0), _reading("solcast", 41.0)]
    plain, _, _ = _engine(shading_apply=False).fuse(readings, TODAY)
    engine = _engine()
    shaded, _, _ = engine.fuse(readings, TODAY)

    assert abs(shaded[SLOT17] / plain[SLOT17] - 0.2) < 0.01
    # Schritt 7 hebt die Abschwächung nicht auf: nur die Stunden um den Schatten
    # ändern sich, die übrigen werden nicht hochskaliert; die Tagessumme sinkt.
    lost = sum(plain.values()) - sum(shaded.values())
    changed = {s for s in plain if abs(shaded[s] - plain[s]) > 0.15}
    assert SLOT17 in changed and changed <= {SLOT17, f"{TODAY.isoformat()}T16:00",
                                             f"{TODAY.isoformat()}T18:00"}, changed
    assert lost > 0.75 * plain[SLOT17]
    ratios = engine.last_shading_ratios[TODAY.isoformat()]
    assert all(r < 1.0 for r in ratios.values())


def test_uncertainty_uses_shaded_daily_totals():
    readings = [_reading("open_meteo_solar_forecast", 38.0), _reading("solcast", 41.0)]
    _, plain_pct, _ = _engine(shading_apply=False).fuse(readings, TODAY)
    engine = _engine()
    shaded, shaded_pct, weights = engine.fuse(readings, TODAY)
    ratios = engine.last_shading_ratios[TODAY.isoformat()]
    assert all(r < 1.0 for r in ratios.values())
    expected = calc.weighted_spread_pct(
        {"open_meteo_solar_forecast": 38.0 * ratios["open_meteo_solar_forecast"],
         "solcast": 41.0 * ratios["solcast"]},
        weights, sum(shaded.values()) / 1000.0,
    )
    assert shaded_pct == expected
    # Gleiche Basis wie die fusionierte Summe: die Streuung wächst nicht um 1/Anteil
    assert abs(shaded_pct - plain_pct) <= 0.2


def test_tomorrow_corrected_too():
    readings = [_reading("solcast", 41.0, tomorrow_kwh=35.0)]
    plain, _, _ = _engine(shading_apply=False).fuse(readings, TOMORROW)
    shaded, _, _ = _engine().fuse(readings, TOMORROW)
    slot = f"{TOMORROW.isoformat()}T17:00"
    assert abs(shaded[slot] / plain[slot] - 0.2) < 0.01
    assert sum(shaded.values()) < sum(plain.values())


def test_horizon_source_is_not_corrected():
    readings = [_reading("open_meteo_solar_forecast", 38.0)]
    plain, _, _ = _engine(shading_apply=False).fuse(readings, TODAY)
    same, _, _ = _engine(horizon_sources=["open_meteo_solar_forecast"]).fuse(readings, TODAY)
    assert same == plain


def test_mixed_horizon_and_corrected_source():
    om, sc = _reading("open_meteo_solar_forecast", 38.0), _reading("solcast", 38.0)
    plain, _, _ = _engine(shading_apply=False).fuse([om, sc], TODAY)
    mixed, _, _ = _engine(horizon_sources=["open_meteo_solar_forecast"]).fuse([om, sc], TODAY)
    # Gleiche Gewichte: 17 Uhr = Mittel aus unkorrigiert (1,0) und korrigiert (0,2)
    assert abs(mixed[SLOT17] / plain[SLOT17] - 0.6) < 0.01


def test_raw_mode_with_learned_calibration_only_redistributes():
    # Anlaufphase: kalibriert wird auf roher Historie, die den mittleren
    # Verschattungsverlust schon enthält → Tagessumme bleibt, Form ändert sich.
    hist = _history(5, "solcast", raw_over_actual=1.25)
    readings = [_reading("solcast", 41.0)]
    plain, _, _ = _engine(hist, shading_apply=False).fuse(readings, TODAY)
    engine = _engine(hist)
    shaded, _, _ = engine.fuse(readings, TODAY)
    assert engine.compute_weights(["solcast"])["solcast"]["shading_corrected_history"] is False
    assert abs(sum(shaded.values()) - sum(plain.values())) < 1.0
    assert shaded[SLOT17] < 0.3 * plain[SLOT17]


def test_raw_mode_without_calibration_data_reduces_total():
    readings = [_reading("solcast", 41.0)]
    plain, _, _ = _engine(shading_apply=False).fuse(readings, TODAY)
    shaded, _, _ = _engine().fuse(readings, TODAY)
    assert sum(shaded.values()) < sum(plain.values()) - 0.5 * plain[SLOT17]


def test_corrected_mode_calibrates_on_corrected_history():
    # Rohprognose 25 % zu hoch, korrigierte trifft genau: im Modus „korrigiert“
    # lernt die Kalibrierung Faktor 1,0, der Tageswert ist roh × ratio.
    hist = _history(10, "solcast", raw_over_actual=1.25, corrected_over_actual=1.0)
    readings = [_reading("solcast", 41.0)]
    engine = _engine(hist)
    info = engine.compute_weights(["solcast"])["solcast"]
    assert info["shading_corrected_history"] is True
    assert info["rmse_calibrated"] == 0.0 and info["rmse_raw"] == 0.0
    shaded, _, _ = engine.fuse(readings, TODAY)
    ratio = engine.last_shading_ratios[TODAY.isoformat()]["solcast"]
    assert abs(sum(shaded.values()) - 41000.0 * ratio) < 2.0

    # Ohne Anwenden: Modus „roh“, der Bias 1,25 wird herauskalibriert
    raw_engine = _engine(hist, shading_apply=False)
    assert raw_engine.compute_weights(["solcast"])["solcast"]["shading_corrected_history"] is False
    plain, _, _ = raw_engine.fuse(readings, TODAY)
    assert abs(sum(plain.values()) - 41000.0 / 1.25) < 2.0


def test_corrected_mode_needs_min_eval_days():
    hist = _history(6, "solcast", 1.25, 1.0)
    assert _engine(hist).compute_weights(["solcast"])["solcast"]["shading_corrected_history"] is False


def test_profile_fallback_is_corrected():
    # Keine Stundenwerte für morgen → Profil aus heutigen Stunden
    r = _reading("solcast", 41.0, tomorrow_kwh=35.0)
    r.hourly_tomorrow = {}
    plain, _, _ = _engine(shading_apply=False).fuse([r], TOMORROW)
    shaded, _, _ = _engine().fuse([r], TOMORROW)
    slot = f"{TOMORROW.isoformat()}T17:00"
    assert abs(shaded[slot] / plain[slot] - 0.2) < 0.01
    assert sum(shaded.values()) < sum(plain.values())


def test_source_without_hourly_gets_mean_ratio():
    om = _reading("open_meteo_solar_forecast", 38.0)
    fs = _reading("forecast_solar", 38.0, hourly=False)
    engine = _engine()
    engine.fuse([om, fs], TODAY)
    ratios = engine.last_shading_ratios[TODAY.isoformat()]
    assert ratios["forecast_solar"] == ratios["open_meteo_solar_forecast"] < 1.0


def test_record_actual_stores_corrected():
    engine = _engine()
    engine.record_actual(date(2026, 10, 3), 30.0,
                         [fusion.SourceReading("solcast", 35.0, 0.0)],
                         corrected={"solcast": 31.23456})
    rec = engine._history[-1]
    assert rec["forecast_kwh"] == 35.0 and rec["forecast_corrected_kwh"] == 31.235


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
