from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError


@pytest.mark.parametrize("mode", ["default", "readonly", "wrong_serial", "wrong_firmware", "failed"])
def test_optional_preparation_is_after_identity_and_never_replayed(tmp_path, mode):
    config = ConnectionConfig("mock://binary-preparation", "rigol.dsa815", "mock")
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        calls = []
        factory = app.sessions.factory

        def make(candidate):
            adapter = factory(config)  # Identity is independent of the requested serial guard.

            def prepare():
                calls.append("prepare")
                adapter.acquisition_preparation = {"status": "failed" if mode == "failed" else "ready"}
                if mode == "failed":
                    raise DeviceError("Initialization timeout", "timeout")

            adapter.prepare_acquisition = prepare
            return adapter

        app.sessions.factory = make
        key = app.add(config)
        selected = replace(config, expected_serial="DIFFERENT") if mode == "wrong_serial" else config
        future = app.sessions.connect(
            key,
            selected,
            configure_acquisition=mode != "readonly",
            expected_firmware="DIFFERENT" if mode == "wrong_firmware" else "",
        )
        if mode in {"wrong_serial", "wrong_firmware", "failed"}:
            with pytest.raises(DeviceError):
                future.result(3)
            assert app.sessions.snapshots()[0]["connectionState"] == "error"
        else:
            future.result(3)
            assert app.sessions.snapshots()[0]["connectionState"] == "connected"
        assert calls == (["prepare"] if mode in {"default", "failed"} else [])
        if calls:
            assert app.sessions.snapshots()[0]["acquisitionPreparation"]["status"] == (
                "failed" if mode == "failed" else "ready"
            )
