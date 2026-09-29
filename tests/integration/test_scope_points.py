from lab_control.domain.instruments import Parameter
from lab_control.ui.pages.monitoring.scope_points import ScopePoints


def test_points_choices_bounds_refresh_and_custom_cancel(qtbot, monkeypatch):
    box = ScopePoints()
    qtbot.addWidget(box)
    writes = []
    box.committed.connect(writes.append)
    box.configure(2500, Parameter("integer", minimum=1000, maximum=25000))
    assert {box.itemData(i) for i in range(box.count())} == {1000, 2500, 10000, 25000, "custom"}
    assert box.currentData() == 2500 and not writes
    monkeypatch.setattr(box, "custom_value", lambda: (1234, False))
    box.choose(box.findData("custom"))
    assert not writes and box.currentData() == 2500
    monkeypatch.setattr(box, "custom_value", lambda: (1234, True))
    box.choose(box.findData("custom"))
    assert writes == [1234]
    box.configure(1234, Parameter("integer", minimum=1000, maximum=1100))
    assert box.currentData() == 1234 and "超出范围" in box.currentText()
    assert writes == [1234]


def test_discrete_points_never_invent_choices(qtbot):
    box = ScopePoints()
    qtbot.addWidget(box)
    box.configure(
        1000, Parameter("integer", minimum=1000, maximum=100000, choices=("1000", "10000", "100000"))
    )
    assert [box.itemData(i) for i in range(box.count())] == [1000, 10000, 100000]
