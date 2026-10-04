"""HTTP API + web page.

GET    /api/lights            list of lights with state and available patterns
POST   /api/lights            add a light: {"name", "type", "address", "segments"?}
POST   /api/lights/{id}       patch: {"on", "brightness" 1-100, "color" "#rrggbb",
                              "pattern" id|null, "speed" 1-100, "name"}
POST   /api/lights/{id}/sync  reconnect and re-read the light's state
DELETE /api/lights/{id}       remove a light
POST   /api/scan              scan for BLE devices and say which ones are known light types

Remotes (infrared / 433 MHz buttons replayed through a Broadlink hub), see remotes.py:
GET    /api/remotes                               hubs, templates, remotes and their buttons
POST   /api/hubs/discover                         look for hubs on the LAN
POST   /api/hubs                                  add a hub: {"host", "name"?}
DELETE /api/hubs/{id}
POST   /api/remotes                               add a remote: {"name", "hub", "template"?}
DELETE /api/remotes/{id}
POST   /api/remotes/{id}/buttons                  add a button: {"name"}
DELETE /api/remotes/{id}/buttons/{button}
POST   /api/remotes/{id}/buttons/{button}/learn   {"kind": "ir"|"rf"}, waits for the button press
POST   /api/remotes/{id}/buttons/{button}/press   {"repeat"?}
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from aiohttp import web

from .drivers import KINDS, detect
from .music import Music
from .remotes import Remotes
from .spotify import Spotify

log = logging.getLogger("lights")
STATIC = Path(__file__).parent / "static"
MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$|^[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}$", re.I)
DEFAULTS = {"host": "0.0.0.0", "port": 8765, "idle_disconnect": 120, "music": True, "lights": []}


async def bleak_scan(seconds=8):
    """Default scanner: [{address, name, rssi}] for everything advertising nearby."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    return [{"address": d.address, "name": d.name or adv.local_name or "", "rssi": adv.rssi}
            for d, adv in found.values()]


class Bridge:
    def __init__(self, data_dir, connector=None, scanner=None, music=None):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.dir / "config.json"
        self.state_file = self.dir / "state.json"
        self.config = {**DEFAULTS, **(json.loads(self.config_file.read_text()) if self.config_file.exists() else {})}
        self.connector = connector
        self.scanner = scanner or bleak_scan
        self.music = music if music is not None else Music()
        if not self.config.get("music"):
            self.music.available = False
        if self.config.get("spotify"):
            self.music.spotify = Spotify(self.config["spotify"])
        saved = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.lights = {}
        for cfg in self.config["lights"]:
            self.lights[cfg["id"]] = self._make(cfg, saved.get(cfg["id"]))
        for light in self.lights.values():
            light.state["pattern"] = None  # running patterns do not survive a restart

    def _make(self, cfg, saved=None):
        return KINDS[cfg["type"]](cfg, saved, self.connector, self.music)

    def save_state(self):
        self.state_file.write_text(json.dumps({l.id: l.state for l in self.lights.values()}))

    def save_config(self):
        self.config_file.write_text(json.dumps(self.config, indent=2) + "\n")

    def add(self, name, kind, address, segments=None):
        name = str(name or "").strip()
        address = str(address or "").strip().upper()
        if not name:
            raise ValueError("name is required")
        if kind not in KINDS:
            raise ValueError(f"unknown type {kind!r}, expected one of {', '.join(KINDS)}")
        if not MAC.match(address):
            raise ValueError("address must be a Bluetooth address like AA:BB:CC:DD:EE:FF")
        if any(l.address.upper() == address for l in self.lights.values()):
            raise ValueError("this light is already added")
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "light"
        lid, n = base, 2
        while lid in self.lights:
            lid, n = f"{base}-{n}", n + 1
        cfg = {"id": lid, "name": name, "type": kind, "address": address}
        if segments:
            cfg["segments"] = max(1, min(15, int(segments)))
        self.config["lights"].append(cfg)
        self.lights[lid] = self._make(cfg)
        self.save_config()
        return self.lights[lid]

    async def remove(self, lid):
        light = self.lights.pop(lid)
        light._stop_pattern()
        await light.disconnect()
        self.config["lights"] = [c for c in self.config["lights"] if c["id"] != lid]
        self.save_config()
        self.save_state()

    def rename(self, lid, name):
        name = str(name or "").strip()
        if not name:
            raise ValueError("name is required")
        self.lights[lid].name = name
        next(c for c in self.config["lights"] if c["id"] == lid)["name"] = name
        self.save_config()

    async def scan(self):
        known = {l.address.upper() for l in self.lights.values()}
        out = []
        for d in await self.scanner():
            kind = detect(d["name"])
            out.append({**d, "type": kind, "label": KINDS[kind].label if kind else None,
                        "added": d["address"].upper() in known})
        # known light types first, then by signal strength
        return sorted(out, key=lambda d: (d["type"] is None, -d["rssi"]))

    async def janitor(self):
        """Drop idle connections so the vendor phone apps can still reach the lights."""
        while True:
            await asyncio.sleep(15)
            for l in list(self.lights.values()):
                if l.client and not l.pattern_task and time.monotonic() - l.last_used > self.config["idle_disconnect"]:
                    async with l.lock:
                        await l.disconnect()
                    log.info("%s idle, disconnected", l.id)


def _light(request):
    light = request.app["bridge"].lights.get(request.match_info["id"])
    if not light:
        raise web.HTTPNotFound()
    return light


async def list_lights(request):
    bridge = request.app["bridge"]
    return web.json_response([l.info() for l in bridge.lights.values()])


async def add_light(request):
    bridge = request.app["bridge"]
    try:
        body = await request.json()
        light = bridge.add(body.get("name"), body.get("type"), body.get("address"), body.get("segments"))
    except (ValueError, TypeError, AttributeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(light.info(), status=201)


async def set_light(request):
    bridge, light = request.app["bridge"], _light(request)
    try:
        patch = await request.json()
        if "name" in patch:
            bridge.rename(light.id, patch.pop("name"))
        if patch:
            await light.apply(patch)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return web.json_response({**light.info(), "error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({**light.info(), "error": str(e)}, status=502)
    bridge.save_state()
    return web.json_response(light.info())


async def sync_light(request):
    light = _light(request)
    try:
        async with light.lock:
            await light.disconnect()
        await light.apply({})
    except Exception as e:
        return web.json_response({**light.info(), "error": str(e)}, status=502)
    return web.json_response(light.info())


async def remove_light(request):
    await request.app["bridge"].remove(_light(request).id)
    return web.json_response({"ok": True})


async def scan(request):
    try:
        return web.json_response(await request.app["bridge"].scan())
    except Exception as e:
        return web.json_response({"error": f"scan failed: {e}"}, status=502)


def remote_api(fn):
    """Run a Remotes call and turn its errors into JSON: 404 unknown id, 400 bad input, 502 hub trouble."""
    async def handler(request):
        remotes = request.app["remotes"]
        try:
            body = await request.json() if request.can_read_body else {}
            out = fn(remotes, request.match_info, body)
            if asyncio.iscoroutine(out):
                out = await out
        except KeyError as e:
            return web.json_response({"error": str(e.args[0])}, status=404)
        except (ValueError, TypeError, AttributeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e) or type(e).__name__}, status=502)
        return web.json_response(out if isinstance(out, list) else remotes.info())
    return handler


REMOTE_ROUTES = [
    ("GET", "/api/remotes", lambda r, m, b: None),
    ("POST", "/api/hubs/discover", lambda r, m, b: r.discover()),
    ("POST", "/api/hubs", lambda r, m, b: r.add_hub(b.get("host"), b.get("name"))),
    ("DELETE", "/api/hubs/{id}", lambda r, m, b: r.remove_hub(m["id"])),
    ("POST", "/api/remotes", lambda r, m, b: r.add_remote(b.get("name"), b.get("hub"), b.get("template") or "custom")),
    ("DELETE", "/api/remotes/{id}", lambda r, m, b: r.remove_remote(m["id"])),
    ("POST", "/api/remotes/{id}/buttons", lambda r, m, b: r.add_button(m["id"], b.get("name"))),
    ("DELETE", "/api/remotes/{id}/buttons/{button}", lambda r, m, b: r.remove_button(m["id"], m["button"])),
    ("POST", "/api/remotes/{id}/buttons/{button}/learn", lambda r, m, b: r.learn(m["id"], m["button"], b.get("kind") or "ir")),
    ("POST", "/api/remotes/{id}/buttons/{button}/press", lambda r, m, b: r.press(m["id"], m["button"], b.get("repeat") or 1)),
]


async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


def create_app(data_dir=None, connector=None, scanner=None, music=None, background=True, hub_backend=None):
    app = web.Application()
    bridge = app["bridge"] = Bridge(data_dir or os.environ.get("LIGHTS_DATA", "data"), connector, scanner, music)
    app["remotes"] = Remotes(bridge.dir, hub_backend)

    async def start(app):
        if background:
            wanted = lambda: any(l.state["pattern"] == "music" for l in bridge.lights.values())
            app["tasks"] = [asyncio.create_task(bridge.janitor()), asyncio.create_task(bridge.music.run(wanted))]

    async def stop(app):
        for t in app.get("tasks", []):
            t.cancel()
        for l in bridge.lights.values():
            l._stop_pattern()
            await l.disconnect()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    app.add_routes([
        web.get("/", index),
        web.get("/api/lights", list_lights),
        web.post("/api/lights", add_light),
        web.post("/api/lights/{id}", set_light),
        web.post("/api/lights/{id}/sync", sync_light),
        web.delete("/api/lights/{id}", remove_light),
        web.post("/api/scan", scan),
    ])
    for method, path, fn in REMOTE_ROUTES:
        app.router.add_route(method, path, remote_api(fn))
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    app = create_app()
    cfg = app["bridge"].config
    web.run_app(app, host=os.environ.get("LIGHTS_HOST", cfg["host"]),
                port=int(os.environ.get("LIGHTS_PORT", cfg["port"])), access_log=None)
