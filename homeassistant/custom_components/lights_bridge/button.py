"""One button entity per learned button of the bridge's remotes (infrared / radio via a hub)."""
from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    data = hass.data[DOMAIN]
    coordinator, known = data["coordinator"], set()

    @callback
    def add_new():
        new = []
        for remote in (coordinator.data or {}).get("remotes", []):
            for b in remote["buttons"]:
                key = (remote["id"], b["id"])
                if b["learned"] and key not in known:
                    known.add(key)
                    new.append(RemoteButton(coordinator, data["bridge"], remote, b))
        if new:
            async_add_entities(new)

    add_new()
    coordinator.async_add_listener(add_new)  # buttons taught later appear on their own


class RemoteButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator, bridge, remote, button):
        super().__init__(coordinator)
        self._bridge = bridge
        self._remote, self._button = remote["id"], button["id"]
        self._attr_unique_id = f"lights_bridge_{remote['id']}_{button['id']}"
        self._attr_name = f"{remote['name']} {button['name']}"

    @property
    def available(self):
        remotes = (self.coordinator.data or {}).get("remotes", [])
        return super().available and any(
            r["id"] == self._remote and any(b["id"] == self._button and b["learned"] for b in r["buttons"])
            for r in remotes)

    async def async_press(self):
        try:
            await self._bridge.post(f"/api/remotes/{self._remote}/buttons/{self._button}/press")
        except Exception as err:
            raise HomeAssistantError(f"{self.name}: {err}") from err
