import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from fakes import Connector, FakeClient, FakeGovee
from lights_bridge.app import create_app
from lights_bridge.music import Music


def serve(tmp_path, test, config=None, scan=()):
    if config:
        (tmp_path / "config.json").write_text(json.dumps(config))

    async def connector(address):
        client = clients.setdefault(address, FakeGovee() if address.startswith("D8") else FakeClient())
        client.is_connected = True
        return client

    async def scanner():
        return list(scan)

    clients = {}

    async def go():
        music = Music()
        music.available = True
        app = create_app(tmp_path, connector, scanner, music, background=False)
        async with TestClient(TestServer(app)) as http:
            await test(http, clients)
    asyncio.run(go())


LAMP = {"id": "lamp", "name": "Lamp", "type": "govee", "address": "D8:00:00:00:00:01", "segments": 7}


def test_page_is_served(tmp_path):
    async def t(http, _):
        r = await http.get("/")
        assert r.status == 200 and "<title>Lights</title>" in await r.text()
    serve(tmp_path, t)


def test_starts_empty_and_adds_a_scanned_light(tmp_path):
    found = [{"address": "BE:00:00:00:00:02", "name": "MELK-OF21M", "rssi": -70},
             {"address": "11:22:33:44:55:66", "name": "Some TV", "rssi": -40}]

    async def t(http, _):
        assert await (await http.get("/api/lights")).json() == []
        devs = await (await http.post("/api/scan")).json()
        assert devs[0]["type"] == "melk" and not devs[0]["added"]   # lights first, despite weaker signal
        assert devs[1]["type"] is None
        r = await http.post("/api/lights", json={"name": "Desk strip", "type": "melk", "address": devs[0]["address"]})
        assert r.status == 201 and (await r.json())["id"] == "desk-strip"
        assert (await (await http.post("/api/scan")).json())[0]["added"]
        saved = json.loads((tmp_path / "config.json").read_text())
        assert saved["lights"][0]["address"] == "BE:00:00:00:00:02"   # survives a restart
    serve(tmp_path, t, scan=found)


def test_add_rejects_bad_input_and_duplicates(tmp_path):
    async def t(http, _):
        for body in ({"name": "", "type": "melk", "address": "AA:BB:CC:DD:EE:FF"},
                     {"name": "x", "type": "hue", "address": "AA:BB:CC:DD:EE:FF"},
                     {"name": "x", "type": "melk", "address": "nope"},
                     {"name": "x", "type": "govee", "address": LAMP["address"]}):
            r = await http.post("/api/lights", json=body)
            assert r.status == 400 and (await r.json())["error"]
    serve(tmp_path, t, {"lights": [LAMP]})


def test_switch_colour_pattern_and_persisted_state(tmp_path):
    async def t(http, clients):
        r = await http.post("/api/lights/lamp", json={"on": True, "brightness": 40, "color": "#00ff9d"})
        body = await r.json()
        assert r.status == 200 and body["state"]["on"] and body["confirmed"] == "confirmed"
        assert clients[LAMP["address"]].on and clients[LAMP["address"]].brightness == 40
        ids = [p["id"] for p in body["patterns"]]
        assert "music" in ids and "rainbow" in ids
        r = await http.post("/api/lights/lamp", json={"pattern": "music"})
        assert (await r.json())["music"] == {"playing": False, "title": "", "artist": "", "room": "", "bpm": None}
        r = await http.post("/api/lights/lamp", json={"on": False})
        assert (await r.json())["state"] == {"on": False, "brightness": 40, "color": "#00ff9d", "pattern": None, "speed": 50}
        assert json.loads((tmp_path / "state.json").read_text())["lamp"]["color"] == "#00ff9d"
    serve(tmp_path, t, {"lights": [LAMP]})


def test_errors(tmp_path):
    async def t(http, _):
        assert (await http.post("/api/lights/nope", json={"on": True})).status == 404
        r = await http.post("/api/lights/lamp", json={"color": "blue"})
        assert r.status == 400 and "bad colour" in (await r.json())["error"]
    serve(tmp_path, t, {"lights": [LAMP]})


def test_rename_and_remove(tmp_path):
    async def t(http, _):
        r = await http.post("/api/lights/lamp", json={"name": "Reading lamp"})
        assert (await r.json())["name"] == "Reading lamp"
        assert (await http.delete("/api/lights/lamp")).status == 200
        assert await (await http.get("/api/lights")).json() == []
        assert json.loads((tmp_path / "config.json").read_text())["lights"] == []
    serve(tmp_path, t, {"lights": [LAMP]})
