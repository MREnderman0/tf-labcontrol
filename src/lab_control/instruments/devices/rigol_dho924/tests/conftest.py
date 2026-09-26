"""Model-only UI runs use the same headless Qt platform as the project suite."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
