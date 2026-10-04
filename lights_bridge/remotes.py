"""Remotes: replay infrared and 433 MHz radio buttons through a Broadlink hub (RM4 Pro, RM4 mini...).

A hub learns each button of a physical remote once; the page then shows those buttons. This is
one-way: the bridge cannot know what state the controlled device is in.

Everything is stored in <data>/remotes.json:
  {"hubs": [{"id", "name", "host", "mac", "model"}],
   "remotes": [{"id", "name", "hub", "buttons": [{"id", "name", "code": base64 or null}]}]}
"""
import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("lights.remotes")

LEVELS = [f"{p}%" for p in range(10, 101, 10)]
TEMPLATES = {
    "custom": ("Custom (start empty)", []),
    "dimmer": ("LED / neon dimmer", ["On", "Off", "Light", "Brighter", "Dimmer", "Mode +", "Mode −",
                                     "Speed +", "Speed −", *LEVELS,
                                     "Timer 5", "Timer 10", "Timer 30", "Timer 60"]),
    "tv": ("TV", ["Power", "Source", "Mute", "Vol +", "Vol −", "Ch +", "Ch −", "Up", "Down", "Left",
                  "Right", "OK", "Back", "Home", "Menu", "Play/Pause"]),
    "audio": ("Speaker / amplifier", ["Power", "Mute", "Vol +", "Vol −", "Source", "Play/Pause", "Next", "Previous"]),
    "fan": ("Fan / air conditioner", ["Power", "Speed +", "Speed −", "Swing", "Mode", "Timer", "Temp +", "Temp −"]),
    "rgb": ("RGB light remote", ["On", "Off", "Brighter", "Dimmer", "Red", "Green", "Blue", "White",
                                 "Orange", "Yellow", "Cyan", "Purple", "Flash", "Strobe", "Fade", "Smooth"]),
}


def slug(name, taken, fallback="item"):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or fallback
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


class BroadlinkBackend:
    """Blocking calls to python-broadlink; Remotes runs them in a thread."""

    def _describe(self, d):
        return {"host": d.host[0], "mac": d.mac.hex(":").upper(), "model": d.model or d.type,
                "rf": hasattr(d, "sweep_frequency")}

    def discover(self, timeout=5):
        import broadlink
        return [self._describe(d) for d in broadlink.discover(timeout=timeout) if hasattr(d, "send_data")]

    def identify(self, host):
        import broadlink
        try:
            d = broadlink.hello(host, timeout=5)
        except Exception:
            raise RuntimeError(f"no hub answered at {host}") from None
        if not hasattr(d, "send_data"):
            raise ValueError(f"{d.model or d.type} is not an IR/RF remote hub")
        return self._describe(d)

    def _device(self, hub):
        import broadlink
        try:
            d = broadlink.hello(hub["host"], timeout=4)
        except Exception:
            # the hub's address may have changed (DHCP): look for its MAC on the LAN
            found = [x for x in broadlink.discover(timeout=5) if x.mac.hex(":").upper() == hub["mac"]]
            if not found:
                raise RuntimeError(f"hub {hub['name']} is not reachable")
            d = found[0]
            hub["host"] = d.host[0]
        d.auth()
        return d

    def send(self, hub, code):
        self._device(hub).send_data(code)

    def _wait_code(self, d, timeout):
        from broadlink.exceptions import ReadError, StorageError
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(1)
            try:
                return d.check_data()
            except (ReadError, StorageError):
                continue
        raise TimeoutError("no button press seen")

    def learn_ir(self, hub, timeout=30):
        d = self._device(hub)
        d.enter_learning()
        return self._wait_code(d, timeout)

    def learn_rf(self, hub, timeout=30):
        """Two steps: hold the button while the hub finds the frequency, then press it briefly."""
        d = self._device(hub)
        if not hasattr(d, "sweep_frequency"):
            raise ValueError("this hub has no radio (433 MHz) support")
        d.sweep_frequency()
        end = time.time() + timeout
        found, freq = False, None
        while time.time() < end and not found:
            time.sleep(1)
            r = d.check_frequency()
            found, freq = r if isinstance(r, tuple) else (r, None)
        if not found:
            d.cancel_sweep_frequency()
            raise TimeoutError("no radio signal found: hold the button down while it searches")
        time.sleep(1.5)  # let the user release the button
        try:
            d.find_rf_packet(freq) if freq else d.find_rf_packet()
        except TypeError:
            d.find_rf_packet()
        return self._wait_code(d, timeout)


class Remotes:
    def __init__(self, data_dir, backend=None):
        self.file = Path(data_dir) / "remotes.json"
        self.data = {"hubs": [], "remotes": []}
        if self.file.exists():
            self.data.update(json.loads(self.file.read_text()))
        self.backend = backend or BroadlinkBackend()
        self.lock = asyncio.Lock()  # one hub operation at a time

    def save(self):
        self.file.write_text(json.dumps(self.data, indent=2) + "\n")

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # ---- views ------------------------------------------------------
    def info(self):
        return {
            "hubs": self.data["hubs"],
            "templates": [{"id": k, "name": v[0]} for k, v in TEMPLATES.items()],
            "remotes": [{"id": r["id"], "name": r["name"], "hub": r["hub"],
                         "buttons": [{"id": b["id"], "name": b["name"], "learned": bool(b.get("code"))}
                                     for b in r["buttons"]]} for r in self.data["remotes"]],
        }

    def _hub(self, hid):
        hub = next((h for h in self.data["hubs"] if h["id"] == hid), None)
        if not hub:
            raise KeyError(f"no hub {hid!r}")
        return hub

    def _remote(self, rid):
        r = next((r for r in self.data["remotes"] if r["id"] == rid), None)
        if not r:
            raise KeyError(f"no remote {rid!r}")
        return r

    def _button(self, rid, bid):
        b = next((b for b in self._remote(rid)["buttons"] if b["id"] == bid), None)
        if not b:
            raise KeyError(f"no button {bid!r}")
        return b

    # ---- hubs -------------------------------------------------------
    async def discover(self):
        known = {h["mac"] for h in self.data["hubs"]}
        return [{**d, "added": d["mac"] in known} for d in await self._run(self.backend.discover)]

    async def add_hub(self, host, name=None):
        host = str(host or "").strip()
        if not host:
            raise ValueError("the hub's IP address is required")
        d = await self._run(self.backend.identify, host)
        if any(h["mac"] == d["mac"] for h in self.data["hubs"]):
            raise ValueError("this hub is already added")
        name = str(name or "").strip() or d["model"]
        hub = {"id": slug(name, {h["id"] for h in self.data["hubs"]}, "hub"), "name": name, **d}
        self.data["hubs"].append(hub)
        self.save()
        return hub

    def remove_hub(self, hid):
        self._hub(hid)
        if any(r["hub"] == hid for r in self.data["remotes"]):
            raise ValueError("remove the remotes that use this hub first")
        self.data["hubs"] = [h for h in self.data["hubs"] if h["id"] != hid]
        self.save()

    # ---- remotes and buttons ----------------------------------------
    def add_remote(self, name, hub, template="custom"):
        name = str(name or "").strip()
        if not name:
            raise ValueError("name is required")
        self._hub(hub)
        if template not in TEMPLATES:
            raise ValueError(f"unknown template {template!r}")
        r = {"id": slug(name, {r["id"] for r in self.data["remotes"]}, "remote"), "name": name, "hub": hub, "buttons": []}
        for label in TEMPLATES[template][1]:
            r["buttons"].append({"id": slug(label.replace("+", " plus").replace("−", " minus").replace("%", " percent"),
                                            {b["id"] for b in r["buttons"]}, "button"), "name": label, "code": None})
        self.data["remotes"].append(r)
        self.save()
        return r

    def remove_remote(self, rid):
        self._remote(rid)
        self.data["remotes"] = [r for r in self.data["remotes"] if r["id"] != rid]
        self.save()

    def add_button(self, rid, name):
        name = str(name or "").strip()
        if not name:
            raise ValueError("name is required")
        r = self._remote(rid)
        b = {"id": slug(name, {b["id"] for b in r["buttons"]}, "button"), "name": name, "code": None}
        r["buttons"].append(b)
        self.save()
        return b

    def remove_button(self, rid, bid):
        self._button(rid, bid)
        r = self._remote(rid)
        r["buttons"] = [b for b in r["buttons"] if b["id"] != bid]
        self.save()

    async def learn(self, rid, bid, kind="ir"):
        if kind not in ("ir", "rf"):
            raise ValueError("kind must be ir or rf")
        b = self._button(rid, bid)
        hub = self._hub(self._remote(rid)["hub"])
        async with self.lock:
            code = await self._run(self.backend.learn_ir if kind == "ir" else self.backend.learn_rf, hub)
        b["code"] = base64.b64encode(code).decode()
        b["kind"] = kind
        self.save()

    async def press(self, rid, bid, repeat=1):
        b = self._button(rid, bid)
        if not b.get("code"):
            raise ValueError(f"button {b['name']} has not been learned yet")
        hub = self._hub(self._remote(rid)["hub"])
        code = base64.b64decode(b["code"])
        async with self.lock:
            for _ in range(max(1, min(10, int(repeat)))):
                await self._run(self.backend.send, hub, code)
