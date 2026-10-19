"""Stateful wire peer for cross-layer E00 tests; no physical transport."""

from lab_control.instruments.devices.coremorrow_xmt.protocol import build_frame, encode_voltage_data

CONFIG_FRAME = bytes.fromhex(
    "aa013d5000000303062e07580100006458585858520a1010ffffffffffffffcfffffffffffffffffffffffffffffffffffffffffffffffff00000058e4"
)


class WirePeer:
    resource = "mock://e00-wire"

    def __init__(self):
        self.voltages = [0.0, 0.0, 0.0]
        self.waves = {}
        self.history = []
        self.closed = False
        self.fail_write = None
        self.fail_read = None

    def query_frame(self, payload):
        self.history.append(payload)
        if self.fail_read:
            raise self.fail_read
        if payload[3] == 80:
            return CONFIG_FRAME
        assert payload[3] == 51
        return build_frame(1, 51, payload=b"\0" + b"".join(encode_voltage_data(v) for v in self.voltages))

    def write_frame(self, payload):
        self.history.append(payload)
        if self.fail_write:
            raise self.fail_write
        channel = payload[5]
        if payload[3] == 0:
            self.voltages[channel] = payload[7] + int.from_bytes(payload[8:10], "big") / 10_000
        elif payload[3] == 15:
            self.waves[channel] = payload
        elif payload[3] == 17:
            self.waves.pop(channel, None)
        else:
            raise AssertionError(payload)

    def close(self):
        self.closed = True
