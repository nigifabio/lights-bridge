"""Optional: ask Spotify what the authorised account is playing.

Sonos hides the track id during Spotify Connect sessions, so the tempo lookup has nothing to go
on. Spotify's "currently playing" endpoint gives the id and the play position in milliseconds.

Config (all under "spotify" in config.json):
  client_id, client_secret   the Spotify app, or
  credentials_file           a JSON file holding them (top level or under a "spotify" key)
  cache_path                 token cache written by an authorisation-code login with the
                             user-read-currently-playing scope (spotipy's .cache format:
                             access_token, refresh_token, expires_at). It is only read.
"""
import base64
import json
import logging
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("lights.spotify")
TOKEN_URL = "https://accounts.spotify.com/api/token"
NOW_URL = "https://api.spotify.com/v1/me/player/currently-playing"


def titles_match(a, b):
    """Loose comparison: Sonos and Spotify format the same title slightly differently."""
    a, b = (" ".join(x.lower().split()) for x in (a or "", b or ""))
    return bool(a and b) and (a == b or a in b or b in a)


def merge(now, sp):
    """Give a Sonos poll result the Spotify track id and precise position, when they agree."""
    if not now or not sp or not sp.get("playing") or not titles_match(now["title"], sp["title"]):
        return now
    return {**now, "key": f'spotify:track:{sp["id"]}|{now["title"]}', "pos": sp["pos"], "precise": True}


class Spotify:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = None
        self.expires = 0.0
        self.pause_until = 0.0

    def _credentials(self):
        c = self.cfg
        if c.get("credentials_file"):
            data = json.loads(Path(c["credentials_file"]).read_text())
            c = data.get("spotify", data)
        return c["client_id"], c["client_secret"]

    async def _access_token(self, http):
        if self.token and time.time() < self.expires - 60:
            return self.token
        cache = json.loads(Path(self.cfg["cache_path"]).read_text())
        if cache.get("expires_at", 0) - 60 > time.time():
            self.token, self.expires = cache["access_token"], cache["expires_at"]
            return self.token
        cid, secret = self._credentials()
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        async with http.post(TOKEN_URL, headers={"Authorization": f"Basic {auth}"},
                             data={"grant_type": "refresh_token", "refresh_token": cache["refresh_token"]}) as r:
            body = await r.json()
            if r.status != 200:
                raise RuntimeError(f"token refresh failed: {r.status} {body.get('error')}")
        self.token, self.expires = body["access_token"], time.time() + body.get("expires_in", 3600)
        return self.token

    async def now_playing(self):
        """{"id", "title", "pos" seconds, "playing"} or None. Never raises."""
        if time.time() < self.pause_until:
            return None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
                token = await self._access_token(http)
                async with http.get(NOW_URL, headers={"Authorization": f"Bearer {token}"}) as r:
                    if r.status == 429:  # rate limited: back off
                        self.pause_until = time.time() + int(r.headers.get("Retry-After", 30))
                        return None
                    if r.status == 401:
                        self.token = None
                        return None
                    if r.status != 200:  # 204 = nothing playing
                        return None
                    body = await r.json()
            item = body.get("item") or {}
            if not item.get("id"):
                return None
            return {"id": item["id"], "title": item.get("name") or "", "playing": bool(body.get("is_playing")),
                    "pos": (body.get("progress_ms") or 0) / 1000}
        except Exception as e:
            log.info("spotify lookup failed: %r", e)
            self.pause_until = time.time() + 30
            return None
