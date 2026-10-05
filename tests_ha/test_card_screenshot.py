"""Screenshot der Solar Fusion Card mit Zuständen aus der Integration.

Die Karte läuft in Chromium (Playwright) auf einer Testseite mit den Farben
des Standard-Themes; Zustände und Attribute liefert die Integration mit der
synthetischen Anlage, die Stundenstatistik ist eine Attrappe. Verglichen wird
mit ``docs/card.png``, das auch die README zeigt.

Nach einer gewollten Änderung der Karte das Bild neu schreiben:
``UPDATE_CARD_SCREENSHOT=1 pytest tests_ha/test_card_screenshot.py``
"""
import json
import os
from datetime import date, timedelta

import pytest

async_api = pytest.importorskip("playwright.async_api")
from PIL import Image, ImageChops  # noqa: E402
from homeassistant.helpers.json import JSONEncoder  # noqa: E402

from common import (  # noqa: E402
    PV, ROOT_DIR, STORE_KEY, at, set_sources, setup_entry, state, synthetic_day,
)

BASELINE = ROOT_DIR / "docs" / "card.png"
OUTPUT = ROOT_DIR / "tests_ha" / "card" / "output"
PAGE_DIR = ROOT_DIR / "tests_ha" / "card"
CARD_DIR = ROOT_DIR / "custom_components" / "solar_fusion" / "frontend"
ORIGIN = "http://card.test"

TODAY = "2026-10-04"
NOW = at(TODAY, 14, 30)
HISTORY_DAYS = 14
# Ein Pixel gilt als geändert, wenn es um mehr als PIXEL_TOLERANCE (0–255)
# abweicht; erlaubt ist ein Anteil von MAX_CHANGED aller Pixel. Das fängt
# Kantenglättung ab, aber keine verschobenen Texte, Balken oder Farben.
PIXEL_TOLERANCE = 32
MAX_CHANGED = 0.0005

_CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript",
                  ".json": "application/json", ".woff2": "font/woff2"}


def _history() -> list[dict]:
    """14 Tage Prognose (wie set_sources: Solcast 5 % höher) und Ist der synthetischen Anlage."""
    records = []
    for offset in range(HISTORY_DAYS, 0, -1):
        day = (date.fromisoformat(TODAY) - timedelta(days=offset)).isoformat()
        fc, ac = synthetic_day(day)
        forecast, actual = sum(fc.values()) / 1000, round(sum(ac.values()) / 1000, 3)
        for source, factor in (("open_meteo_solar_forecast", 1.0), ("solcast", 1.05)):
            records.append({"date": day, "source": source,
                            "forecast_kwh": round(forecast * factor, 3), "actual_kwh": actual})
    return records


def _statistics(actual: dict) -> dict:
    """Antwort von recorder/statistics_during_period je Periode: Stunden bis jetzt."""
    def row(start, kwh):
        return {"start": start.timestamp() * 1000, "change": round(kwh, 4)}
    hours = [row(at(TODAY, h), actual[f"{h:02d}"] / 1000) for h in range(NOW.hour)]
    minutes = [row(at(TODAY, NOW.hour, m), actual[f"{NOW.hour:02d}"] / 12000)
               for m in range(0, NOW.minute, 5)]
    return {"hour": hours, "5minute": minutes}


async def _card_states(hass, hass_storage, freezer) -> tuple[str, dict, dict]:
    """Integration starten; (Entity-ID forecast_today, Zustände der Karte, Statistik)."""
    freezer.move_to(NOW)
    hass_storage[STORE_KEY] = {"version": 2, "minor_version": 1, "key": STORE_KEY, "data": {
        "history": _history(), "calibration_state": {}, "morning_snapshots": {}}}
    set_sources(hass, tomorrow_shape_day="2026-09-27")
    meter = {"unit_of_measurement": "kWh", "state_class": "total_increasing",
             "device_class": "energy"}
    hass.states.async_set(PV, 1000.0, meter)
    await setup_entry(hass)
    _, actual = synthetic_day(TODAY)
    so_far = (sum(actual[f"{h:02d}"] for h in range(NOW.hour))
              + actual[f"{NOW.hour:02d}"] * NOW.minute / 60) / 1000
    hass.states.async_set(PV, round(1000.0 + so_far, 3), meter)
    await hass.async_block_till_done()

    today = state(hass, "forecast_today")
    states = {
        st.entity_id: st.as_dict()
        for st in (today, state(hass, "forecast_tomorrow"),
                   state(hass, "diagnostics_pv_daily_production"))
    }
    return today.entity_id, json.loads(json.dumps(states, cls=JSONEncoder)), _statistics(actual)


async def _serve(route):
    path = route.request.url.removeprefix(ORIGIN).split("?", 1)[0]
    if path == "/":
        file = PAGE_DIR / "page.html"
    elif path.startswith("/fonts/"):
        file = PAGE_DIR / path.removeprefix("/fonts/")
    elif path.startswith("/solar_fusion/"):
        file = CARD_DIR / path.removeprefix("/solar_fusion/")
    else:
        file = None
    if file is None or not file.is_file():
        await route.fulfill(status=404)
        return
    await route.fulfill(body=file.read_bytes(),
                        content_type=_CONTENT_TYPES.get(file.suffix, "application/octet-stream"))


async def _screenshot(entity_id: str, states: dict, statistics: dict) -> bytes:
    async with async_api.async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(
                viewport={"width": 492, "height": 600}, device_scale_factor=2,
                locale="en-US", timezone_id="Europe/Berlin",
            )
            await page.clock.set_fixed_time(NOW)
            await page.route(f"{ORIGIN}/**", _serve)
            await page.goto(f"{ORIGIN}/")
            await page.evaluate("""async ({entityId, states, statistics}) => {
                await import("/solar_fusion/solar-fusion-card.js?v=test");
                const card = document.querySelector("solar-fusion-card");
                card.setConfig({ entity: entityId });
                card.hass = {
                    language: "en",
                    states,
                    callWS: async (msg) => ({ [msg.statistic_ids[0]]: statistics[msg.period] || [] }),
                };
            }""", {"entityId": entity_id, "states": states, "statistics": statistics})
            # Übersetzungen und Stundenstatistik kommen asynchron
            await page.wait_for_function("""() => {
                const card = document.querySelector("solar-fusion-card");
                return Object.keys(card._locale).length > 0
                    && Object.keys(card._actualHourly).length > 0
                    && card.shadowRoot.querySelector("ha-card");
            }""")
            await page.evaluate("document.fonts.ready.then(() => true)")
            return await page.locator("solar-fusion-card").screenshot(animations="disabled")
        finally:
            await browser.close()


def _changed_share(actual: Image.Image, baseline: Image.Image) -> tuple[float, Image.Image | None]:
    if actual.size != baseline.size:
        return 1.0, None
    diff = ImageChops.difference(actual.convert("RGB"), baseline.convert("RGB")).convert("L")
    mask = diff.point(lambda v: 255 if v > PIXEL_TOLERANCE else 0)
    return mask.histogram()[255] / (actual.width * actual.height), mask


async def test_card_looks_like_the_reference(berlin, freezer, hass_storage, tmp_path):
    hass = berlin
    entity_id, states, statistics = await _card_states(hass, hass_storage, freezer)
    png = await _screenshot(entity_id, states, statistics)
    shot = tmp_path / "card.png"
    shot.write_bytes(png)
    actual = Image.open(shot)

    if os.environ.get("UPDATE_CARD_SCREENSHOT"):
        BASELINE.parent.mkdir(exist_ok=True)
        BASELINE.write_bytes(png)
        return
    if not BASELINE.is_file():
        pytest.fail(f"{BASELINE} fehlt; mit UPDATE_CARD_SCREENSHOT=1 anlegen")

    changed, mask = _changed_share(actual, Image.open(BASELINE))
    if changed > MAX_CHANGED:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        (OUTPUT / "card-actual.png").write_bytes(png)
        if mask is not None:
            highlight = Image.composite(Image.new("RGB", actual.size, (255, 0, 0)),
                                        actual.convert("RGB"), mask)
            highlight.save(OUTPUT / "card-diff.png")
        size = f"{actual.size} statt {Image.open(BASELINE).size}" if mask is None else ""
        pytest.fail(
            f"Karte weicht von {BASELINE.relative_to(ROOT_DIR)} ab: {changed:.2%} der Pixel "
            f"(erlaubt {MAX_CHANGED:.2%}) {size}. Bilder in {OUTPUT.relative_to(ROOT_DIR)}; "
            "gewollte Änderung: UPDATE_CARD_SCREENSHOT=1"
        )
