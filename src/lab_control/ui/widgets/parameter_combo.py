"""Compatibility entry point for parameter and unit choices."""

from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


class ParameterComboBox(WorkspaceComboBox):
    """Parameter editors share the application-wide popup and wheel policy."""
