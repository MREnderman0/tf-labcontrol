"""Generate capability inventory and model implementation sections without opening hardware.

Run with: uv run python scripts/generate_device_docs.py [--check]
Independent model notes live in their packages; legacy notes remain here.
Commands and capabilities always come from the registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from lab_control.application.config import ConnectionConfig
from lab_control.application.controls import CATEGORY_LABELS, COMMON, MEASUREMENT_CATEGORIES, OPERATION_LABELS
from lab_control.domain.instruments import jsonable
from lab_control.instruments.base import ScpiAdapter
from lab_control.instruments.registry import DEVICE_DEFINITIONS, PENDING, PROFILES, describe
from lab_control.transport.base import MockTransport

ROOT = Path(__file__).resolve().parents[1]
MODEL_CARD_MARKER = "<!-- Generated from an independent device package; edit its README.md. -->"
LEGACY_CARD_MARKER = "<!-- Generated from scripts/generate_device_docs.py legacy metadata. -->"

CATEGORY_FOLDERS = {
    "signal-sources": "信号源",
    "power-supplies": "电源",
    "oscilloscopes": "示波器",
    "spectrum-analyzers": "频谱仪",
    "measurement-analyzers": "测量与分析仪",
    "temperature-load-drivers": "温控、负载与驱动",
}

# adapter, card, display name, connection, driver, model-specific acceptance boundary
LEGACY_MODELS = [
    (
        "siglent.sdg6032x",
        "signal-sources/sdg6032x",
        "Siglent SDG6032X",
        "USB / LAN VXI-11 / 已配置 TCP Socket",
        "VISA Runtime；直接 TCP 不需要 VISA",
        "BSWV/MDWV/SWWV/BTWV 是复合命令。先显式启用调制/扫频/突发，再设置子参数；不会替用户启用。WVDT 使用小端有符号 16 位数据，二进制末尾不追加换行。建议先用 USB/VXI-11 验证上传。",
    ),
    (
        "keithley.2231a",
        "power-supplies/keithley-2231a",
        "Keithley 2231A-30-3",
        "2231A-001 USB 适配器 / 虚拟串口",
        "专用 USB 适配器驱动；PySerial 或 VISA ASRL",
        "附件只写 2231A；实机身份必须为 2231A-30-3。INST:NSEL 后设置和读回在同一 worker 中执行。CHAN:OUTP 是选中通道，OUTP 是整机；CH3 限制 5 V。串并联/跟踪必须先核对接线。",
    ),
    (
        "siglent.spd4306x",
        "power-supplies/spd4306x",
        "Siglent SPD4306X",
        "USB / LAN VISA / 已配置 TCP Socket",
        "64 位 VISA Runtime 或直接 TCP",
        "四通道；CH1/4 6 V、3 A；CH2/3 30 V、6 A。命令格式 VOLT CH2,值；设定读回 VOLT:SET? CH2。远端采样仅 CH2/3。列表上传不执行列表。未发送手册未列出的 SYST:ERR?。",
    ),
    (
        "itech.it6322",
        "power-supplies/it6322",
        "ITECH IT6322",
        "RS-232 / 已安装 USB 或 GPIB 接口",
        "相应接口驱动；VISA 或 PySerial",
        "依据 ITECH IT6322-402205 V2.2 厂家手册（经销商托管副本），不能套用 IT6300 新系列命令。OUTP 控制选中通道；VOLT:PROT 在该型号代表电压上限，不能当成硬件 OVP 跳闸值。",
    ),
    (
        "pico.6404e",
        "oscilloscopes/picoscope-6404e",
        "PicoScope 6404E",
        "USB 3.x + ps6000a SDK",
        "PicoSDK 64 位、ps6000a.dll 和 USB 驱动",
        "显式选择序列号，使用 8 位模式；先设置通道，再 block 或 streaming。6404E 的 1 MΩ 量程为 ±10 mV～±20 V，50 Ω 最大 ±5 V；偏移上限在 ≤1 V 量程为 ±1.25 V，较大量程为 ±20 V，依据[6000E 数据表第 31 页](https://www.picotech.com/download/datasheets/picoscope-6000e-series-data-sheet.pdf)。不能使用共享 SDK 枚举中的 50 V 量程。触发阈值按配置偏移转换为 ADC 计数。SDK 缓冲区保持到 Stop 和解除注册后才释放。streaming 为有限批次，批次间有间隔，尚未承诺无缝长时间数据流。",
    ),
    (
        "rs.rtm3004",
        "oscilloscopes/rtm3004",
        "R&S RTM3004",
        "LAN / USB VISA",
        "64 位 VISA Runtime",
        "探头倍率 PROB:SET:ATT:MAN；导出 ASCII 电压及 XINC/XOR 时间轴。支持平均、采样/峰值/高分辨率设置；当前导出只接受每点一个值，峰值/包络双值数据会拒绝。分段开关需要 segmented_memory 选件，逐段时间戳导出待扩展。",
    ),
    (
        "rs.rto64",
        "oscilloscopes/rto64",
        "R&S RTO64",
        "LAN / USB VISA",
        "64 位 VISA Runtime",
        "使用 RTO6 手册；时间偏移 TIM:HOR:POS。导出前明确选择单通道数据格式，ASCII 波形按 HEAD 轴缩放。多通道按顺序读取；应先单次采集/前面板停止，避免跨帧数据，尚无硬件同步多设备采集。",
    ),
    (
        "lecroy.9254",
        "oscilloscopes/waverunner-9254",
        "Teledyne LeCroy WaveRunner 9254",
        "LAN VICP（1861）/ VISA VXI-11 或 USBTMC",
        "VICP 直接 TCP 不需要 VISA；其他路线需 VISA",
        "VICP 使用完整 8 字节头和 DATA/EOI 分段。WAVEDESC 提供字节序、单位、增益、偏移、有效点范围；当前仅接受单段波形。普通 TCP transport 不可代替 VICP。",
    ),
    (
        "rigol.ds1202ze",
        "oscilloscopes/rigol-ds1202z",
        "RIGOL DS1202Z（待确认后缀）/ DS1202Z-E",
        "DS1202Z-E：USB / LAN VISA",
        "64 位 VISA Runtime",
        "附件原型号 DS1202Z 保留待补信息；仅 DS1202Z-E 身份可匹配此实现。两通道；当前读取 NORM 屏幕波形，不宣称导出全部存储深度。BYTE 缩放包含 YORigin/YREFerence。",
    ),
    (
        "rs.fph",
        "spectrum-analyzers/rs-fph",
        "R&S FPH",
        "LAN / USB VISA（核对仪器当前接口配置）",
        "64 位 VISA Runtime",
        "使用 FPH 手册，轨迹为 TRAC1:DATA?，不能套用 DSA815 的 TRACE1 参数。频率上限保守为 2 GHz，RBW/VBW 使用保守范围；扩展频率、前置放大和测量应用选件需现场核对后扩展。",
    ),
    (
        "thorlabs.pm100d",
        "measurement-analyzers/thorlabs-pm100d-s154c",
        "Thorlabs PM100D + S154C",
        "PM100D USB；S154C 接控制台传感器接口",
        "Thorlabs OPM、匹配的 TLPMX 或旧版 PM100D 64 位 DLL/驱动",
        "两个后端分别调用带通道/不带通道 ABI。初始化 reset=0；不切换驱动、不复位。波长范围通过 SDK 查询当前探头；不把 S154C 当成控制台型号。归零需遮光后明确确认。",
    ),
    (
        "keysight.3458a",
        "measurement-analyzers/keysight-3458a",
        "Keysight / Agilent / HP 3458A",
        "GPIB + USB-GPIB/LAN-GPIB 控制器",
        "控制器驱动和 64 位 VISA Runtime",
        "使用原生 ID?/FUNC/NPLC/NRDGS/RMEM，不发送 *IDN?。HP 3458A/HP3458A 响应归一为型号；无序列号时不自动确认物理同一性。FUNC? 同时返回功能码和量程。读取 RMEM 会停止内部存储，故即使不 initiate 也需 --confirm。",
    ),
    (
        "rs.znb3004",
        "measurement-analyzers/rs-znb3004",
        "R&S ZNB3004",
        "LAN / USB VISA",
        "64 位 VISA Runtime",
        "使用 ZNB3000 手册。当前实现两端口逻辑 S11/S21/S12/S22；s_parameter 显式创建并选择 LabControl 轨迹。读取复数 SDAT 和 STIM 频率轴，保存修正状态；不执行校准向导。四条同频率轴轨迹才能导出 .s2p。",
    ),
    (
        "microchip.53100a",
        "measurement-analyzers/phase-noise-53100a",
        "53100A（Microchip 候选）",
        "仪器由已配置 TimeLab/TSERVE 服务控制；电脑连接其命令端口 1299 / 数据端口 1298",
        "厂家 TimeLab/TSERVE；本应用不安装或启动服务",
        "需现场确认厂家、型号、序列号和 TSERVE 控制权，并设置 identity_confirmed=true。频率公共单位 Hz，命令单位 MHz。相噪/AM/ADEV 经命令口读取；数据口仅接收已配置 P/F 格式，timestamp=0，stream_interval 与服务一致。",
    ),
]


def render(definitions=None) -> dict[Path, str]:
    definitions = DEVICE_DEFINITIONS if definitions is None else definitions
    models = [
        (
            d.adapter_id,
            d.documentation.card,
            d.documentation.title,
            d.documentation.connection,
            d.documentation.driver,
            "",
        )
        for d in definitions.values()
    ] + LEGACY_MODELS
    for field, label in ((0, "adapter ID"), (1, "card path")):
        values = [row[field] for row in models]
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate documentation {label}")
    for definition in definitions.values():
        for relative in ("README.md", "hardware/README.md"):
            if not (definition.root / relative).is_file():
                raise ValueError(f"Missing model documentation source: {definition.root / relative}")
    rows = []
    files: dict[Path, str] = {}
    fixtures = ROOT / "tests/fixtures/protocols"
    for adapter, card, title, connection, driver, note in models:
        config = ConnectionConfig("metadata://only", adapter)
        definition = definitions.get(adapter)
        profile = definition.profile if definition else PROFILES.get(adapter)
        caps = (
            ScpiAdapter(MockTransport([]), profile, config).capabilities() if definition else describe(config)
        )
        page = (
            ("实时监测" if caps.category in MEASUREMENT_CATEGORIES else "设备管理")
            + " / "
            + CATEGORY_LABELS[caps.category]
        )
        fixture = (
            definition.root / "tests/fixtures/control.json"
            if definition
            else fixtures / adapter / "control.json"
        )
        fixture_op = json.loads(fixture.read_text(encoding="utf-8"))["operation"] if fixture.exists() else ""
        record = {
            "adapter_id": adapter,
            "model": title,
            "category": caps.category,
            "control_page": page,
            "channels": caps.channels,
            "source": profile.source if profile else caps.operations[0].source,
            "revision": profile.revision if profile else "SDK headers/examples or 53100A DS50002991G",
            "acquisition": profile.acquisition
            if profile
            else {
                "pico.6404e": "block/streaming",
                "thorlabs.pm100d": "scalar",
                "microchip.53100a": "phase_noise/am_noise/stability/streaming",
            }[adapter],
            "channel_limits": profile.channel_limits if profile else {},
            "hardware_passed": False,
            "operations": [],
        }
        operations = []
        for cap in caps.operations:
            entry = jsonable(cap)
            entry.update(
                {
                    "implementation": "implemented",
                    "schema_tested": bool(profile),
                    "protocol_fixture_tested": cap.name == fixture_op,
                    "hardware_passed": False,
                    "ui_label": OPERATION_LABELS[cap.name],
                    "ui_location": page
                    + " / "
                    + ("状态与诊断" if not cap.writable else "常用" if cap.name in COMMON else "高级"),
                }
            )
            if profile:
                spec = profile.commands[cap.name]
                entry["wire_templates"] = {
                    "select": spec.select,
                    "writes": spec.writes,
                    "query": spec.query,
                    "custom_encoder": cap.writable and not bool(spec.writes),
                }
            record["operations"].append(entry)
            parameters = (
                "; ".join(
                    f"{name}: {p.kind} {p.unit} "
                    + ("/".join(p.choices) if p.choices else f"[{p.minimum}, {p.maximum}]")
                    for name, p in cap.parameters.items()
                )
                or "无"
            )
            state = "缺选件/配置" if not cap.available else "软件已实现；实测按资产查证"
            operations.append(
                f"| `{cap.name}` | {parameters} | {'通道' if cap.channel_scoped else '整机'} | {'读' if cap.readable else ''}{'/写' if cap.writable else ''} | {'需要' if cap.requires_confirmation else '无需'} | {state} |"
            )
        rows.append(record)
        source = record["source"]
        files[ROOT / f"docs/devices/{card}.md"] = f"""{LEGACY_CARD_MARKER}

# {title}

维护源：[旧型号文档元数据](../../../scripts/generate_device_docs.py)。本型号尚未迁入独立包，能力来自注册表；不要直接修改本页。

## 1. 型号与可信度

注册 ID：`{adapter}`。型号精确匹配；软件实现与具体资产的实测结果分别记录。附件简写和身份别名见本卡片说明。软件连接成功不等于验收通过。

## 2. 电脑物理连接

{connection}。接口和选件以设备背板、固件与现场盘点为准。

## 3. Windows 前置条件

{driver}。安装步骤见[实机测试指南](../../testing/hardware-testing.md)；DLL 放在厂商安装位置，通过绝对路径加载，不复制进源码。

## 4. 资源与枚举

连接模板见[config/examples](../../../config/examples/README.md)。VISA 使用扫描返回的完整资源；串口填写确认的 COM/波特率；Pico 使用 `PICO::序列号`；PM100D 使用 SDK 枚举的资源；53100A 手工填写已部署服务地址。扫描只枚举 SDK，不打开 SDK 仪器。

## 5. 官方命令/API 依据

[厂家资料]({source})；版本：{record["revision"]}。逐操作命令定位、参数和模板见[自动生成能力清单](../../generated/capability-matrix.json)。测试样本是按手册编写的夹具，尚非实机抓包。

## 6. 当前统一能力

逻辑类别 `{caps.category}`；通道 `{caps.channels}`。`None` 表示该侧未在通用参数层给出数值边界；适配器仍执行关联参数与通道校验。未列出的操作返回 `unsupported`。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
{chr(10).join(operations)}

## 7. 控制、读回与采集

页面入口：**{page}**。参数由当前注册能力生成；常用、高级、状态与诊断分组。只读采集能力通过监测页的单次/开始操作使用；缺选件项显示原因且禁用。详细操作见[控制与监测指南](../../guides/control-and-monitoring.md)。

采集路线：`{record["acquisition"] or "无测量输入；由外部测量仪器采集输出"}`。`apply` 返回成功、拒绝、部分执行或不确定；只有提供读回命令的操作才有硬件读回，不能将 SDK 已接收设置描述成测量值。`status` 显式读取，扫描和总览定时刷新不会额外采集。

{note}

## 8. 限制和故障处理

单设备单 worker 串行执行。数值范围是软件保守边界，仍需核对负载、频率、探头和接线条件。输出/触发等动作要求明确确认；超时不重发、不自动回滚，先诊断再重连。断开软件连接不表示输出关闭。错误数据使用质量标记，不能以零值代替。

## 9. Python 落点

通过 `ApplicationRuntime` → `SessionManager.submit()` → 注册表 `{adapter}` 调用；SCPI 型号共用所属类别的实现和独立 `Profile`，不创建空壳同名类。源码见 [instruments](../../../src/lab_control/instruments/)；示例见 [examples](../../../examples/README.md)。

## 10. Mock 与实机验收

软件：身份精确匹配、参数越界、未知能力和控制夹具；SDK 使用函数替身验证参数/句柄/缓冲区。完整结果以 `pytest` 报告为准，不能从该表推断全部高级命令已逐项验证。

现场按[统一验收步骤](../../testing/hardware-testing.md)记录完整身份、固件、驱动、资源、每个测试操作、请求、读回、原始错误、数据文件和恢复结果。按授权范围先只读，再控制/采集和恢复。`hardware_passed=false` 是保留的型号级兼容字段，不代表具体资产失败或尚未测试；逐项结论见运行目录摘要。
"""
        if definition:
            card_path = ROOT / f"docs/devices/{card}.md"
            readme = definition.root / "README.md"
            body = readme.read_text(encoding="utf-8")

            def rebase_link(match):
                label, target = match.groups()
                if "://" in target or target.startswith("#"):
                    return match.group(0)
                resolved = readme.parent / target
                relative = Path(os.path.relpath(resolved, card_path.parent)).as_posix()
                return f"[{label}]({relative})"

            body = re.sub(r"\[([^\]]+)\]\(([^\s)]+)\)", rebase_link, body)
            files[card_path] = (
                MODEL_CARD_MARKER
                + "\n\n维护源：[包内 README]("
                + Path(os.path.relpath(readme, card_path.parent)).as_posix()
                + ")。修改维护源后重新生成。\n\n"
                + body.rstrip()
                + "\n\n## 注册能力（自动生成）\n\n"
                + "范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。\n\n"
                + "| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |\n"
                + "| --- | --- | --- | --- | --- | --- |\n"
                + "\n".join(operations)
                + "\n"
            )
    matrix = {
        "schema_version": 1,
        "hardware_tested": False,
        "hardware_evidence_scope": "asset/interface/firmware/test-case records in runtime directory",
        "legacy_hardware_boolean_note": "Compatibility fields only; false is not an asset verdict.",
        "models": rows,
        "pending": PENDING,
    }
    files[ROOT / "docs/generated/capability-matrix.json"] = (
        json.dumps(jsonable(matrix), ensure_ascii=False, indent=2) + "\n"
    )
    lines = [
        "# 实际能力矩阵（自动生成）",
        "",
        "由 `scripts/generate_device_docs.py` 从实际注册表生成。下面的操作均有实现；单项参数、命令定位与软件测试标记见 [JSON](capability-matrix.json)。未列出的功能返回 `unsupported`；选件或 TSERVE 配置缺失会禁用相应能力。具体资产的实机证据保存在运行目录，不将个别资产的验证结果推广到整个型号。",
        "",
        "软件测试分为通用参数约束、特定协议夹具、SDK 生命周期和应用集成；协议夹具通过不代表目标固件通过，也不代表所有高级组合已经验证。",
        "",
        "JSON 保留 hardware_tested/hardware_passed 兼容字段；固定 false 不代表现场设备失败或全部未测。资产证据见[离线台账说明](../testing/records.md)。",
        "",
        "| 型号 | 注册 ID | 操作数 | 采集 | 详情 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for record, meta in zip(rows, models, strict=True):
        lines.append(
            f"| {record['model']} | `{record['adapter_id']}` | {len(record['operations'])} | {record['acquisition'] or '外部仪器测量输出'} | [型号卡片](../devices/{meta[1]}.md) |"
        )
    lines += [
        "",
        "## 未实现 / 待补项",
        "",
        "通用协议错误处理与已注册功能可进入实测。下列功能尚未作为真实能力开放，调用返回 `unsupported`，不能仅靠配置开启：",
        "",
        "- 函数发生器复杂序列编辑；SMA100B 未注册的选件专用功能（频率、扫频边界和功率范围已按实机查询适配）。",
        "- 电源斜率、触发链以及未核实型号的远端采样/列表（SPD4306X 已实现列表与 CH2/3 远端采样）。",
        "- 示波器分段逐段时间戳、多帧/数学波形导出；Pico 无间隙无限 streaming。",
        "- 频谱仪前置放大与带宽/信道功率应用；网络分析仪分段表写入及完整校准向导。",
        "- PM100D 校准证书导出；未知厂家设备真实协议。",
        "",
        "这些是后续能力扩展任务，不标记为硬件不具备。",
        "",
        "| 原始条目 | 状态 |",
        "| --- | --- |",
    ]
    lines += [f"| {name} | 待补信息：{reason} |" for name, reason in PENDING.items()]
    files[ROOT / "docs/generated/capability-matrix.md"] = "\n".join(lines) + "\n"
    cards = {
        f"docs/devices/{d.documentation.card}.md": hashlib.sha256(
            files[ROOT / f"docs/devices/{d.documentation.card}.md"].encode("utf-8")
        ).hexdigest()
        for d in definitions.values()
    }
    files[ROOT / "docs/generated/device-module-docs.json"] = json.dumps(cards, indent=2) + "\n"
    folders = dict(CATEGORY_FOLDERS)
    for _adapter, card, *_rest in models:
        folders.setdefault(str(Path(card).parent), str(Path(card).parent))
    device_navigation = [
        "# 设备型号导航（自动生成）",
        "",
        "独立型号的维护源在各自包内。旧型号暂保留原实现；未注册条目仅作为资料需求。实测结果按具体资产、接口和固件查看。",
        "",
        "[原始设备条目对应](../device-research-index.md) · [新设备接入流程](../testing/onboarding.md) · [能力矩阵](../generated/capability-matrix.md)",
        "",
    ]
    for folder, title in folders.items():
        navigation = [
            f"# {title}（自动生成）",
            "",
            "软件实现与资产实测分别判定；维护型号包后重新生成本导航。",
            "",
            "| 型号 | 接入路线 | 文档 |",
            "| --- | --- | --- |",
        ]
        owned = set()
        for _adapter, card, model_title, connection, _driver, _note in models:
            if str(Path(card).parent) == folder:
                owned.add(Path(card).name + ".md")
                navigation.append(f"| {model_title} | {connection} | [型号说明]({Path(card).name}.md) |")
        # Unregistered requirement cards are human-maintained; never delete or manufacture an adapter.
        for pending in sorted((ROOT / "docs/devices" / folder).glob("*.md")):
            text = pending.read_text(encoding="utf-8")
            if (
                pending.name == "README.md"
                or pending.name in owned
                or text.startswith((MODEL_CARD_MARKER, LEGACY_CARD_MARKER))
            ):
                continue
            name = next(
                (line.lstrip("# ") for line in text.splitlines() if line.startswith("# ")), pending.stem
            )
            navigation.append(f"| {name} | 未注册，待补资料 | [待补卡片]({pending.name}) |")
        files[ROOT / "docs/devices" / folder / "README.md"] = "\n".join(navigation) + "\n"
        device_navigation.append(f"- [{title}]({folder}/README.md)")
    files[ROOT / "docs/devices/README.md"] = "\n".join(device_navigation) + "\n"
    index_path = ROOT / "docs/device-research-index.md"
    index = index_path.read_text(encoding="utf-8")
    index_rows = [
        "| 型号 | 注册 ID | 连接路线 | 归档 |",
        "| --- | --- | --- | --- |",
        *(
            f"| {d.documentation.title} | `{d.adapter_id}` | {d.documentation.connection} | "
            f"[卡片](devices/{d.documentation.card}.md) |"
            for d in definitions.values()
        ),
    ]
    index, count = re.subn(
        r"<!-- device-modules:start -->.*?<!-- device-modules:end -->",
        "<!-- device-modules:start -->\n" + "\n".join(index_rows) + "\n<!-- device-modules:end -->",
        index,
        flags=re.DOTALL,
    )
    if count != 1:
        raise ValueError("Device research index requires one generated module section")
    files[index_path] = index
    return files


def obsolete_cards(files: dict[Path, str]) -> list[Path]:
    """Only remove our recorded, unmodified generated cards under docs/devices."""
    manifest = ROOT / "docs/generated/device-module-docs.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    current = json.loads(files[manifest])
    stale = []
    for name in previous.keys() - current.keys():
        path = (ROOT / name).resolve()
        if not path.is_relative_to((ROOT / "docs/devices").resolve()) or path.suffix != ".md":
            raise ValueError(f"Unsafe generated document path: {name}")
        if path.exists():
            contents = path.read_text(encoding="utf-8")
            if (
                not contents.startswith(MODEL_CARD_MARKER)
                or hashlib.sha256(contents.encode("utf-8")).hexdigest() != previous[name]
            ):
                raise ValueError(f"Removed model card was manually modified; preserve it: {name}")
            stale.append(path)
    return stale


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = render()
    stale = obsolete_cards(files)
    changed = []
    for path in stale:
        changed.append(str(path.relative_to(ROOT)))
        if not args.check:
            path.unlink()
    for path, content in files.items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
    if args.check and changed:
        print("Outdated generated documents: " + ", ".join(changed))
        return 1
    print(f"Generated documentation checked ({len(changed)} changed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
