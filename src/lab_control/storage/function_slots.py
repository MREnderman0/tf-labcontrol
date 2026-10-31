"""Persistent, workspace-scoped logical slots; built-ins seed an untouched category."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lab_control.domain.workspace import GENERAL, category_slots, slot_label

if TYPE_CHECKING:
    from lab_control.storage.repository import Repository


class FunctionSlots:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    @staticmethod
    def key(category: str, mode: str) -> str:
        return f"function_slots/{mode}/{category}"

    def definitions(self, category: str, mode: str = GENERAL) -> dict[str, str]:
        saved = self.repository.load_workspace(self.key(category, mode))
        if saved is None:
            return {slot: slot_label(slot) for slot in category_slots(category)}
        return dict(saved)

    def label(self, slot: str, mode: str = GENERAL) -> str:
        if not slot:
            return "未绑定"
        category = slot.partition(":")[0]
        return self.definitions(category, mode).get(slot, slot_label(slot))

    def save(self, category: str, mode: str, definitions: dict[str, str]) -> None:
        # An explicitly empty dictionary must not restore the built-in slots.
        self.repository.save_workspace(self.key(category, mode), definitions)
