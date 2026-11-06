"""Shared software-test setup for public and model-owned test trees."""

import gc
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # Only hand pytest a fresh directory we own: --basetemp is cleared by pytest.
    # Resolve from the checkout, never the current drive or a developer's machine.
    if config.option.basetemp is None and not config.option.collectonly:
        root = Path(__file__).resolve().parent / ".temp"
        root.mkdir(exist_ok=True)
        config.option.basetemp = tempfile.mkdtemp(prefix="p", dir=root)


@pytest.fixture
def tmp_path(request, tmp_path_factory):
    # Short, unique leaves avoid Windows path limits in nested processing exports.
    path = tmp_path_factory.mktemp("t")
    request.node.add_report_section("setup", "temporary directory", str(path))
    return path


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
