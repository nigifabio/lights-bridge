"""Base class for a BLE light: connection handling, verified commands, pattern runner."""
import asyncio
import logging
import time

from .patterns import SOFT_PATTERNS, hex_to_rgb

log = logging.getLogger("lights")


async def bleak_connect(address):
    """Default connector: find the device and return a connected BleakClient."""
    from bleak import BleakClient, BleakScanner
    dev = await BleakScanner.find_device_by_address(address, timeout=10)
    if not dev:
        raise RuntimeError("not in range (is it plugged in?)")
    client = BleakClient(dev, timeout=20)
    await client.connect()
    return client


class Light:
    kind = "generic"
    label = "Generic"
    segments = 1
    frame_interval = 0.25   # seconds between software-pattern frames
    beat_only = False       # follow-music: flat colour changes instead of a smooth pulse
    beat_every = 1          # ...and only every N beats
    hw_patterns = {}        # effects built into the controller: code -> label
    write_uuid = None
    connect_attempts = 4
    command_attempts = 3

    def __init__(self, cfg, saved=None, connector=None, music=None):
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.address = cfg["address"]
        self.connector = connector or bleak_connect
        self.music = music
        self.state = {"on": False, "brightness": 100, "color": "#ffffff", "pattern": None, "speed": 50}
        self.state.update(saved or {})
        self.client = None
        self.lock = asyncio.Lock()
        self.pattern_task = None
        self.last_used = 0.0
        self.error = None
        self.confirmed = None  # how the last command was verified, see _verify()
        self._last_frame = None

    # ---- connection -------------------------------------------------
    async def _ensure(self):
        if self.client and self.client.is_connected:
            return
        last = None
        for attempt in range(self.connect_attempts):
            try:
                self.client = await self.connector(self.address)
                await self._on_connect()
                log.info("%s connected (attempt %d)", self.id, attempt + 1)
                return
            except Exception as e:  # BLE is flaky: retry
                last = e
                log.warning("%s connect failed: %r", self.id, e)
                await self.disconnect()
                await asyncio.sleep(self.retry_delay)
        raise RuntimeError(f"cannot reach {self.name}: {last}")

    retry_delay = 1.0

    async def _on_connect(self):
        pass

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None

    async def _write(self, data):
        await self.client.write_gatt_char(self.write_uuid, data, response=False)

    # ---- public API -------------------------------------------------
    def patterns(self):
        out = []
        if self.music and self.music.available:
            out.append({"id": "music", "name": "♪ Follow music"})
        out += [{"id": k, "name": v[0]} for k, v in SOFT_PATTERNS.items()]
        out += [{"id": f"hw:{k}", "name": v} for k, v in self.hw_patterns.items()]
        return out

    def info(self):
        following = self.state["pattern"] == "music" and self.music
        return {"id": self.id, "name": self.name, "kind": self.kind, "address": self.address,
                "state": self.state, "patterns": self.patterns(), "error": self.error,
                "confirmed": self.confirmed, "music": self.music.info() if following else None,
                "connected": bool(self.client and self.client.is_connected)}

    async def apply(self, patch):
        """Send a command and make sure it arrived: verify, and on failure reconnect and resend."""
        async with self.lock:
            last = None
            try:
                for attempt in range(self.command_attempts):
                    try:
                        await self._ensure()
                        await self._apply(patch)
                        self.confirmed = await self._verify()
                        self.error = None
                        return
                    except (ValueError, KeyError, TypeError):
                        raise
                    except Exception as e:
                        last = e
                        log.warning("%s command not confirmed (attempt %d): %r", self.id, attempt + 1, e)
                        await self.disconnect()
                self._stop_pattern()
                self.state["pattern"] = None
                self.confirmed = None
                self.error = f"command not confirmed: {last}"
                raise RuntimeError(self.error)
            finally:
                self.last_used = time.monotonic()

    async def _verify(self):
        """Return how the command was verified, or raise if the light did not take it."""
        return None

    def _valid_pattern(self, pid):
        if pid == "music":
            return bool(self.music and self.music.available)
        if pid in SOFT_PATTERNS:
            return True
        return pid.startswith("hw:") and pid[3:].isdigit() and int(pid[3:]) in self.hw_patterns

    async def _apply(self, p):
        s = self.state
        # validate everything before touching the light
        if "color" in p:
            hex_to_rgb(p["color"])
        if p.get("pattern") and not self._valid_pattern(p["pattern"]):
            raise ValueError(f"unknown pattern {p['pattern']}")
        if "speed" in p:
            s["speed"] = max(1, min(100, int(p["speed"])))
            if s["pattern"] and s["pattern"].startswith("hw:"):
                await self._hw_pattern(s["pattern"])
        if p.get("on") is False:
            self._stop_pattern()
            s["pattern"] = None
            await self._power(False)
            s["on"] = False
            return
        if p.get("on") is True or (not s["on"] and any(k in p for k in ("color", "pattern", "brightness"))):
            await self._power(True)
            s["on"] = True
        if "brightness" in p:
            s["brightness"] = max(1, min(100, int(p["brightness"])))
            await self._brightness(s["brightness"])
        if "color" in p:
            self._stop_pattern()
            s["pattern"] = None
            s["color"] = p["color"]
            await self._color(hex_to_rgb(s["color"]))
        if "pattern" in p:
            self._stop_pattern()
            pid = p["pattern"]
            s["pattern"] = pid or None
            if not pid:
                await self._color(hex_to_rgb(s["color"]))
            elif pid.startswith("hw:"):
                await self._hw_pattern(pid)
            else:
                self.pattern_task = asyncio.create_task(self._run_soft(pid))

    def _stop_pattern(self):
        if self.pattern_task:
            self.pattern_task.cancel()
            self.pattern_task = None
        self._last_frame = None

    def _soft_frame(self, pid, t):
        if pid == "music":
            return self.music.frame(self.segments, pulse=not self.beat_only, every=self.beat_every)
        fn = SOFT_PATTERNS[pid][1]
        base = hex_to_rgb(self.state["color"])
        return [fn(t, i, self.segments, base) for i in range(self.segments)]

    async def _run_soft(self, pid):
        interval = 0.11 if pid == "music" else self.frame_interval
        t = 0.0
        fails = 0
        while True:
            try:
                async with self.lock:
                    await self._ensure()
                    frame = self._soft_frame(pid, t)
                    if frame != self._last_frame:  # identical frames are not resent
                        await self._frame(frame)
                        self._last_frame = frame
                    self.last_used = time.monotonic()
                fails = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fails += 1
                log.warning("%s pattern frame failed: %r", self.id, e)
                await self.disconnect()
                if fails > 5:
                    self.error = str(e)
                    self.state["pattern"] = None
                    return
            # one full cycle takes 60 s at speed 1 and ~3 s at speed 100
            t += interval / (60 - 57 * (self.state["speed"] - 1) / 99)
            await asyncio.sleep(interval)

    async def _frame(self, colors):
        await self._color(colors[0])

    async def _hw_pattern(self, pid):
        raise ValueError("no built-in patterns")

    async def _power(self, on):
        raise NotImplementedError

    async def _brightness(self, pct):
        raise NotImplementedError

    async def _color(self, rgb):
        raise NotImplementedError
