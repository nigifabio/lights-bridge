import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lights_bridge.app import create_app
from lights_bridge.remotes import Remotes

HUB = {"host": "192.0.2.10", "mac": "AA:BB:CC:00:00:01", "model": "RM4 pro", "rf": True}


class FakeHub:
    """Stands in for the Broadlink hub: learns a canned code, records what is sent."""

    def __init__(self):
        self.sent = []
        self.next_code = b"\x26\x00IR"
        self.offline = False

    def discover(self, timeout=5):
        return [dict(HUB)]

    def identify(self, host):
        if host != HUB["host"]:
            raise RuntimeError("no answer")
        return dict(HUB)

    def send(self, hub, code):
        if self.offline:
            raise RuntimeError("hub is not reachable")
        self.sent.append(code)

    def learn_ir(self, hub, timeout=30):
        if self.next_code is None:
            raise TimeoutError("no button press seen")
        return self.next_code

    def learn_rf(self, hub, timeout=30):
        return b"\xb2\x00RF"


def run(coro):
    return asyncio.run(coro)


def make(tmp_path):
    hub = FakeHub()
    return Remotes(tmp_path, hub), hub


def test_hub_is_found_added_once_and_saved(tmp_path):
    async def go():
        r, _ = make(tmp_path)
        assert (await r.discover())[0]["added"] is False
        hub = await r.add_hub(HUB["host"], "Living room hub")
        assert hub["id"] == "living-room-hub" and (await r.discover())[0]["added"]
        with pytest.raises(ValueError):
            await r.add_hub(HUB["host"])
        assert Remotes(tmp_path, FakeHub()).data["hubs"][0]["mac"] == HUB["mac"]  # survives a restart
    run(go())


def test_template_creates_unlearned_buttons(tmp_path):
    async def go():
        r, _ = make(tmp_path)
        await r.add_hub(HUB["host"], "hub")
        neon = r.add_remote("Neon sign", "hub", "dimmer")
        names = [b["name"] for b in neon["buttons"]]
        assert names[:2] == ["On", "Off"] and "50%" in names and "Timer 60" in names
        assert len({b["id"] for b in neon["buttons"]}) == len(names)   # ids are unique
        assert not any(b["learned"] for b in r.info()["remotes"][0]["buttons"])
        assert "code" not in r.info()["remotes"][0]["buttons"][0]      # codes never go to the page
    run(go())


def test_learn_then_press_sends_the_same_code(tmp_path):
    async def go():
        r, hub = make(tmp_path)
        await r.add_hub(HUB["host"], "hub")
        r.add_remote("TV", "hub", "tv")
        with pytest.raises(ValueError):
            await r.press("tv", "power")          # not learned yet
        await r.learn("tv", "power", "ir")
        await r.press("tv", "power")
        await r.press("tv", "power", repeat=3)
        assert hub.sent == [b"\x26\x00IR"] * 4
        await r.learn("tv", "mute", "rf")
        await r.press("tv", "mute")
        assert hub.sent[-1] == b"\xb2\x00RF"
        saved = json.loads((tmp_path / "remotes.json").read_text())
        assert saved["remotes"][0]["buttons"][0]["code"]
    run(go())


def test_failed_learning_keeps_the_old_code(tmp_path):
    async def go():
        r, hub = make(tmp_path)
        await r.add_hub(HUB["host"], "hub")
        r.add_remote("TV", "hub", "tv")
        await r.learn("tv", "power")
        hub.next_code = None
        with pytest.raises(TimeoutError):
            await r.learn("tv", "power")
        await r.press("tv", "power")
        assert hub.sent == [b"\x26\x00IR"]
    run(go())


def test_buttons_and_remotes_can_be_added_and_removed(tmp_path):
    async def go():
        r, _ = make(tmp_path)
        await r.add_hub(HUB["host"], "hub")
        r.add_remote("Fan", "hub")
        assert r.add_button("fan", "Power")["id"] == "power"
        assert r.add_button("fan", "Power")["id"] == "power-2"
        r.remove_button("fan", "power")
        with pytest.raises(ValueError):
            r.remove_hub("hub")                    # still used by a remote
        r.remove_remote("fan")
        r.remove_hub("hub")
        assert r.info()["hubs"] == [] and r.info()["remotes"] == []
        for bad in (lambda: r.add_remote("", "hub"), lambda: r.add_remote("x", "nope"), lambda: r.remove_remote("nope")):
            with pytest.raises((ValueError, KeyError)):
                bad()
    run(go())


def test_api(tmp_path):
    async def go():
        hub = FakeHub()
        app = create_app(tmp_path, background=False, hub_backend=hub)
        async with TestClient(TestServer(app)) as http:
            assert (await (await http.get("/api/remotes")).json())["remotes"] == []
            assert (await (await http.post("/api/hubs/discover")).json())[0]["model"] == "RM4 pro"
            assert (await http.post("/api/hubs", json={"host": "192.0.2.99"})).status == 502   # nothing there
            assert (await http.post("/api/hubs", json={"host": HUB["host"], "name": "Hub"})).status == 200
            r = await http.post("/api/remotes", json={"name": "Neon", "hub": "hub", "template": "dimmer"})
            assert (await r.json())["remotes"][0]["buttons"][0] == {"id": "on", "name": "On", "learned": False}
            assert (await http.post("/api/remotes/neon/buttons/on/press")).status == 400       # not learned
            assert (await http.post("/api/remotes/neon/buttons/on/learn", json={"kind": "ir"})).status == 200
            assert (await http.post("/api/remotes/neon/buttons/on/press")).status == 200
            assert hub.sent == [b"\x26\x00IR"]
            assert (await http.post("/api/remotes/nope/buttons/on/press")).status == 404
            hub.offline = True
            r = await http.post("/api/remotes/neon/buttons/on/press")
            assert r.status == 502 and "not reachable" in (await r.json())["error"]
            assert (await http.delete("/api/remotes/neon")).status == 200
    run(go())
