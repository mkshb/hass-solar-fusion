"""Solar Fusion Card: Auslieferung mit der Integration."""
import json

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from common import ROOT_DIR, set_sources, setup_entry

MANIFEST = json.loads((ROOT_DIR / "custom_components/solar_fusion/manifest.json").read_text())
CARD_URL = f"/solar_fusion/solar-fusion-card.js?v={MANIFEST['version']}"


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
