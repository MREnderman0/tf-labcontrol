import struct
from types import SimpleNamespace

from lab_control.application import environment


def pe_file(path, bits):
    data = bytearray(128)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 80)
    data[80:84] = b"PE\0\0"
    struct.pack_into("<H", data, 84, 0x8664 if bits == 64 else 0x14C)
    path.write_bytes(data)
    return path


def test_diagnostics_distinguish_architecture_symbols_and_hardware(tmp_path, monkeypatch):
    wrong = pe_file(tmp_path / "ps6000a.dll", 32)
    matching = pe_file(tmp_path / "TLPMX_64.dll", struct.calcsize("P") * 8)
    monkeypatch.setattr(
        environment,
        "sdk_candidates",
        lambda kind: {"pico": (wrong,), "tlpmx": (matching,), "pm100d": ()}[kind],
    )
    opened = []

    class FakeLibrary:
        def __init__(self, path):
            opened.append(path)
            self.library = SimpleNamespace(TLPMX_init=None, TLPMX_close=None, TLPMX_findRsrc=None)

        def close(self):
            pass

    monkeypatch.setattr(environment, "VendorLibrary", FakeLibrary)
    diagnostics = environment.dependency_diagnostics(())
    assert diagnostics["sdk"]["pico"]["state"] == "error"
    assert diagnostics["sdk"]["tlpmx"]["state"] == "loadable"
    assert diagnostics["sdk"]["pm100d"]["state"] == "missing"
    assert not diagnostics["sdk"]["tlpmx"]["libraries"][0]["hardware_accepted"]
    assert opened == [str(matching)]
    assert environment.find_sdk("tlpmx") == str(matching)
    assert environment.find_sdk("pico") == ""
