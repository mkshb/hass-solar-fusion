"""Parser der Quellen (source_reader.py): Stundenwerte und Zeitstempel.

Läuft unter pytest und als Skript, ohne Home Assistant (siehe ha_stub.py;
Zeitzone Europe/Berlin).
"""
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import ha_stub  # noqa: E402

PKG = os.path.join(HERE, "..", "custom_components", "solar_fusion")
sr = ha_stub.load_module(PKG, "source_reader", "sf_reader")
TZ = ha_stub.TZ


# ── _normalise_ts ───────────────────────────────────────────────────────────

def test_normalise_iso_with_offset_and_seconds():
    assert sr._normalise_ts("2026-10-04T17:00:00+02:00") == "2026-10-04T17:00"
    assert sr._normalise_ts("2026-10-04T17:45:00+02:00") == "2026-10-04T17:00"


def test_normalise_converts_utc_to_local():
    assert sr._normalise_ts("2026-10-04T15:00:00+00:00") == "2026-10-04T17:00"
    assert sr._normalise_ts("2026-10-04T15:30:00Z") == "2026-10-04T17:00"
    assert sr._normalise_ts("2026-12-04T15:30:00Z") == "2026-12-04T16:00"   # Winterzeit


def test_normalise_naive_is_taken_as_local():
    assert sr._normalise_ts("2026-10-04T08:15:00") == "2026-10-04T08:00"
    assert sr._normalise_ts("2026-10-04T08:15") == "2026-10-04T08:00"


def test_normalise_datetime_objects():
    aware = datetime(2026, 10, 4, 15, 20, tzinfo=timezone.utc)
    assert sr._normalise_ts(aware) == "2026-10-04T17:00"
    assert sr._normalise_ts(datetime(2026, 10, 4, 9, 59)) == "2026-10-04T09:00"


def test_normalise_unix_timestamp():
    ts = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc).timestamp()
    assert sr._normalise_ts(ts) == "2026-10-04T17:00"
    assert sr._normalise_ts(int(ts)) == "2026-10-04T17:00"


def test_normalise_unparseable_is_truncated():
    assert sr._normalise_ts("garbage") == "garbage"
    assert sr._normalise_ts("2026-10-04 something else") == "2026-10-04 somet"   # 16 Zeichen


# ── wh_hours / wh_period ────────────────────────────────────────────────────

def test_wh_period_keys_are_period_start():
    raw = {
        "2026-10-04T07:00:00+02:00": 51,
        "2026-10-04T08:00:00+02:00": 621.5,
        "2026-10-04T17:00:00+02:00": 2508.25,
    }
    assert sr._extract_wh_hours(raw) == {
        "2026-10-04T07:00": 51.0,
        "2026-10-04T08:00": 621.5,
        "2026-10-04T17:00": 2508.25,
    }


def test_dst_end_sums_the_doubled_hour():
    # 25.10.2026: 02:00–03:00 gibt es zweimal (CEST, dann CET)
    raw = {
        "2026-10-25T01:00:00+02:00": 1,
        "2026-10-25T02:00:00+02:00": 2,
        "2026-10-25T02:00:00+01:00": 3,
        "2026-10-25T03:00:00+01:00": 4,
    }
    assert sr._extract_wh_hours(raw) == {
        "2026-10-25T01:00": 1.0,
        "2026-10-25T02:00": 5.0,
        "2026-10-25T03:00": 4.0,
    }
    # Periodenende (Forecast.Solar): 03:00+01:00 ist das Ende der zweiten 02-Uhr-Stunde
    end = {"2026-10-25T02:00:00+01:00": 2, "2026-10-25T03:00:00+01:00": 3}
    assert sr._extract_wh_hours(end, period_end=True) == {"2026-10-25T02:00": 5.0}


def test_dst_start_has_no_02_slot():
    # 29.03.2026: auf 01:59 CET folgt 03:00 CEST
    raw = {"2026-03-29T01:00:00+01:00": 1, "2026-03-29T03:00:00+02:00": 2}
    assert sr._extract_wh_hours(raw) == {"2026-03-29T01:00": 1.0, "2026-03-29T03:00": 2.0}


def test_wh_hours_period_end_moves_to_previous_hour():
    # Forecast.Solar-API: Wert gilt vom letzten bis zu diesem Zeitstempel
    raw = {
        "2026-10-04 07:13:00": 0,       # Sonnenaufgang
        "2026-10-04 08:00:00": 349,     # 07:13–08:00
        "2026-10-04 09:00:00": 1200,
        "2026-10-04 18:42:00": 40,      # Sonnenuntergang: 18:00–18:42
    }
    assert sr._extract_wh_hours(raw, period_end=True) == {
        "2026-10-04T07:00": 349.0,
        "2026-10-04T08:00": 1200.0,
        "2026-10-04T18:00": 40.0,
    }


def test_wh_hours_period_end_with_offset_and_datetime_keys():
    raw = {
        datetime(2026, 10, 4, 10, 0, tzinfo=TZ): 500,
        "2026-10-04T11:00:00+02:00": 700,
    }
    assert sr._extract_wh_hours(raw, period_end=True) == {
        "2026-10-04T09:00": 500.0,
        "2026-10-04T10:00": 700.0,
    }


def test_wh_hours_skips_invalid_values():
    raw = {"2026-10-04T08:00:00+02:00": "n/a", "2026-10-04T09:00:00+02:00": None,
           "2026-10-04T10:00:00+02:00": "12.5"}
    assert sr._extract_wh_hours(raw) == {"2026-10-04T10:00": 12.5}
    assert sr._extract_wh_hours(raw, period_end=True) == {"2026-10-04T09:00": 12.5}


class _State:
    def __init__(self, attributes):
        self.attributes = attributes


def test_open_meteo_attribute_fallback():
    period = {"2026-10-04T08:00:00+02:00": 1}
    hours = {"2026-10-04T09:00:00+02:00": 2}
    assert sr._open_meteo_hourly_attr(_State({"wh_period": period, "wh_hours": hours})) == period
    assert sr._open_meteo_hourly_attr(_State({"wh_hours": hours})) == hours
    assert sr._open_meteo_hourly_attr(_State({"wh_period": {}, "wh_hours": hours})) == hours
    assert sr._open_meteo_hourly_attr(_State({})) == {}


# ── Solcast detailedHourly ──────────────────────────────────────────────────

def test_solcast_kwh_to_wh_and_local_hours():
    slots = [
        {"period_start": "2026-10-04T17:00:00+02:00", "pv_estimate": 3.6585},
        {"period_start": "2026-10-04T16:00:00+00:00", "pv_estimate": 0.8496},   # = 18:00 lokal
    ]
    out = sr._extract_solcast_hourly(slots)
    assert set(out) == {"2026-10-04T17:00", "2026-10-04T18:00"}
    assert abs(out["2026-10-04T17:00"] - 3658.5) < 1e-6
    assert abs(out["2026-10-04T18:00"] - 849.6) < 1e-6


def test_solcast_sums_sub_hourly_periods_and_skips_missing_start():
    slots = [
        {"period_start": "2026-10-04T12:00:00+02:00", "pv_estimate": 1.0},
        {"period_start": "2026-10-04T12:30:00+02:00", "pv_estimate": 1.5},
        {"pv_estimate": 9.0},
        {"period_start": "", "pv_estimate": 9.0},
    ]
    assert sr._extract_solcast_hourly(slots) == {"2026-10-04T12:00": 2500.0}


# ── Rekonstruktion aus Recorder-Attributen ──────────────────────────────────

def test_hourly_from_attributes_per_source():
    om = {"wh_period": {"2026-10-04T12:00:00+02:00": 5000}}
    fs = {"wh_hours": {"2026-10-04 13:00:00": 4000}}
    sc = {"detailedHourly": [{"period_start": "2026-10-04T12:00:00+02:00", "pv_estimate": 4.5}]}
    assert sr.hourly_from_attributes("open_meteo_solar_forecast", om) == {"2026-10-04T12:00": 5000.0}
    assert sr.hourly_from_attributes("forecast_solar", fs) == {"2026-10-04T12:00": 4000.0}
    assert sr.hourly_from_attributes("solcast", sc) == {"2026-10-04T12:00": 4500.0}
    # Solcast ohne aufgezeichnetes detailedHourly, unbekannte Quelle
    assert sr.hourly_from_attributes("solcast", {"estimate": 40.0}) == {}
    assert sr.hourly_from_attributes("unknown", om) == {}


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
