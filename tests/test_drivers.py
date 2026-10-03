import asyncio

import pytest

from fakes import Connector, FakeClient, FakeGovee
from lights_bridge.drivers import Govee, Melk, detect

CFG = {"id": "x", "name": "X", "address": "AA:BB:CC:DD:EE:FF"}


def run(coro):
    return asyncio.run(coro)


def melk(client=None, **kw):
    client = client or FakeClient()
    return Melk(CFG, connector=Connector(client), **kw), client


def govee(client=None, **kw):
    client = client or FakeGovee()
    return Govee({**CFG, "segments": 7}, connector=Connector(client), **kw), client


# ---- frames ------------------------------------------------------------
def test_govee_packet_is_20_bytes_with_xor_checksum():
    p = Govee.packet(0x33, 0x01, 0x01)
    assert len(p) == 20 and p[:3] == b"\x33\x01\x01"
    assert p[19] == 0x33 ^ 0x01 ^ 0x01
    x = 0
    for b in p:
        x ^= b
    assert x == 0


def test_melk_frames():
    async def go():
        light, c = melk()
        await light.apply({"on": True, "brightness": 60, "color": "#ff8000"})
        assert c.writes[0] == bytes([0x7E, 0x07, 0x83])                       # init on connect
        assert bytes([0x7E, 0x00, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF]) in c.writes  # power on
        assert bytes([0x7E, 0x04, 0x01, 60, 0xFF, 0x00, 0xFF, 0x00, 0xEF]) in c.writes    # brightness
        assert c.writes[-1] == bytes([0x7E, 0x00, 0x05, 0x03, 0xFF, 0x80, 0x00, 0x00, 0xEF])
        assert light.confirmed == "delivered"
    run(go())


def test_govee_segment_frame_groups_equal_colours():
    async def go():
        light, c = govee()
        await light._ensure()
        c.writes.clear()
        red, blue = (255, 0, 0), (0, 0, 255)
        await light._frame([red, blue, red, blue, red, blue, red])
        assert len(c.writes) == 2  # one write per colour, not per segment
        assert c.writes[0][:7] == bytes([0x33, 0x05, 0x15, 0x01, 255, 0, 0])
        assert c.writes[0][12] == 0b1010101 and c.writes[1][12] == 0b0101010
    run(go())


# ---- verified commands -------------------------------------------------
def test_govee_on_off_is_confirmed_by_the_lamp():
    async def go():
        light, c = govee()
        await light.apply({"on": True})
        assert c.on and light.state["on"] and light.confirmed == "confirmed"
        await light.apply({"on": False})
        assert not c.on and not light.state["on"] and light.confirmed == "confirmed"
    run(go())


def test_govee_resends_when_the_lamp_did_not_obey():
    async def go():
        light, c = govee(FakeGovee(deaf=1))
        await light.apply({"on": True})
        assert c.on and light.error is None
        assert light.connector.calls == 2  # reconnected once to resend
    run(go())


def test_govee_reports_an_error_when_the_lamp_never_obeys():
    async def go():
        light, c = govee(FakeGovee(deaf=99))
        with pytest.raises(RuntimeError):
            await light.apply({"on": True})
        assert "not confirmed" in light.error and light.confirmed is None
    run(go())


def test_govee_picks_up_state_changed_with_the_remote():
    async def go():
        light, c = govee(FakeGovee(on=True, brightness=33))
        await light.apply({})
        assert light.state["on"] is True and light.state["brightness"] == 33
    run(go())


def test_melk_resends_after_a_lost_link():
    async def go():
        client = FakeClient()
        client.fail_reads = 1  # the delivery check fails once
        light, c = melk(client)
        await light.apply({"on": False})
        assert light.confirmed == "delivered" and light.connector.calls == 2
        offs = [w for w in c.writes if w == bytes([0x7E, 0x00, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF])]
        assert len(offs) == 2  # sent again after reconnecting
    run(go())


def test_connect_is_retried_then_reported():
    async def go():
        light = Melk(CFG, connector=Connector(FakeClient(), fail=2))
        await light.apply({"on": True})
        assert light.error is None
        dead = Melk(CFG, connector=Connector(FakeClient(), fail=999))
        with pytest.raises(RuntimeError):
            await dead.apply({"on": True})
        assert "cannot reach" in dead.error
    run(go())


# ---- state logic -------------------------------------------------------
def test_colour_turns_the_light_on_and_clears_the_pattern():
    async def go():
        light, c = melk()
        await light.apply({"pattern": "hw:138"})
        assert light.state["on"] and light.state["pattern"] == "hw:138"
        await light.apply({"color": "#00ff00"})
        assert light.state["pattern"] is None and light.state["color"] == "#00ff00"
    run(go())


def test_bad_input_is_rejected_without_touching_the_light():
    async def go():
        light, c = melk()
        for bad in ({"color": "red"}, {"pattern": "nope"}, {"pattern": "hw:1"}, {"brightness": "x"}):
            with pytest.raises((ValueError, TypeError)):
                await light.apply(bad)
        assert light.state["pattern"] is None and light.error is None
    run(go())


def test_brightness_is_clamped():
    async def go():
        light, c = govee()
        await light.apply({"brightness": 500})
        assert light.state["brightness"] == 100 and c.brightness == 100
    run(go())


def test_software_pattern_sends_frames_and_stops_on_off():
    async def go():
        light, c = govee()
        light.frame_interval = 0.01
        await light.apply({"pattern": "rainbow", "speed": 100})
        await asyncio.sleep(0.08)
        frames = [w for w in c.writes if w[:3] == bytes([0x33, 0x05, 0x15])]
        assert len(frames) >= 7  # at least one full 7-segment frame
        await light.apply({"on": False})
        assert light.pattern_task is None and light.state["pattern"] is None
        n = len(c.writes)
        await asyncio.sleep(0.05)
        assert len(c.writes) == n  # nothing sent after off
    run(go())


def test_scanner_recognises_light_names():
    assert detect("MELK-OF21M") == "melk"
    assert detect("ELK-BLEDOM01") == "melk"
    assert detect("Govee_H6076_1A2B") == "govee"
    assert detect("ihoment_H6159_1234") == "govee"
    assert detect("[TV] Living room") is None
    assert detect("") is None and detect(None) is None


def test_melk_patterns_are_gentle_on_the_link():
    # at most one frame per second, and music changes colour every second beat with no pulse
    assert Melk.frame_interval >= 1.0 and Melk.beat_only and Melk.beat_every == 2
