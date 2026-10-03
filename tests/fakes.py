"""In-memory stand-ins for the BLE lights, so the tests need no Bluetooth."""
from lights_bridge.drivers import Govee


class FakeClient:
    def __init__(self):
        self.is_connected = True
        self.writes = []
        self.fail_reads = 0   # next N reads raise
        self.fail_writes = 0  # next N writes raise

    async def write_gatt_char(self, uuid, data, response=False):
        if self.fail_writes:
            self.fail_writes -= 1
            self.is_connected = False
            raise RuntimeError("link lost")
        self.writes.append(bytes(data))

    async def read_gatt_char(self, uuid):
        if self.fail_reads:
            self.fail_reads -= 1
            raise RuntimeError("read failed")
        return b"\x7e"

    async def start_notify(self, uuid, cb):
        self.notify = cb

    async def disconnect(self):
        self.is_connected = False


class FakeGovee(FakeClient):
    """Behaves like the lamp: keeps power/brightness and answers the aa01 / aa04 queries."""

    def __init__(self, on=False, brightness=50, deaf=0):
        super().__init__()
        self.on, self.brightness = on, brightness
        self.deaf = deaf  # ignore the next N power commands (a command lost on the way)
        self.notify = None

    async def write_gatt_char(self, uuid, data, response=False):
        await super().write_gatt_char(uuid, data, response)
        if data[0] == 0x33 and data[1] == 0x01:
            if self.deaf:
                self.deaf -= 1
            else:
                self.on = bool(data[2])
        elif data[0] == 0x33 and data[1] == 0x04:
            self.brightness = data[2]
        elif data[0] == 0xAA and data[1] == 0x01:
            self.notify(None, Govee.packet(0xAA, 0x01, int(self.on)))
        elif data[0] == 0xAA and data[1] == 0x04:
            self.notify(None, Govee.packet(0xAA, 0x04, self.brightness))


class Connector:
    """Hands out the given clients in order, one per connection; counts the connections."""

    def __init__(self, *clients, fail=0):
        self.clients = list(clients)
        self.fail = fail
        self.calls = 0

    async def __call__(self, address):
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise RuntimeError("not in range")
        client = self.clients.pop(0) if len(self.clients) > 1 else self.clients[0]
        client.is_connected = True
        return client
