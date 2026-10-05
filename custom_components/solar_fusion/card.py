"""Solar Fusion Card: mit der Integration ausliefern.

Die Karte (``frontend/``) wird einmal je HA-Start unter ``/solar_fusion/``
bereitgestellt und als Frontend-Modul eingetragen; eine Dashboard-Ressource
ist nicht nötig. Ist die frühere HACS-Karte noch als Ressource eingetragen,
weist ein Reparaturhinweis darauf hin.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.start import async_at_started
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_DIR = Path(__file__).parent / "frontend"
CARD_FILE = "solar-fusion-card.js"
URL_BASE = f"/{DOMAIN}"
LEGACY_ISSUE = "legacy_card_resource"


async def async_setup_card(hass: HomeAssistant) -> None:
    """Karte bereitstellen und als Frontend-Modul eintragen."""
    version = (await async_get_integration(hass, DOMAIN)).version
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(CARD_DIR), cache_headers=True)]
    )
    url = f"{URL_BASE}/{CARD_FILE}?v={version}"
    if "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, url)
    else:
        _LOGGER.debug("Frontend not loaded, card not registered")

    @callback
    def _check(_hass: HomeAssistant) -> None:
        hass.async_create_task(async_check_legacy_resource(hass))

    async_at_started(hass, _check)


async def async_check_legacy_resource(hass: HomeAssistant) -> None:
    """Reparaturhinweis, solange die HACS-Karte als Dashboard-Ressource eingetragen ist."""
    legacy = [url for url in await _async_resource_urls(hass) if _is_legacy(url)]
    if legacy:
        _LOGGER.warning(
            "The Solar Fusion card now ships with the integration. Remove the "
            "dashboard resource %s and uninstall the HACS card hass-solar-fusion-card",
            ", ".join(legacy),
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            LEGACY_ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=LEGACY_ISSUE,
            translation_placeholders={"url": ", ".join(legacy)},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, LEGACY_ISSUE)


def _is_legacy(url: str) -> bool:
    path = url.split("?", 1)[0]
    return path.endswith(f"/{CARD_FILE}") and not path.startswith(f"{URL_BASE}/")


async def _async_resource_urls(hass: HomeAssistant) -> list[str]:
    """URLs der Dashboard-Ressourcen (Speicher- und YAML-Modus)."""
    lovelace = hass.data.get("lovelace")
    # Ab HA 2025.2 ein Dataclass, davor ein dict
    resources = (
        lovelace.get("resources") if isinstance(lovelace, dict)
        else getattr(lovelace, "resources", None)
    )
    if resources is None:
        return []
    try:
        await resources.async_get_info()  # lädt die Ressourcen im Speichermodus
        return [str(item.get("url", "")) for item in resources.async_items() or []]
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not read dashboard resources: %s", err)
        return []
