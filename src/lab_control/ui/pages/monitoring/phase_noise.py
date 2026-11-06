"""Compatibility import; finite phase capture now lives in device management."""

from lab_control.ui.pages.device_control.phase_noise import LogAxis, PhaseNoisePage

PhaseNoiseMonitoringPage = PhaseNoisePage

__all__ = ["LogAxis", "PhaseNoiseMonitoringPage"]
