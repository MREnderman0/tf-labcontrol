"""RTM3004 controls and model-owned snapshot fields."""

from dataclasses import replace

from lab_control.domain.instruments import DeviceError, Parameter
from lab_control.instruments.base import Profile, command, enum, number, parameter, read_command

SOURCE = "https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1335_9090_01/RTM3000_UserManual_en_12.pdf"
BOOL = Parameter("boolean")
CHANNEL_SETTINGS = (
    "channel_enabled",
    "scale",
    "offset",
    "vertical_position",
    "coupling",
    "probe_attenuation",
    "bandwidth_limit",
    "polarity",
    "trigger_level",
)
GLOBAL_SETTINGS = (
    "timebase",
    "time_position",
    "time_reference",
    "horizontal_divisions",
    "record_length",
    "record_length_auto",
    "sample_rate",
    "trigger_source",
    "trigger_sweep",
    "trigger_type",
    "trigger_slope",
    "acquisition_mode",
    "decimation",
    "averages",
    "acquisition_count",
)
RANGED = ("scale", "offset", "trigger_level", "timebase", "time_position", "record_length")


def token(raw):
    return raw.decode("ascii").strip().upper()


def boolean(raw):
    value = token(raw)
    if value not in {"0", "1", "ON", "OFF"}:
        raise DeviceError("Invalid boolean response", "parse_error")
    return value in {"1", "ON"}


def rtm3004():
    commands = {}
    for name, path, spec, scoped in (
        ("channel_enabled", "CHAN{ch}:STAT", Parameter("boolean"), True),
        ("scale", "CHAN{ch}:SCAL", parameter("V/div", 1e-9, 1e9), True),
        ("offset", "CHAN{ch}:OFFS", parameter("V", -1e9, 1e9), True),
        ("coupling", "CHAN{ch}:COUP", enum("DCL", "ACL", "GND", "DC"), True),
        ("probe_attenuation", "PROB{ch}:SET:ATT:MAN", parameter("ratio", 0.001, 1000000), True),
        ("trigger_level", "TRIG:A:LEV{ch}", parameter("V", -1e9, 1e9), True),
        ("timebase", "TIM:SCAL", parameter("s/div", 1e-12, 500), False),
        ("time_position", "TIM:POS", parameter("s", -1e9, 1e9), False),
        ("record_length", "ACQ:POIN", Parameter("integer", minimum=1, maximum=80000000), False),
        ("trigger_source", "TRIG:A:SOUR", enum("CH1", "CH2", "CH3", "CH4", "EXT"), False),
        ("trigger_slope", "TRIG:A:EDGE:SLOP", enum("POS", "NEG", "EITH"), False),
        ("trigger_mode", "TRIG:A:MODE", enum("AUTO", "NORM", "STOP", "SINGLE"), False),
        ("acquisition_mode", "ACQ:TYPE", enum("REFR", "AVER", "ENV"), False),
        ("averages", "ACQ:AVER:COUN", Parameter("integer", minimum=2, maximum=1024), False),
        ("acquisition_count", "ACQ:NSIN:COUN", Parameter("integer", minimum=1, maximum=1000000), False),
        ("decimation", "CHAN:TYPE", enum("SAMP", "PDET", "HRES"), False),
        ("bandwidth_limit", "CHAN{ch}:BAND", enum("FULL", "B20"), True),
    ):
        decode = boolean if spec.kind == "boolean" else token if spec.kind == "enum" else number
        commands[name] = replace(command(name, path, spec, scoped=scoped), decode=decode)
    commands["record_length"] = replace(
        commands["record_length"],
        capability=replace(
            commands["record_length"].capability,
            writable=False,
            reason="当前固件的存储深度设置未通过读回验证；暂仅显示实测点数",
        ),
    )
    for name, path, decode, scoped in (
        ("vertical_position", "CHAN{ch}:POS?", number, True),
        ("polarity", "CHAN{ch}:POL?", token, True),
        ("sample_rate", "ACQ:SRAT?", number, False),
        ("record_length_auto", "ACQ:POIN:AUT?", boolean, False),
        ("time_reference", "TIM:REF?", number, False),
        ("horizontal_divisions", "TIM:DIV?", number, False),
        ("trigger_type", "TRIG:A:TYPE?", token, False),
        ("trigger_sweep", "TRIG:A:MODE?", token, False),
        ("acquisition_state", "ACQ:STAT?", token, False),
        ("transfer_format", "FORM?", token, False),
        ("byte_order", "FORM:BORD?", token, False),
        ("waveform_header", "CHAN{ch}:DATA:HEAD?", token, True),
    ):
        commands[name] = replace(read_command(name, path, scoped=scoped), decode=decode)
    for name, path in (("run", "RUN"), ("stop", "STOP"), ("single", "SING")):
        commands[name] = command(name, path, read=False, scoped=False, confirm=True)
    commands["auto_setup"] = command("auto_setup", "AUT", read=False, scoped=False, confirm=True)
    commands["segmented"] = command(
        "segmented", "ACQ:SEGM:STAT", Parameter("boolean"), scoped=False, option="segmented_memory"
    )
    return Profile(
        "rs.rtm3004",
        ("Rohde&Schwarz", "Rohde-Schwarz"),
        ("RTM3004",),
        "oscilloscope",
        (1, 2, 3, 4),
        SOURCE,
        "1335.9090 revision 12: 17.4, 17.9.1; REAL32 qualified on FW 01.400",
        commands,
        ("channel_enabled", "scale", "offset", "timebase", "trigger_mode"),
        acquisition="rtm",
    )
