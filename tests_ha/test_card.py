"""Solar Fusion Card: Auslieferung mit der Integration und Vertrag mit den Sensoren."""
import json
import re

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from common import ROOT_DIR, STORE_KEY, set_sources, setup_entry, state

MANIFEST = json.loads((ROOT_DIR / "custom_components/solar_fusion/manifest.json").read_text())
CARD_URL = f"/solar_fusion/solar-fusion-card.js?v={MANIFEST['version']}"
CARD_JS = (ROOT_DIR / "custom_components/solar_fusion/frontend/solar-fusion-card.js").read_text()


def _fake_frontend(hass) -> UrlManager:
    """Das Frontend selbst braucht das Paket home-assistant-frontend; nur die URL-Liste."""
    hass.config.components.add("frontend")
    hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *_: None, [])
    return hass.data[DATA_EXTRA_MODULE_URL]


async def test_card_and_locales_are_served(berlin, hass_client):
    hass = berlin
    set_sources(hass)
    await setup_entry(hass)
    client = await hass_client()

    resp = await client.get(CARD_URL)
    assert resp.status == 200
    assert 'customElements.define("solar-fusion-card"' in await resp.text()

    for lang in ("de", "en"):
        resp = await client.get(f"/solar_fusion/locales/{lang}.json?v={MANIFEST['version']}")
        assert resp.status == 200
        assert "today" in json.loads(await resp.text())


async def test_card_is_registered_as_frontend_module_once(berlin):
    hass = berlin
    urls = _fake_frontend(hass)
    set_sources(hass)
    await setup_entry(hass)
    assert urls.urls == frozenset({CARD_URL})


async def test_card_without_frontend(berlin):
    # Ohne Frontend (z. B. reine API-Installation) läuft die Integration trotzdem
    hass = berlin
    set_sources(hass)
    await setup_entry(hass)
    assert DATA_EXTRA_MODULE_URL not in hass.data


LEGACY = "/hacsfiles/hass-solar-fusion-card/solar-fusion-card.js?hacstag=123"


async def _lovelace_with_resources(hass, hass_storage, urls):
    hass_storage["lovelace_resources"] = {
        "version": 1, "minor_version": 1, "key": "lovelace_resources",
        "data": {"items": [
            {"id": str(i), "type": "module", "url": url} for i, url in enumerate(urls)
        ]},
    }
    assert await async_setup_component(hass, "lovelace", {})


async def test_legacy_hacs_resource_raises_repair_issue(berlin, hass_storage, caplog):
    hass = berlin
    await _lovelace_with_resources(hass, hass_storage, ["/local/other-card.js", LEGACY])
    set_sources(hass)
    await setup_entry(hass)
    issue = ir.async_get(hass).async_get_issue("solar_fusion", "legacy_card_resource")
    assert issue is not None
    assert issue.translation_placeholders == {"url": LEGACY}
    assert "now ships with the integration" in caplog.text


async def test_repair_issue_disappears_with_the_resource(berlin, hass_storage):
    hass = berlin
    await _lovelace_with_resources(hass, hass_storage, [LEGACY])
    set_sources(hass)
    await setup_entry(hass)
    resources = hass.data["lovelace"].resources
    await resources.async_delete_item("0")
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue("solar_fusion", "legacy_card_resource") is None


async def test_no_repair_issue_without_legacy_resource(berlin, hass_storage):
    hass = berlin
    # Eigene URL der Integration und fremde Karten zählen nicht
    await _lovelace_with_resources(hass, hass_storage, [CARD_URL, "/local/other-card.js"])
    set_sources(hass)
    await setup_entry(hass)
    assert ir.async_get(hass).async_get_issue("solar_fusion", "legacy_card_resource") is None


def _card_reads(pattern: str) -> set[str]:
    return set(re.findall(pattern, CARD_JS))


async def test_sensor_attributes_cover_what_the_card_reads(berlin, hass_storage):
    """Vertrag Karte ↔ Sensor: alles, was die Karte liest, wird geliefert."""
    hass = berlin
    hass_storage[STORE_KEY] = {"version": 2, "minor_version": 1, "key": STORE_KEY, "data": {
        "history": [{"date": "2026-10-03", "source": "solcast",
                     "forecast_kwh": 28.0, "actual_kwh": 25.0}],
        "calibration_state": {}, "morning_snapshots": {},
    }}
    set_sources(hass)
    await setup_entry(hass)
    today = state(hass, "forecast_today")
    attrs = today.attributes

    top = _card_reads(r"\battrs\.(\w+)")
    assert {"sources", "history", "fused_tomorrow_kwh"} <= top  # Regex greift
    assert top <= set(attrs)

    per_source = _card_reads(r"\bs\.(\w+)")
    assert {"name", "today_kwh", "quality_label"} <= per_source
    assert attrs["sources"]
    for sid, source in attrs["sources"].items():
        assert per_source <= set(source), (sid, per_source - set(source))

    per_record = _card_reads(r"\br\.(\w+)")
    assert per_record == {"date", "forecast_kwh", "actual_kwh"}
    assert attrs["history"]
    for record in attrs["history"]:
        assert per_record <= set(record)

    # Weitere Entitäten leitet die Karte aus der Entity-ID von forecast_today ab
    prefix = today.entity_id.removesuffix("_forecast_today")
    suffixes = _card_reads(r"\$\{this\._prefix\}(_\w+)")
    assert suffixes == {"_forecast_tomorrow", "_diagnostics_pv_daily_production"}
    for suffix in suffixes:
        assert hass.states.get(prefix + suffix) is not None, prefix + suffix
