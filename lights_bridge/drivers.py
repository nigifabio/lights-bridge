"""Protocol drivers. Add a class here, list it in KINDS, and give it name prefixes for the scanner."""
import asyncio

from .light import Light


class Melk(Light):
    """ELK-BLEDOM family (ELK-BLEDOM*, ELK-BLE*, MELK-*, LEDBLE-*): 9-byte 7E..EF frames on fff3.

    These controllers report nothing back and drop the connection when written to often on a weak
    link (measured: ~15 s at 9 writes/s, ~60 s at 1 write/s, stable when idle). So software patterns
    send at most one frame per second and follow-music changes colour every second beat. The
    built-in effects (hw:*) need a single write and are the most reliable choice.
    """
    kind = "melk"
    label = "ELK-BLEDOM / MELK LED strip"
    name_prefixes = ("ELK-BLE", "MELK", "LEDBLE", "ELK-BULB")
    write_uuid = "0000fff3-0000-1000-8000-00805f9b34fb"
    frame_interval = 1.0
    beat_only = True
    beat_every = 2
    hw_patterns = {
        0x8A: "Fade 7 colours", 0x89: "Fade RGB", 0x88: "Jump 7 colours", 0x87: "Jump RGB",
        0x95: "Strobe 7 colours", 0x92: "Fade red-green", 0x93: "Fade red-blue",
        0x94: "Fade green-blue", 0x9C: "Strobe white",
    }

    async def _on_connect(self):
        # MELK controllers ignore commands until they get these two
        await self._write(bytes([0x7E, 0x07, 0x83]))
        await asyncio.sleep(0.2)
        await self._write(bytes([0x7E, 0x04, 0x04]))
        await asyncio.sleep(0.2)

    async def _verify(self):
        # The lamp reports no state. A read is a full round trip, and BLE delivers in order,
        # so a successful read proves the writes before it reached the lamp.
        await asyncio.sleep(0.15)
        await self.client.read_gatt_char(self.write_uuid)
        return "delivered"

    async def _power(self, on):
        # both the MELK and the ELK-BLEDOM variant of the frame: each controller obeys one
        if on:
            await self._write(bytes([0x7E, 0x07, 0x04, 0xFF, 0x00, 0x01, 0x02, 0x01, 0xEF]))
            await self._write(bytes([0x7E, 0x00, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF]))
        else:
            await self._write(bytes([0x7E, 0x07, 0x04, 0x00, 0x00, 0x00, 0x02, 0x01, 0xEF]))
            await self._write(bytes([0x7E, 0x00, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF]))

    async def _brightness(self, pct):
        await self._write(bytes([0x7E, 0x04, 0x01, pct, 0xFF, 0x00, 0xFF, 0x00, 0xEF]))

    async def _color(self, rgb):
        await self._write(bytes([0x7E, 0x00, 0x05, 0x03, *rgb, 0x00, 0xEF]))

    async def _hw_pattern(self, pid):
        await self._write(bytes([0x7E, 0x00, 0x03, int(pid[3:]), 0x03, 0x00, 0x00, 0x00, 0xEF]))
        await self._write(bytes([0x7E, 0x00, 0x02, self.state["speed"], 0x00, 0x00, 0x00, 0x00, 0xEF]))


class Govee(Light):
    """Govee RGBIC lights (tested on the H6076): 20-byte XOR-checksummed frames, per-segment colour.

    Power and brightness can be read back, so commands are confirmed by the lamp itself.
    """
    kind = "govee"
    label = "Govee RGBIC light"
    name_prefixes = ("Govee_", "ihoment_", "GBK_")
    write_uuid = "00010203-0405-0607-0809-0a0b0c0d2b11"
    notify_uuid = "00010203-0405-0607-0809-0a0b0c0d2b10"
    frame_interval = 0.35

    def __init__(self, cfg, saved=None, connector=None, music=None):
        super().__init__(cfg, saved, connector, music)
        self.segments = int(cfg.get("segments", 7))
        self.reported = {}

    @staticmethod
    def packet(*b):
        p = bytearray(b) + bytearray(19 - len(b))
        x = 0
        for v in p:
            x ^= v
        return bytes(p + bytes([x]))

    def _notified(self, _, data):
        # replies to the aa01 / aa04 queries: the lamp's real power and brightness
        if data[0] == 0xAA and data[1] == 0x01:
            self.reported["on"] = bool(data[2])
        elif data[0] == 0xAA and data[1] == 0x04:
            self.reported["brightness"] = data[2]

    query_wait = 0.1

    async def _query(self):
        self.reported = {}
        for _ in range(3):
            await self._write(self.packet(0xAA, 0x01))
            await self._write(self.packet(0xAA, 0x04))
            for _ in range(10):
                await asyncio.sleep(self.query_wait)
                if len(self.reported) == 2:
                    return self.reported
        raise RuntimeError("lamp did not report its state")

    async def _on_connect(self):
        await self.client.start_notify(self.notify_uuid, self._notified)
        r = await self._query()  # pick up changes made with the remote or the vendor app
        self.state["on"] = r["on"]
        if r["brightness"]:
            self.state["brightness"] = min(100, r["brightness"])

    async def _verify(self):
        r = await self._query()
        if r["on"] != self.state["on"]:
            raise RuntimeError(f"lamp reports {'on' if r['on'] else 'off'}")
        if r["on"] and r["brightness"] != self.state["brightness"]:
            raise RuntimeError(f"lamp reports brightness {r['brightness']}")
        return "confirmed"

    async def _power(self, on):
        await self._write(self.packet(0x33, 0x01, 1 if on else 0))

    async def _brightness(self, pct):
        await self._write(self.packet(0x33, 0x04, pct))

    async def _segments(self, rgb, mask):
        await self._write(self.packet(0x33, 0x05, 0x15, 0x01, *rgb, 0, 0, 0, 0, 0, mask & 0xFF, mask >> 8))

    async def _color(self, rgb):
        await self._segments(rgb, 0x7FFF)

    async def _frame(self, colors):
        groups = {}
        for i, c in enumerate(colors):
            groups[c] = groups.get(c, 0) | (1 << i)
        for c, mask in groups.items():
            await self._segments(c, mask)


KINDS = {c.kind: c for c in (Melk, Govee)}


def detect(name):
    """Driver kind for a BLE advertised name, or None when the device is not a known light."""
    for kind, cls in KINDS.items():
        if name and name.upper().startswith(tuple(p.upper() for p in cls.name_prefixes)):
            return kind
    return None
