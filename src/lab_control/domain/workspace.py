"""Workspace labels retained in public APIs and historical recording metadata."""

GENERAL = "general"
LEGACY_MODES = ("electronic", "optical")
WORKSPACE_MODES = (GENERAL, *LEGACY_MODES)

CATEGORY_LABELS = {
    "signal_generator": "信号源",
    "power_supply": "电源",
    "oscilloscope": "示波器",
    "spectrum_analyzer": "频谱仪",
    "optical_power_meter": "光功率计",
    "multimeter": "万用表",
    "frequency_counter": "频率计数器",
    "network_analyzer": "网络分析仪",
    "phase_noise_analyzer": "相噪仪",
    "temperature_controller": "温控仪",
    "electronic_load": "电子负载",
    "pump_driver": "泵浦驱动",
    "optical_spectrometer": "光谱仪",
}


def slot_label(slot: str) -> str:
    """Use the same logical slot names in the UI and saved measurement records."""
    category, _, index = slot.partition(":")
    if category == "optical_power_meter" and index:
        return f"功率计{index}"
    suffix = {"1": "A", "2": "B"}.get(index, index)
    return f"{CATEGORY_LABELS.get(category, category)} {suffix}" if index else "未绑定"


def category_slots(category: str) -> tuple[str, ...]:
    """Stable logical slot IDs shared by validation, migration and presentation."""
    count = 4 if category == "optical_power_meter" else 2
    return tuple(f"{category}:{index}" for index in range(1, count + 1))
