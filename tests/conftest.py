import gc
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    # pytest-qt closes widgets with deleteLater(), but processEvents() alone
    # does not drain DeferredDelete. Finish fixture shutdown first, then delete
    # on the GUI thread before another test replaces the application style.
    had_widgets = bool(getattr(item, "qt_widgets", ()))
    result = yield
    if had_widgets:
        from PySide6.QtCore import QCoreApplication, QEvent

        if QCoreApplication.instance() is not None:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            gc.collect()
    return result
