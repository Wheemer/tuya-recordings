from __future__ import annotations

from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN, NAME

FRONTEND_URL_PATH = "tuya-recordings"
STATIC_URL_PATH = f"/{DOMAIN}_static"
PANEL_ELEMENT_NAME = "tuya-recordings-panel"


async def async_register_frontend(hass: HomeAssistant) -> None:
    if hass.data.setdefault(DOMAIN, {}).get("_panel_registered"):
        return
    frontend_path = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL_PATH, str(frontend_path), cache_headers=False)])
    await panel_custom.async_register_panel(
        hass=hass,
        frontend_url_path=FRONTEND_URL_PATH,
        webcomponent_name=PANEL_ELEMENT_NAME,
        sidebar_title=NAME,
        sidebar_icon="mdi:memory",
        module_url=f"{STATIC_URL_PATH}/panel.js",
        embed_iframe=False,
    )
    hass.data[DOMAIN]["_panel_registered"] = True
