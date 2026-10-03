import asyncio

from lights_bridge.music import SPOTIFY_ID, Music


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def playing(pos=0, title="Song"):
    return {"title": title, "artist": "A", "room": "Kitchen", "key": f"uri|{title}", "pos": pos}


def make(bpm=None):
    clock = Clock()
    m = Music(clock)

    async def tempo(key):
        return bpm
    m._tempo = tempo
    return m, clock


def test_spotify_id_is_found_in_sonos_uris():
    assert SPOTIFY_ID.search("x-sonos-spotify:spotify%3atrack%3a0VjIjW4GlUZAMYd2vXMi3b?sid=9").group(1) == "0VjIjW4GlUZAMYd2vXMi3b"
    assert SPOTIFY_ID.search("spotify:track:0VjIjW4GlUZAMYd2vXMi3b")
    assert not SPOTIFY_ID.search("x-sonos-vli:RINCON_000:2,spotify:abcdef")


def test_tempo_falls_back_to_120():
    m, _ = make(None)
    asyncio.run(m.update(playing()))
    assert m.playing and m.bpm == 120 and m.info()["bpm"] is None
    m, _ = make(171.0)
    asyncio.run(m.update(playing()))
    assert m.bpm == 171.0 and m.info()["bpm"] == 171


def test_one_missed_poll_does_not_go_idle():
    m, clock = make()
    asyncio.run(m.update(playing()))
    clock.t += 4
    asyncio.run(m.update(None))
    assert m.playing
    clock.t += 20
    asyncio.run(m.update(None))
    assert not m.playing


def test_frame_alternates_on_the_beat_and_never_goes_dark():
    m, clock = make()  # 120 BPM: a beat every 0.5 s
    asyncio.run(m.update(playing()))
    a = m.frame(1, pulse=False)
    clock.t += 0.2
    assert m.frame(1, pulse=False) == a      # same beat: same colour, so no extra write
    clock.t += 0.5
    assert m.frame(1, pulse=False) != a      # next beat: other colour
    m.t0 = clock.t
    two = m.frame(1, pulse=False, every=2)
    clock.t += 0.6                               # one beat later: unchanged when every=2
    assert m.frame(1, pulse=False, every=2) == two
    clock.t += 0.5                               # two beats later: changed
    assert m.frame(1, pulse=False, every=2) != two
    for step in range(40):
        clock.t += 0.05
        assert all(max(c) >= 90 for c in m.frame(7))  # the pulse never drops near off


def test_idle_frame_is_a_steady_glow():
    m, clock = make()
    f1 = m.frame(3)
    clock.t += 7
    assert m.frame(3) == f1 and max(f1[0]) >= 90


def test_new_track_changes_colour():
    m, _ = make()
    asyncio.run(m.update(playing(title="One")))
    h = m.hue
    asyncio.run(m.update(playing(title="Two")))
    assert m.hue != h and m.title == "Two"
