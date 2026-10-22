from __future__ import annotations

from typing import Any

from lab_control.application.catalog import instrument_category, mode_applicable

CATEGORIES = {
    "signal_generator": "信号源",
    "power_supply": "电源",
    "oscilloscope": "示波器",
    "spectrum_analyzer": "频谱仪",
    "temperature_controller": "温控设备",
    "optical_power_meter": "光学测量",
    "measurement_analyzers": "测量分析",
    "load_drivers": "负载 / 泵浦",
    "unknown": "未分类 / 未知设备",
}
CATEGORY_DESCRIPTIONS = {
    "signal_generator": "频率、幅度和输出状态",
    "power_supply": "电压、电流与通道状态",
    "oscilloscope": "波形、触发和采样配置",
    "spectrum_analyzer": "频率范围、RBW 与轨迹",
    "temperature_controller": "温度读回与稳定状态",
    "optical_power_meter": "光功率、光谱与传感器",
    "measurement_analyzers": "万用表、频率计、网络分析与相位噪声",
    "load_drivers": "电子负载与泵浦驱动",
    "unknown": "待确认身份与设备协议",
}


def category_for(record: dict[str, Any]) -> str:
    category = instrument_category(record)
    if category in {"multimeter", "network_analyzer", "phase_noise_analyzer", "frequency_counter"}:
        return "measurement_analyzers"
    if category in {"electronic_load", "pump_driver"}:
        return "load_drivers"
    if category == "optical_spectrometer":
        return "optical_power_meter"
    return category


def applicable(record: dict[str, Any], mode: str) -> bool:
    return mode_applicable(record, mode)
