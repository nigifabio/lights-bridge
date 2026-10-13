"""Lights of the bridge: on/off, brightness, colour, and the bridge's patterns as effects."""
from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_EFFECT, ATTR_RGB_COLOR, ColorMode, LightEntity, LightEntityFeature,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN

NO_EFFECT = "None"


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    data = hass.data[DOMAIN]
    coordinator, known = data["coordinator"], set()

    @callback
    def add_new():
        new = [lid for lid in (coordinator.data or {}).get("lights", {}) if lid not in known]
        known.update(new)
        if new:
            async_add_entities(BridgeLight(coordinator, data["bridge"], lid) for lid in new)

    add_new()
    coordinator.async_add_listener(add_new)  # lights added later in the bridge's page


class BridgeLight(CoordinatorEntity, LightEntity):
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, coordinator, bridge, light_id):
        super().__init__(coordinator)
        self._bridge = bridge
        self._id = light_id
        self._attr_unique_id = f"lights_bridge_{light_id}"

    @property
    def _light(self):
        return (self.coordinator.data or {}).get("lights", {}).get(self._id)

    @property
    def available(self):
        return super().available and self._light is not None

    @property
    def name(self):
        return self._light["name"] if self._light else self._id

    @property
    def is_on(self):
        return self._light["state"]["on"]

    @property
    def brightness(self):
        return round(self._light["state"]["brightness"] * 255 / 100)

    @property
    def rgb_color(self):
        h = self._light["state"]["color"].lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    @property
    def effect_list(self):
        return [NO_EFFECT] + [p["name"] for p in self._light["patterns"]]

    @property
    def effect(self):
        pid = self._light["state"]["pattern"]
        return next((p["name"] for p in self._light["patterns"] if p["id"] == pid), NO_EFFECT)

    @property
    def extra_state_attributes(self):
        l = self._light
        return {"verified": l.get("confirmed"), "bridge_error": l.get("error"), "kind": l.get("kind")}

    async def _send(self, patch):
        try:
            light = await self._bridge.post(f"/api/lights/{self._id}", patch)
        except Exception as err:
            raise HomeAssistantError(f"{self.name}: {err}") from err
        self.coordinator.data["lights"][self._id] = light
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        patch = {"on": True}
        if ATTR_BRIGHTNESS in kwargs:
            patch["brightness"] = max(1, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255))
        if ATTR_RGB_COLOR in kwargs:
            patch["color"] = "#%02x%02x%02x" % tuple(kwargs[ATTR_RGB_COLOR])
        if ATTR_EFFECT in kwargs:
            name = kwargs[ATTR_EFFECT]
            patch["pattern"] = next((p["id"] for p in self._light["patterns"] if p["name"] == name), None)
        await self._send(patch)

    async def async_turn_off(self, **kwargs):
        await self._send({"on": False})
