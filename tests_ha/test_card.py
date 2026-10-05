"""Solar Fusion Card: Auslieferung mit der Integration."""
import json

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager

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
