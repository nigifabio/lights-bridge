# lights-bridge

A small web page and HTTP API to control cheap Bluetooth (BLE) lights from a Raspberry Pi or any
Linux box with Bluetooth: the kind of lamps and LED strips that ship with a little remote and a
phone app, and no way to automate them.

- **One page, phone and desktop:** switch, brightness, colour palette + custom colour, patterns, speed.
- **Scan and add:** the page scans for nearby lights, recognises the supported ones, and adds them.
- **Commands are verified:** every command is checked and resent after a reconnect if it did not land.
- **Patterns:** rainbow, sunset, ocean, aurora, forest, breathe, candle, fire, party, plus the
  effects built into the light's own controller.
- **Follow music:** lights pulse at the tempo of what is playing on Sonos.
- **Remotes:** replay the buttons of infrared and 433 MHz remotes (TV, fan, neon sign...) through a
  Broadlink hub, taught from the real remote in the page.
- **HTTP API:** everything the page does is one `curl` away, for scripts and home automation.

No cloud, no account, no build step: Python, [bleak](https://github.com/hbldh/bleak) and one HTML file.

## Supported lights

| Type | Devices (BLE name) | State read back | Notes |
|---|---|---|---|
| `melk` | ELK-BLEDOM family: `ELK-BLEDOM*`, `ELK-BLE*`, `MELK-*`, `LEDBLE-*` strips and lamps | no | Tested on a `MELK-OF21M`. |
| `govee` | Govee RGBIC lights: `Govee_*`, `ihoment_*` | power, brightness | Tested on an H6076 (7 segments). Set `segments` for other models. |

Lights that only have an infrared or 433 MHz remote and no Bluetooth do not show up in a scan.
They can still be driven through a hub: see [Remotes](#remotes).

Adding a protocol is one class in [`lights_bridge/drivers.py`](lights_bridge/drivers.py): the frames
for power, brightness and colour, and the name prefixes the scanner should recognise.

## Run it

Requirements: Linux with BlueZ and a Bluetooth LE adapter (a Raspberry Pi 3/4/5 works out of the
box), Python 3.11+.

```bash
git clone https://github.com/nigifabio/lights-bridge && cd lights-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m lights_bridge
```

Open `http://<host>:8765`, go to the **Add a light** tab and press **Scan**. Plug the light in and close its phone
app first: a light that is connected to a phone does not advertise.

To keep it running, see the example unit [`lights-bridge.service`](lights-bridge.service).

### Docker

The container uses the **host's** Bluetooth daemon over D-Bus, so Bluetooth must work on the host
(`bluetoothctl show` says `Powered: yes`). Linux hosts only.

```bash
docker compose up -d
```

or without compose:

```bash
docker run -d --name lights-bridge --restart unless-stopped --network host \
  -v /run/dbus:/run/dbus:ro -v "$PWD/data:/data" ghcr.io/nigifabio/lights-bridge:latest
```

`--network host` is only needed for follow-music (Sonos discovery uses multicast); without it,
publish the port with `-p 8765:8765`.

## Configuration

Everything lives in the data directory (`./data`, or `$LIGHTS_DATA`; `/data` in the container):
`config.json` (the lights, written by the page when you add or remove one) and `state.json` (last
known state). See [`config.example.json`](config.example.json).

| Key | Default | Meaning |
|---|---|---|
| `host`, `port` | `0.0.0.0`, `8765` | Listen address. `LIGHTS_HOST` / `LIGHTS_PORT` override them. |
| `idle_disconnect` | `120` | Seconds the Bluetooth connection is kept after the last command. While it is held, the vendor phone app cannot connect to that light. |
| `music` | `true` | Offer the follow-music pattern (needs Sonos speakers on the LAN). |
| `spotify` | unset | Optional, see Follow music. |
| `lights[]` | `[]` | `id`, `name`, `type`, `address`, and `segments` for Govee RGBIC lights. |

## API

```bash
curl http://localhost:8765/api/lights
curl -X POST -H 'Content-Type: application/json' -d '{"on":true,"brightness":40,"color":"#00ff9d"}' http://localhost:8765/api/lights/floor-lamp
curl -X POST -H 'Content-Type: application/json' -d '{"pattern":"rainbow","speed":80}' http://localhost:8765/api/lights/floor-lamp
curl -X POST -H 'Content-Type: application/json' -d '{"on":false}' http://localhost:8765/api/lights/floor-lamp
curl -X POST http://localhost:8765/api/scan
curl -X POST -H 'Content-Type: application/json' -d '{"name":"Desk strip","type":"melk","address":"AA:BB:CC:DD:EE:02"}' http://localhost:8765/api/lights
curl -X DELETE http://localhost:8765/api/lights/desk-strip
```

Patch keys for a light: `on`, `brightness` (1-100), `color` (`#rrggbb`), `pattern` (an id from the
light's `patterns` list, or `null`), `speed` (1-100), `name`. Setting a colour, pattern or
brightness turns the light on. A command that could not be verified returns HTTP 502 with `error`.

## How commands are verified

A command is retried up to three times, reconnecting in between, before the page shows an error.

- **Govee:** power and brightness are read back from the lamp and compared (`confirmed`).
- **ELK-BLEDOM / MELK:** these report nothing. The bridge does a read round trip after its writes;
  BLE delivers in order, so a successful read proves the command reached the controller
  (`delivered`). It cannot prove the controller obeyed.

## Follow music

The `music` pattern follows what Sonos is playing (discovered with [SoCo](https://github.com/SoCo/SoCo),
polled every 2 s). There is no audio capture: the bridge runs a beat clock at the track's tempo,
anchored on the play position, so the pulse matches the tempo but is not locked to the exact beat.
The tempo comes from [ReccoBeats](https://reccobeats.com) when the track's Spotify id is known.
Sonos provides it when you play from the Sonos app. During Spotify Connect sessions Sonos hides
it; add the optional `spotify` setting below and the bridge asks Spotify instead, which also gives
the play position in milliseconds for a tighter pulse. Radio, other sources and tracks ReccoBeats
does not know fall back to 120 BPM. Each track gets its own colour.

```json
"spotify": {
  "client_id": "...", "client_secret": "...",
  "cache_path": "/data/spotify-cache"
}
```

`cache_path` is a token cache from an authorisation-code login with the
`user-read-currently-playing` scope, in [spotipy](https://github.com/spotipy-dev/spotipy)'s format;
the bridge only reads it. `credentials_file` can replace the two client keys with a JSON file that
holds them. Only playback on the authorised Spotify account is seen.

## Remotes

For anything with an infrared or 433 MHz radio remote, the **Remotes** tab replays the remote's
buttons through a [Broadlink](https://github.com/mjg59/python-broadlink) hub (RM4 Pro for
infrared + radio, RM4 mini for infrared only).

1. Join the hub to your Wi-Fi (2.4 GHz) with the Broadlink app. In the app's device settings keep
   **Lock device** off, or the hub refuses local control. The app is not needed after that.
2. In the Remotes tab press **Find hub** (or type its IP address).
3. Create a remote from a button set (LED / neon dimmer, TV, speaker, fan, RGB light, or empty).
4. Tap each button and press the same button on the real remote, pointed at the hub. For radio
   remotes switch to **Radio**: hold the button until the hub finds the frequency, release, then
   press it once. Radio remotes with rolling codes (garage doors, some sockets) cannot be learned.

Buttons then send with one tap, or from a script:

```bash
curl http://localhost:8765/api/remotes
curl -X POST http://localhost:8765/api/remotes/neon-sign/buttons/on/press
curl -X POST -H 'Content-Type: application/json' -d '{"repeat":3}' http://localhost:8765/api/remotes/tv/buttons/vol-plus/press
```

This is one-way: the bridge replays buttons and cannot know whether the device is on. Hubs,
remotes and learned codes are stored in `remotes.json` in the data directory.

## Limits worth knowing

- **No authentication.** Anyone who can reach the port can switch your lights. Keep it on your LAN
  or put it behind a reverse proxy that adds a login.
- **Changes made with the physical remote** are not seen for lights that report no state.
- **Cheap ELK-BLEDOM controllers drop the connection when written to often** over a weak link.
  The driver therefore sends at most one pattern frame per second and, in follow-music, changes
  colour every second beat without a pulse. Their built-in effects need a single write and are
  the most reliable. If the light is far from the adapter, a USB Bluetooth dongle on a short
  extension cable helps more than anything in software.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests use in-memory fake lights (`tests/fakes.py`), so they need no Bluetooth hardware.

## License

MIT
