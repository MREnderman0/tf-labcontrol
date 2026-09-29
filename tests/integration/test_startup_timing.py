from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.pages.monitoring.timing import MonitoringTiming, monitoring_options


@pytest.mark.parametrize("adapter", ["lecroy.9404m", "rigol.dho924", "rigol.dsa815", "rs.fph"])
@pytest.mark.parametrize(
    "previous", [{"cadence": "fastest", "interval": 0.5}, {"cadence": "interval", "interval": 8}]
)
def test_each_launch_resets_but_rebinding_and_modes_preserve_current_choices(
    tmp_path, qtbot, adapter, previous
):
    config = ConnectionConfig("mock://startup", adapter, "mock")
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = runtime.add(config)
        pref = f"monitor-timing/electronic/{key}"
        runtime.repository.save_workspace(pref, previous)
    for _ in range(2):
        with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
            controller = SimpleNamespace(
                runtime=runtime, records={r["id"]: r for r in runtime.inventory.all()}
            )
            options = monitoring_options(controller, key, "electronic")
            assert options["cadence"] == "interval" and options["interval"] == 0.5
            widget = MonitoringTiming(controller)
            qtbot.addWidget(widget)
            widget.bind(key, "electronic")
            widget.interval.setValue(1.25)
            widget.cadence.setCurrentIndex(widget.cadence.findData("fastest"))
            widget.commit()
            widget.bind("", "electronic")
            widget.bind(key, "electronic")
            assert widget.options()["cadence"] == "fastest"
            assert widget.options()["interval"] == 1.25
            assert monitoring_options(controller, key, "optical")["interval"] == 0.5
            assert not runtime.sessions.sessions and not runtime.monitoring.tasks
            widget.deleteLater()
