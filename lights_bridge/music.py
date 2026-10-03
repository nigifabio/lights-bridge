"""Follow-music pattern: a beat clock at the tempo of whatever Sonos is playing.

There is no audio capture. The tempo comes from ReccoBeats when the Sonos track URI carries a
Spotify track id, else DEFAULT_BPM. The clock is anchored on the Sonos play position (1 s
resolution), so the pulse matches the tempo but is not locked to the exact beat.
"""
import asyncio
import logging
import math
import re
import time
import zlib

import aiohttp

from .patterns import hsv, scale

try:
    import soco
    soco.config.REQUEST_TIMEOUT = 3.0  # the default 20 s stalls the poll when a speaker sleeps
except ImportError:  # optional dependency
    soco = None

log = logging.getLogger("lights.music")
SPOTIFY_ID = re.compile(r"spotify(?::|%3a)track(?::|%3a)([A-Za-z0-9]{22})", re.I)


class Music:
    DEFAULT_BPM = 120.0
    IDLE_AFTER = 12  # seconds without a playing speaker before the lights go idle

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.available = soco is not None
        self.devices = {}  # uid -> SoCo; never shrinks: a partial discovery must not drop a speaker
        self.last_discover = float("-inf")
        self.last_heard = float("-inf")
        self.playing = False
        self.title = self.artist = self.room = ""
        self.key = None
        self.bpm = self.DEFAULT_BPM
        self.bpm_known = False
        self.hue = 0.0
        self.t0 = clock()
        self.tempos = {}  # track key -> bpm or None

    def info(self):
        return {"playing": self.playing, "title": self.title, "artist": self.artist, "room": self.room,
                "bpm": round(self.bpm) if self.bpm_known else None}

    def _poll_sonos(self):
        if not self.devices or self.clock() - self.last_discover > 60:
            self.last_discover = self.clock()
            try:
                self.devices.update({d.uid: d for d in soco.discover(timeout=4) or []})
            except Exception as e:
                log.warning("sonos discovery failed: %r", e)
        for d in list(self.devices.values()):
            try:
                if d.get_current_transport_info().get("current_transport_state") != "PLAYING":
                    continue
                t = d.get_current_track_info()
                if not t.get("title"):
                    continue
                h, m, sec = (int(x) for x in (t.get("position") or "0:00:00").split(":"))
                # Spotify Connect keeps one URI for a whole session: the title makes the key per track
                return {"title": t["title"], "artist": t.get("artist") or "", "room": d.player_name,
                        "key": f'{t.get("uri") or ""}|{t["title"]}', "pos": h * 3600 + m * 60 + sec}
            except Exception:
                continue
        return None

    async def _tempo(self, key):
        m = SPOTIFY_ID.search(key)
        if not m:
            return None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as http:
                async with http.get("https://api.reccobeats.com/v1/audio-features", params={"ids": m.group(1)}) as r:
                    data = await r.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            tempo = float((items[0] if isinstance(items, list) else items).get("tempo") or 0)
            return tempo if 40 <= tempo <= 220 else None
        except Exception as e:
            log.info("no tempo for %s: %r", m.group(1), e)
            return None

    async def update(self, now):
        """Fold one poll result (dict or None) into the beat clock."""
        stamp = self.clock()
        if not now:
            # one missed poll must not dim the lights
            if stamp - self.last_heard > self.IDLE_AFTER:
                self.playing = False
            return
        self.playing = True
        self.last_heard = stamp
        self.title, self.artist, self.room = now["title"], now["artist"], now["room"]
        if now["key"] != self.key:
            self.key = now["key"]
            self.hue = (zlib.crc32(self.key.encode()) % 360) / 360
            if self.key not in self.tempos:
                self.tempos[self.key] = await self._tempo(self.key)
            bpm = self.tempos[self.key]
            self.bpm, self.bpm_known = bpm or self.DEFAULT_BPM, bool(bpm)
            self.t0 = stamp - now["pos"]
            log.info("now playing: %s - %s, %s BPM", self.artist, self.title, bpm or "unknown")
        elif abs((stamp - self.t0) - now["pos"]) > 2.5:  # seek or pause/resume
            self.t0 = stamp - now["pos"]

    async def run(self, wanted):
        """Poll Sonos every 2 s while wanted() says a light is following the music."""
        loop = asyncio.get_running_loop()
        while True:
            if not self.available or not wanted():
                await asyncio.sleep(1)
                continue
            await self.update(await loop.run_in_executor(None, self._poll_sonos))
            await asyncio.sleep(2)

    def frame(self, n, pulse=True, every=1):
        """Colours for n segments now.

        pulse=False gives flat colours that change only every `every` beats: one write per change,
        for controllers that drop the link when written to often.
        """
        if not self.playing:  # steady glow: very low levels make LED strips cut out
            return [scale(hsv(self.hue, 0.7), 0.4)] * n
        beats = (self.clock() - self.t0) * self.bpm / 60
        level = 0.4 + 0.6 * math.exp(-4.5 * (beats - int(beats))) if pulse else 1.0
        beat = int(beats // every)
        hue = self.hue + 0.09 * (beat // 8)  # drift every two bars
        a, b = scale(hsv(hue), level), scale(hsv(hue + 0.38), level)
        return [a if (i + beat) % 2 == 0 else b for i in range(n)]
