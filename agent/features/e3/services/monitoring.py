from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.core.config import e3_data_root


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def sync_status_path(workspace: str | Path) -> Path:
    return Path(workspace) / "sync_status.json"


def read_sync_status(workspace: str | Path) -> dict[str, Any]:
    return read_json_object(sync_status_path(workspace))


def write_sync_status(workspace: str | Path, payload: dict[str, Any]) -> None:
    write_json_atomic(sync_status_path(workspace), payload)


def reminder_worker_status_path() -> Path:
    return e3_data_root() / "reminder_worker_status.json"


def read_reminder_worker_status() -> dict[str, Any]:
    return read_json_object(reminder_worker_status_path())


def write_reminder_worker_status(payload: dict[str, Any]) -> None:
    write_json_atomic(reminder_worker_status_path(), payload)
