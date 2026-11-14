"""Register recording cameras independently of optional notification entities."""

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .alerts import EVENT_ENTITY_SUFFIX, safe_unique_fragment
from .const import DOMAIN, MANUFACTURER, NAME, SIGNAL_RECORDINGS_UPDATED


def camera_identifier(entry_id: str, dev_id: str) -> tuple[str, str]:
    return DOMAIN, f"{entry_id}_camera_{dev_id}"


@callback
def async_register_camera_device(hass, entry, dev_id, name, event_entity_id=None):
    """Keep legacy notification identifiers as aliases of the camera device."""
    registry = dr.async_get(hass)
    hub = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)},
        name=getattr(entry, "title", None) or NAME, manufacturer=MANUFACTURER, model=NAME,
    )
    identifiers = {camera_identifier(entry.entry_id, dev_id)}
    if event_entity_id:
        identifiers.add((DOMAIN, f"{entry.entry_id}_{safe_unique_fragment(event_entity_id)}"))
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers=identifiers,
        name=name, manufacturer=MANUFACTURER, model=NAME,
    )
    if device.via_device_id != hub.id:
        device = registry.async_update_device(device.id, via_device_id=hub.id)
    return device


@callback
def async_setup_camera_devices(hass, entry, client):
    """Synchronize local inventory changes on HA's loop; never query a camera."""
    registered = None

    @callback
    def sync(entry_id=entry.entry_id):
        nonlocal registered
        if entry_id != entry.entry_id:
            return
        inventory = client.camera_inventory
        if registered == inventory:
            return
        registry = dr.async_get(hass)
        sources = {}
        # Include registry sources whose Tuya platform has not loaded yet.
        for entity in er.async_get(hass).entities.values():
            if (entity.platform != "tuya" or not entity.entity_id.startswith("event.")
                    or not (entity.entity_id.endswith(EVENT_ENTITY_SUFFIX)
                            or entity.unique_id.endswith(EVENT_ENTITY_SUFFIX))
                    or not entity.device_id):
                continue
            device = registry.async_get(entity.device_id)
            if device is not None:
                for domain, dev_id in device.identifiers:
                    if domain == "tuya" and dev_id in inventory:
                        sources.setdefault(dev_id, entity.entity_id)
        for dev_id, name in inventory.items():
            async_register_camera_device(hass, entry, dev_id, name, sources.get(dev_id))
        registered = inventory

    sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_RECORDINGS_UPDATED, sync))
