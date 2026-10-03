import asyncio

from lights_bridge.music import Music
from lights_bridge.spotify import merge, titles_match

SONOS = {"title": "Anchor Point", "artist": "A", "room": "Kitchen", "key": "x-sonos-vli:RINCON_0:2,spotify:abc|Anchor Point", "pos": 61}
SPOTIFY = {"id": "0VjIjW4GlUZAMYd2vXMi3b", "title": "Anchor Point", "playing": True, "pos": 61.42}


def test_titles_match_loosely():
    assert titles_match("Anchor Point", "anchor  point")
    assert titles_match("Made You Look", "Made You Look - Radio Edit")
    assert not titles_match("Anchor Point", "Kaluma")
    assert not titles_match("", "Kaluma")


def test_merge_adds_track_id_and_precise_position():
    out = merge(SONOS, SPOTIFY)
    assert out["key"].startswith("spotify:track:0VjIjW4GlUZAMYd2vXMi3b|") and out["pos"] == 61.42 and out["precise"]


def test_merge_ignores_spotify_when_it_is_not_what_sonos_plays():
    assert merge(SONOS, {**SPOTIFY, "title": "Other song"}) is SONOS   # someone else's music on Sonos
    assert merge(SONOS, {**SPOTIFY, "playing": False}) is SONOS
    assert merge(SONOS, None) is SONOS and merge(None, SPOTIFY) is None


def test_spotify_connect_session_gets_its_tempo():
    seen = []

    class Clock:
        t = 500.0

        def __call__(self):
            return self.t

    m = Music(Clock())

    async def tempo(key):
        seen.append(key)
        return 124.0 if "0VjIjW4GlUZAMYd2vXMi3b" in key else None
    real = m._tempo

    async def go():
        from lights_bridge.music import SPOTIFY_ID
        assert not SPOTIFY_ID.search(SONOS["key"])           # Sonos alone: no id
        merged = merge(SONOS, SPOTIFY)
        assert SPOTIFY_ID.search(merged["key"]).group(1) == SPOTIFY["id"]
        m._tempo = tempo
        await m.update(merged)
    asyncio.run(go())
    assert m.bpm == 124.0 and m.info()["bpm"] == 124 and real is not None
    assert abs((m.clock() - m.t0) - 61.42) < 1e-6          # beat clock anchored on the ms position
