"""Durable request deduplication and artifact references; never stores curve arrays."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from lab_control.domain.instruments import DeviceError, jsonable


class AgentLedger:
    def __init__(self, repository) -> None:
        self.repository = repository
        with repository.lock, repository.db:
            repository.db.execute(
                "CREATE TABLE IF NOT EXISTS agent_tasks "
                "(id TEXT PRIMARY KEY, client TEXT NOT NULL, request TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, document TEXT NOT NULL, UNIQUE(client, request))"
            )
            repository.db.execute(
                "CREATE TABLE IF NOT EXISTS agent_artifacts (id TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )
        # A new process cannot know whether a previously accepted command reached
        # the instrument. Keep its idempotency key and evidence, never replay it.
        for task in self.tasks(active_only=True):
            task.update(
                state="interrupted", error="软件已重启；上次操作最终状态需核对", code="outcome_unknown"
            )
            self.update(task)

    @staticmethod
    def fingerprint(value: Any) -> str:
        payload = json.dumps(jsonable(value), sort_keys=True, ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    def find_request(self, client: str, request: str, fingerprint: str) -> dict | None:
        with self.repository.lock:
            row = self.repository.db.execute(
                "SELECT fingerprint, document FROM agent_tasks WHERE client=? AND request=?",
                (client, request),
            ).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise DeviceError("request_id 已用于不同请求", "request_conflict")
        return json.loads(row[1])

    def insert(self, task: dict, fingerprint: str) -> None:
        with self.repository.lock, self.repository.db:
            self.repository.db.execute(
                "INSERT INTO agent_tasks VALUES (?, ?, ?, ?, ?)",
                (
                    task["id"],
                    task["client_id"],
                    task["request_id"],
                    fingerprint,
                    json.dumps(jsonable(task), ensure_ascii=False, allow_nan=False),
                ),
            )

    def update(self, task: dict) -> None:
        with self.repository.lock, self.repository.db:
            self.repository.db.execute(
                "UPDATE agent_tasks SET document=? WHERE id=?",
                (json.dumps(jsonable(task), ensure_ascii=False, allow_nan=False), task["id"]),
            )

    def get(self, task_id: str) -> dict:
        with self.repository.lock:
            row = self.repository.db.execute(
                "SELECT document FROM agent_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise DeviceError("任务不存在", "task_not_found")
        return json.loads(row[0])

    def tasks(self, *, active_only: bool = False) -> list[dict]:
        with self.repository.lock:
            # The restart pass must see all active rows, including older requests.
            query = "SELECT document FROM agent_tasks ORDER BY rowid DESC"
            rows = self.repository.db.execute(query + ("" if active_only else " LIMIT 200")).fetchall()
        values = [json.loads(row[0]) for row in rows]
        return [v for v in values if v["state"] in ACTIVE] if active_only else values

    def save_artifact(self, value: dict) -> None:
        with self.repository.lock, self.repository.db:
            self.repository.db.execute(
                "INSERT OR REPLACE INTO agent_artifacts VALUES (?, ?)",
                (value["id"], json.dumps(value, ensure_ascii=False)),
            )

    def artifacts(self, task_id: str = "") -> list[dict]:
        with self.repository.lock:
            rows = self.repository.db.execute(
                "SELECT document FROM agent_artifacts ORDER BY rowid DESC"
            ).fetchall()
        values = [json.loads(row[0]) for row in rows]
        return [v for v in values if not task_id or v["task_id"] == task_id]

    def artifact(self, artifact_id: str) -> tuple[dict, Path]:
        with self.repository.lock:
            row = self.repository.db.execute(
                "SELECT document FROM agent_artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise DeviceError("产物不存在", "artifact_not_found")
        value = json.loads(row[0])
        path, root = Path(value["path"]).resolve(), Path(value["root"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise DeviceError("产物已移动或路径不在登记目录内", "artifact_unavailable")
        # A registered regular file must not turn into a link to a different file.
        if str(path) != value["path"]:
            raise DeviceError("产物路径已变化", "artifact_unavailable")
        return value, path


ACTIVE = frozenset({"accepted", "running", "stopping", "binary_saved", "converting"})
