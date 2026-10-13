"""Home Assistant integration for lights-bridge.

configuration.yaml:

    lights_bridge:
      url: http://raspberrypi.local:8765

Every light of the bridge becomes a light entity (on/off, brightness, colour, patterns as
effects) and every learned remote button becomes a button entity.
"""
from datetime import timedelta
import logging

import voluptuous as vol

from homeassistant.const import CONF_URL
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

DOMAIN = "lights_bridge"
_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({vol.Required(CONF_URL): cv.url})}, extra=vol.ALLOW_EXTRA)


class Bridge:
    """Thin HTTP client for the bridge's API."""

    def __init__(self, hass, url):
        self.url = url.rstrip("/")
        self.http = async_get_clientsession(hass)

    async def get(self, path):
        async with self.http.get(f"{self.url}{path}", timeout=10) as r:
            r.raise_for_status()
            return await r.json()

    async def post(self, path, body=None):
        # a light command is verified by the bridge (with BLE retries), so allow it time
        async with self.http.post(f"{self.url}{path}", json=body or {}, timeout=60) as r:
            data = await r.json()
            if r.status >= 400:
                raise RuntimeError(data.get("error") or f"bridge answered {r.status}")
            return data


async def async_setup(hass, config):
    if DOMAIN not in config:
        return True
    bridge = Bridge(hass, config[DOMAIN][CONF_URL])

    async def refresh():
        try:
            lights = await bridge.get("/api/lights")
            remotes = await bridge.get("/api/remotes")
        except Exception as err:
            raise UpdateFailed(f"lights-bridge unreachable: {err}") from err
        return {"lights": {l["id"]: l for l in lights}, "remotes": remotes.get("remotes", [])}

    coordinator = DataUpdateCoordinator(hass, _LOGGER, name=DOMAIN, update_method=refresh,
                                        update_interval=timedelta(seconds=10))
    await coordinator.async_refresh()
    hass.data[DOMAIN] = {"bridge": bridge, "coordinator": coordinator}
    for platform in ("light", "button"):
        hass.async_create_task(async_load_platform(hass, platform, DOMAIN, {}, config))
    return True
