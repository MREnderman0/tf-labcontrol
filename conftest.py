"""Shared software-test setup for public and model-owned test trees."""

import gc
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    # pytest-qt schedules deleteLater(); drain it after all fixture finalizers,
    # before the next test replaces styles or creates another model workspace.
    had_widgets = bool(getattr(item, "qt_widgets", ()))
    result = yield
    if had_widgets:
        from PySide6.QtCore import QCoreApplication, QEvent

        if QCoreApplication.instance() is not None:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            gc.collect()
    return result
