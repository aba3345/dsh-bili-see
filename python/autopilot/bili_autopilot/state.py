from __future__ import annotations

from typing import Any

from .config import RunPaths
from .util import atomic_write_json, now_iso, read_json


TERMINAL = {"completed", "failed", "cancelled", "needs_continue"}
ACTIVE = {"starting", "running", "resuming"}


class StateStore:
    def __init__(self, paths: RunPaths):
        self.paths = paths

    def load(self) -> dict[str, Any]:
        data = read_json(self.paths.state, {})
        return data if isinstance(data, dict) else {}

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        state["updated_at"] = now_iso()
        atomic_write_json(self.paths.state, state)
        return state

    def update(self, **changes: Any) -> dict[str, Any]:
        state = self.load()
        state.update(changes)
        return self.save(state)

    def create(self, run_id: str, config: dict[str, Any], *, status: str = "starting") -> dict[str, Any]:
        state = {
            "run_id": run_id,
            "status": status,
            "created_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "updated_at": now_iso(),
            "pid": None,
            "phase": "queued",
            "steps": 0,
            "continues": 0,
            "videos_seen": 0,
            "comments_posted": 0,
            "partial": False,
            "last_error": None,
            "current_bvid": None,
            "model": config.get("model"),
            "write": bool(config.get("write")),
            "config": config,
            "result": {},
        }
        return self.save(state)

    def set_running(self, pid: int | None = None) -> dict[str, Any]:
        return self.update(
            status="running",
            phase="starting",
            started_at=now_iso(),
            pid=pid,
            last_error=None,
        )

    def finish(
        self,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        partial: bool = False,
    ) -> dict[str, Any]:
        return self.update(
            status=status,
            phase="done" if status != "needs_continue" else "paused",
            finished_at=now_iso(),
            result=result or {},
            last_error=error,
            partial=bool(partial),
        )

    def request_cancel(self) -> None:
        self.paths.cancel_flag.write_text(now_iso() + "\n", encoding="utf-8")

    def cancel_requested(self) -> bool:
        return self.paths.cancel_flag.exists()

    def clear_cancel(self) -> None:
        try:
            self.paths.cancel_flag.unlink()
        except FileNotFoundError:
            pass
