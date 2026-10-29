"""Presentation-neutral control and data-source descriptions; no manufacturer commands."""

from __future__ import annotations

from dataclasses import dataclass, replace

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, CapabilitySet
from lab_control.domain.workspace import CATEGORY_LABELS as CATEGORY_LABELS
from lab_control.domain.workspace import slot_label as slot_label
from lab_control.instruments.registry import DEVICE_DEFINITIONS, PROFILES, describe

MEASUREMENT_CATEGORIES = {
    "oscilloscope",
    "spectrum_analyzer",
    "optical_power_meter",
    "multimeter",
    "frequency_counter",
    "network_analyzer",
    "phase_noise_analyzer",
    "optical_spectrometer",
}
OPERATION_LABELS = dict(
    line.split("=", 1)
    for line in """
waveform=波形
control_state=控制状态
standard_waveform=标准波形
waveform_stop=停止标准波形
normal_mode=常规模式
frequency=频率
gate_time=闸门时间
input_impedance=输入阻抗
input_range=输入量程
auto_level=自动触发电平
input_slope=输入触发斜率
input_filter=100 kHz 低通滤波
probe_factor=探头倍率
input_protection=输入保护状态
amplitude=幅度
offset=偏移
amplitude_unit=幅度单位
phase=相位
pulse_duty=脉冲占空比
duty=占空比
pulse_width=脉冲宽度
ramp_symmetry=斜坡对称性
output=通道输出
load=输出负载
burst=突发输出
burst_mode=突发模式
burst_cycles=突发周期数
burst_period=突发重复周期
burst_delay=突发延时
burst_phase=突发起始相位
sweep=扫频状态
sweep_time=扫描时间
sweep_time_auto=自动扫描时间
sweep_points=扫描点数
points_auto=自动采样点数
center_wavelength=中心波长
wavelength_span=波长跨度
sensitivity=灵敏度
repeat=启动重复扫描
stop=停止扫描
axis_mode=横轴模式
trace_points=轨迹实际点数
active_trace=当前轨迹
wavelength_medium=波长介质
wavelength_offset=波长偏置
marker_scan_range=标记限制扫描范围
sweep_count=单次扫描次数
sweep_current=已完成扫描次数
transfer_format=轨迹传输格式
options=已安装选件
byte_order=传输字节序
response_header=响应头格式
waveform_setup=波形传输设置
sample_rate=实际采样率
channel_unit=通道单位
vernier=垂直细调
trigger_type=触发类型
trigger_sweep=触发运行方式
trigger_holdoff=触发保持时间
acquisition_type=采集方式
timebase_mode=时基模式
roll_enabled=自动滚动
xy_enabled=XY 模式
delay_enabled=延迟时基
transfer_mode=波形传输范围
transfer_source=波形传输源
transfer_start=传输起始点
transfer_stop=传输结束点
transfer_points=传输窗口点数
trigger_setup=触发配置
sequence_setup=分段采集配置
interleaved=随机交织采样
measurement_mode=测量模式
power_config=控制器配置
raw_channel_data=原始通道数据
report=原始协议报告
firmware_version=固件版本
user_id=K+K 用户号
operation_condition=扫描运行状态
frequency_offset=频率显示偏移
reference_offset=参考电平偏移
preamplifier=前置放大
mixer_level=混频器最大电平
marker_position=标记位置
marker_mode=标记模式
marker_readout=标记坐标类型
marker_trace=标记对应轨迹
marker_trace_auto=自动选择标记轨迹
marker_function=标记测量功能
marker_tracking=连续峰值跟踪
marker_search=峰值搜索模式
sweep_spacing=扫描间隔方式
sweep_direction=扫频方向
sweep_start=扫频起始频率
sweep_stop=扫频终止频率
sweep_trigger=扫频触发源
sweep_trigger_now=立即触发扫频
burst_trigger=突发触发源
burst_trigger_now=立即触发突发
trigger=触发
reference_clock=参考时钟
am_enabled=AM 调制
am_source=AM 调制源
am_frequency=AM 调制频率
am_shape=AM 调制波形
fm_enabled=FM 调制
fm_source=FM 调制源
fm_frequency=FM 调制频率
fm_shape=FM 调制波形
pm_enabled=PM 调制
pm_source=PM 调制源
pm_frequency=PM 调制频率
pm_shape=PM 调制波形
am_depth=AM 深度
fm_deviation=FM 频偏
pm_deviation=PM 相偏
frequency_mode=频率模式
trigger_source=触发源
phase_sync=相位同步
upload_waveform=上传任意波形
select_uploaded_waveform=选择已上传波形
load_waveform=加载任意波形
arb_enabled=任意波形输出模式
arb_sample_rate=任意波形采样率
modulation=调制开关
modulation_type=调制类型
power=功率
display_power=显示功率
wavelength=波长
sampling_rate=采样率
display_wavelengths=显示波长表
pdl=偏振相关损耗
sweep_dwell=扫描驻留时间
sweep_mode=扫描运行方式
sweep_step_linear=线性扫频步进
sweep_step_log=对数扫频步进
pulse_enabled=脉冲调制
list_mode=列表模式
list_trigger_source=列表触发源
list_dwell=列表驻留时间
list_trigger=触发列表
upload_list=上传频率 / 功率列表
voltage=电压设定
current=电流设定
measured_voltage=实测电压
measured_current=实测电流
measured_power=实测功率
all_outputs=整机输出
tracking=跟踪 / 串并联
status_register=状态寄存器
remote=进入远程控制
local=返回面板控制
errors=设备错误
trigger_interval=触发间隔
scan_list=扫描通道列表
trigger_count=触发次数
scan_size=扫描通道数
dmm_installed=DMM 模块已安装
dmm_state=DMM 状态
monitor_state=监视状态
configuration=通道配置
measurements=测量读数
system_version=系统版本
slot_100=插槽 100 模块
slot_200=插槽 200 模块
slot_300=插槽 300 模块
slot_400=插槽 400 模块
slot_500=插槽 500 模块
voltage_limit=电压上限
voltage_limit_enabled=电压上限开关
timer=定时输出
timer_delay=定时时间
voltage_step=电压步进
current_step=电流步进
coupling=耦合方式
protection=保护状态
ovp=过压保护阈值
ocp=过流保护阈值
ovp_enabled=过压保护开关
ocp_enabled=过流保护开关
ocp_delay=过流保护延迟
output_on_delay=输出开启延迟
output_off_delay=输出关闭延迟
list_cycles=列表重复次数
list_continuous=列表连续运行
list_run=运行列表
sense_mode=远端采样模式
ovp_tripped=过压跳闸状态
ocp_tripped=过流跳闸状态
list_upload=上传电源列表
channel=通道配置
timebase=时基
sample_interval=采样间隔
block=Block 采集
streaming=Streaming 采集
channel_enabled=通道显示 / 启用
scale=垂直量程
probe_attenuation=探头倍率
trigger_level=触发电平
time_position=水平位置
trigger_mode=触发模式
trigger_slope=触发沿
record_length=记录长度
acquisition_count=采集次数
run=运行仪器
stop=停止仪器
single=仪器单次采集
waveform_header=波形头信息
acquisition_mode=采集模式
averages=平均次数
decimation=抽取 / 高分辨率
bandwidth_limit=带宽限制
segmented=分段采集模式
invert=波形反相
force_trigger=强制触发
trigger_status=触发状态
start_frequency=起始频率
stop_frequency=终止频率
center_frequency=中心频率
span=频率跨度
rbw=分辨率带宽 RBW
vbw=视频带宽 VBW
rbw_auto=自动 RBW
vbw_auto=自动 VBW
reference_level=参考电平
auto_setup=自动设置
attenuation=输入衰减
attenuation_auto=自动衰减
continuous=仪器连续扫描
marker_enabled=标记开关
marker_frequency=标记频率
marker_frequency_mode=标记坐标模式
marker_peak=搜索峰值
marker_value=标记读数
trace_mode=轨迹模式
detector=检波器
detector_auto=自动检波器
wavelength=波长
autorange=自动量程
range=量程
averaging_time=平均时间
unit=单位
zero=归零 / 暗电流校正
sensor=传感器信息
function=测量功能
temperature_sensor=温度传感器类型
thermistor_type=热敏电阻标称阻值（25℃）
temperature_unit=温度单位
integration=积分时间
resolution=分辨率
autozero=自动调零
delay=测量延迟
trigger_arm=触发预备
memory=内部存储模式
output_format=读数格式
function_status=当前测量功能
integration_status=当前积分时间
temperature=温度
sample_count=采样数量
points=扫描点数
if_bandwidth=中频带宽
averaging=平均开关
sweep_type=扫描类型
port_power=端口功率
correction_status=校准修正状态
trace_catalog=轨迹目录
s_parameter=S 参数
selected_trace=当前轨迹
start=开始仪器测量
input_frequency=输入频率
reference_frequency=参考频率
phase_noise=相位噪声
am_noise=AM 噪声
stability=稳定度
timed_phase=定时相位采集与稳定度
data_stream=相位 / 频率数据流
setpoint=温度设定值
ramp=温度斜坡
pid_p=PID 比例系数
start_wavelength=起始波长
stop_wavelength=终止波长
""".strip().splitlines()
)
PARAMETER_LABELS = {
    "value": "目标值",
    "enabled": "启用",
    "range": "量程",
    "coupling": "耦合",
    "offset": "偏移",
    "threshold": "阈值",
    "direction": "触发沿",
    "timeout_us": "自动触发等待 / µs",
    "name": "文件 / 列表名称",
    "samples": "归一化样点",
    "frequencies": "频率 / Hz",
    "powers": "功率 / dBm",
    "voltage": "电压 / V",
    "current": "电流 / A",
    "duration": "时长 / s",
}
COMMON = {
    "waveform",
    "frequency",
    "amplitude",
    "offset",
    "phase",
    "power",
    "output",
    "all_outputs",
    "voltage",
    "current",
    "channel",
    "channel_enabled",
    "scale",
    "coupling",
    "probe_attenuation",
    "timebase",
    "time_position",
    "trigger",
    "trigger_level",
    "trigger_source",
    "trigger_mode",
    "trigger_slope",
    "run",
    "stop",
    "single",
    "start",
    "start_frequency",
    "stop_frequency",
    "center_frequency",
    "span",
    "rbw",
    "vbw",
    "reference_level",
    "attenuation",
    "wavelength",
    "function",
    "temperature_sensor",
    "thermistor_type",
    "temperature_unit",
    "scan_list",
    "range",
    "autorange",
    "integration",
    "points",
    "if_bandwidth",
    "s_parameter",
    "setpoint",
    "temperature",
    "input_frequency",
    "reference_frequency",
}


def control_capabilities(
    config: ConnectionConfig, channel: int = 1, schema: CapabilitySet | None = None
) -> CapabilitySet:
    schema = schema or describe(config, channel)
    limits = (
        dict(PROFILES[config.adapter].channel_limits.get(channel, {})) if config.adapter in PROFILES else {}
    )
    for key, limit in config.limits.items():
        limits[key] = min(limit, limits.get(key, limit))
    operations = []
    for cap in schema.operations:
        value = cap.parameters.get("value")
        if cap.name in limits and value:
            maximum = min(limits[cap.name], value.maximum) if value.maximum is not None else limits[cap.name]
            source = "experiment" if cap.name in config.limits else "model_channel"
            cap = replace(
                cap,
                parameters={**cap.parameters, "value": replace(value, maximum=maximum, range_source=source)},
            )
        operations.append(cap)
    return replace(schema, operations=tuple(operations))


@dataclass(frozen=True)
class DataSource:
    key: str
    label: str
    kind: str
    quantity: str = ""
    measurement: str = ""
    view: str = "curve"

    def request(
        self,
        channels: tuple[int, ...],
        points: int | None = None,
        initiate: bool = False,
        confirmed: bool = False,
    ) -> AcquisitionRequest:
        return AcquisitionRequest(
            self.kind,
            channels,
            points if points is not None else (2000 if self.kind not in {"scalar", "status"} else 1),
            self.quantity,
            initiate,
            confirmed,
        )


def data_sources(config: ConnectionConfig) -> tuple[DataSource, ...]:
    schema = describe(config)
    category = schema.category
    if category == "signal_generator":
        return tuple(
            DataSource(name, OPERATION_LABELS[name] + "读回", "status", name, name, "number")
            for name in ("frequency", "amplitude", "power", "output")
            if any(c.name == name and c.available for c in schema.operations)
        )
    if category == "power_supply":
        return (
            DataSource("voltage", "电压", "scalar", measurement="voltage"),
            DataSource("current", "电流", "scalar", measurement="current"),
        )
    if category == "oscilloscope":
        sources = [DataSource("waveform", "时域波形", "waveform")]
        definition = DEVICE_DEFINITIONS.get(config.adapter)
        if definition and "streaming" in definition.acquisition_kinds:
            sources.append(DataSource("streaming", "Streaming 波形", "streaming"))
        return tuple(sources)
    if category == "spectrum_analyzer":
        return (DataSource("spectrum", "频谱 / 零跨度轨迹", "spectrum"),)
    if category == "network_analyzer":
        return (
            DataSource("magnitude", "当前 S 参数幅度", "s_parameters", view="magnitude"),
            DataSource("phase", "当前 S 参数相位", "s_parameters", view="phase"),
        )
    if category == "phase_noise_analyzer":
        result = [
            DataSource(name, OPERATION_LABELS[name], "scalar", name)
            for name in ("phase_noise", "am_noise", "stability")
        ]
        if any(c.name == "data_stream" and c.available for c in schema.operations):
            result.append(DataSource("data_stream", "相位 / 频率流", "streaming"))
        return tuple(result)
    if category == "frequency_counter":
        return (DataSource("frequency", "频率", "scalar", "frequency", "frequency"),)
    if category == "optical_spectrometer":
        label = "光谱曲线（模拟）" if config.transport == "mock" else "光谱曲线"
        return (DataSource("spectrum", label, "spectrum"),)
    return (
        DataSource(
            "reading",
            {
                "optical_power_meter": "光功率",
                "multimeter": "测量读数",
                "temperature_controller": "温度",
                "electronic_load": "负载电流",
                "pump_driver": "泵浦电流",
            }.get(category, "读数"),
            "scalar",
            quantity="power" if category == "optical_power_meter" else "",
            measurement="power" if category == "optical_power_meter" else "",
        ),
    )
