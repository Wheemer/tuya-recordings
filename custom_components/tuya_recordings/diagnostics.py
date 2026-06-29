from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry):
    data = dict(entry.data)
    for key in ("client_secret", "cookies", "login_result"):
        if key in data:
            data[key] = "redacted"
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    return {
        "entry": data,
        "runtime": {
            "configured": entry.entry_id in hass.data.get(DOMAIN, {}),
            "client_loaded": bool(runtime.get("client")),
            "client": runtime["client"].diagnostics() if runtime.get("client") else None,
        },
    }
