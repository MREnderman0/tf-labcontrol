import pytest

from lab_control.instruments.devices.kplusk_fxe.reports import ReportReader


@pytest.fixture
def fake_clock(monkeypatch):
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    original = ReportReader.__init__

    def initialize(self, *args):
        original(self, *args)
        self.clock, self.sleep = clock, clock.sleep

    monkeypatch.setattr(ReportReader, "__init__", initialize)
    return clock
