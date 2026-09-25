"""Synthetic protocol data, not a captured physical waveform."""

import struct

import numpy as np


def binary(channel=1, endian="<", width=1, **fields):
    data = bytearray(346)
    data[:8] = b"WAVEDESC"
    data[16:26] = b"LECROY_2_3"
    entries = {
        32: ("h", width),
        34: ("h", int(endian == "<")),
        36: ("i", 346),
        60: ("i", 4 * (width + 1)),
        116: ("i", 4),
        124: ("i", 0),
        128: ("i", 3),
        144: ("i", 1),
        156: ("f", 0.01),
        160: ("f", 0.02),
        176: ("f", 0.001),
        180: ("d", -0.001),
        328: ("f", 1.0),
        336: ("f", 1.0),
        344: ("h", channel - 1),
    }
    for offset, value in fields.items():
        entries[int(offset)] = value
    for offset, (fmt, value) in entries.items():
        struct.pack_into(endian + fmt, data, offset, value)
    data[196] = ord("V")
    data[244] = ord("s")
    data += np.array([-2, 0, 1, 3], dtype=endian + ("i2" if width else "i1")).tobytes()
    return f"C{channel}:WF ALL,#9{len(data):09d}".encode() + data + b"\n"


GLOBALS = {
    "timebase": b"TDIV 0.001 S",
    "time_position": b"TRDL 0 S",
    "trigger_mode": b"TRMD STOP",
    "record_length": b"MSIZ 1000",
    "trigger_setup": b"TRSE EDGE,SR,C1,HT,OFF",
    "sequence_setup": b"SEQ OFF,10",
    "interleaved": b"ILVD OFF",
    "transfer_format": b"CFMT DEF9,WORD,BIN",
    "response_header": b"CHDR SHORT",
    "byte_order": b"CORD LO",
    "waveform_setup": b"WFSU SP,0,NP,0,FP,0,SN,0",
}
CHANNELS = {
    "channel_enabled": b"ON",
    "scale": b"0.02 V",
    "offset": b"0 V",
    "coupling": b"D50",
    "probe_attenuation": b"1",
    "trigger_level": b"0 V",
    "trigger_slope": b"POS",
}


def snapshot_exchanges(profile, channels=(1,), overrides=None):
    from ..adapter import CHANNEL_SETTINGS, GLOBAL_SETTINGS

    overrides = overrides or {}
    exchanges = []
    for channel, operations in ((1, GLOBAL_SETTINGS), *((ch, CHANNEL_SETTINGS) for ch in channels)):
        for operation in operations:
            raw = GLOBALS[operation] if operation in GLOBALS else CHANNELS[operation]
            exchanges.append(
                (
                    profile.commands[operation].query.format(ch=channel),
                    overrides.get(f"{channel}.{operation}", raw),
                )
            )
    return exchanges
