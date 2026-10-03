import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.tuya_recordings import frontend


def test_panel_reload_does_not_register_static_route_twice(monkeypatch):
    register = AsyncMock()
    panel = AsyncMock()
    hass = SimpleNamespace(data={}, http=SimpleNamespace(async_register_static_paths=register))
    monkeypatch.setattr(frontend.panel_custom, "async_register_panel", panel)

    async def lifecycle():
        await frontend.async_register_frontend(hass)
        hass.data[frontend.DOMAIN].pop("_panel_registered")
        await frontend.async_register_frontend(hass)

    asyncio.run(lifecycle())
    assert register.await_count == 1
    assert panel.await_count == 2
