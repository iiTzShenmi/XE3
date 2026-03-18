from concurrent.futures import ThreadPoolExecutor
import threading
import time
from typing import Any, Callable

from agent.features.e3 import handle_e3_command, run_e3_async_command


BACKGROUND_COMMAND_DEDUPE_SECONDS = 8
_BACKGROUND_COMMANDS = {}
_BACKGROUND_LOCK = threading.Lock()
_HEAVY_TASK_SEMAPHORE = threading.Semaphore(2)
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="line-bg")


def build_processing_ack(text: str) -> str:
    lowered = text.strip().lower()
    label = "E3 指令"
    if "timeline" in lowered or "近期" in text or "行事曆" in text:
        label = "時間軸"
    elif "course" in lowered or "課程" in text:
        label = "課程"
    elif "detail" in lowered or "詳情" in text:
        label = "事件詳情"
    elif lowered.startswith("e3 login"):
        label = "登入"
    elif lowered in {"e3 relogin", "e3 重新登入"}:
        label = "重新登入"
    return f"⏳ 已收到{label}指令，正在處理中，完成後會再推播結果給你。"


def is_async_e3_command(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized.startswith("e3 login") or normalized in {"e3 relogin", "e3 重新登入"}


def is_deferred_read_e3_command(text: str) -> bool:
    normalized = text.strip().lower()
    prefixes = (
        "e3 course",
        "e3 課程",
        "e3 timeline",
        "e3 行事曆",
        "e3 近期",
        "e3 grades",
        "e3 成績",
        "e3 files",
        "e3 檔案",
        "e3 detail",
        "e3 詳情",
    )
    return normalized.startswith(prefixes)


def is_background_e3_command(text: str) -> bool:
    return is_async_e3_command(text) or is_deferred_read_e3_command(text)


def _background_command_key(line_user_id: str | None, text: str) -> str:
    normalized = " ".join(str(text or "").strip().lower().split())
    return f"{line_user_id or '-'}::{normalized}"


def _cleanup_background_commands(now=None):
    now = now or time.time()
    expired = []
    with _BACKGROUND_LOCK:
        for key, item in _BACKGROUND_COMMANDS.items():
            started_at = item.get("started_at", 0)
            if now - started_at > 300:
                expired.append(key)
        for key in expired:
            _BACKGROUND_COMMANDS.pop(key, None)


def register_background_command(line_user_id: str | None, text: str):
    now = time.time()
    _cleanup_background_commands(now)
    key = _background_command_key(line_user_id, text)
    with _BACKGROUND_LOCK:
        existing = _BACKGROUND_COMMANDS.get(key)
        if existing and now - existing["started_at"] < BACKGROUND_COMMAND_DEDUPE_SECONDS:
            return False, existing
        item = {"started_at": now, "text": text}
        _BACKGROUND_COMMANDS[key] = item
        return True, item


def finish_background_command(line_user_id: str | None, text: str) -> None:
    key = _background_command_key(line_user_id, text)
    with _BACKGROUND_LOCK:
        _BACKGROUND_COMMANDS.pop(key, None)


def start_e3_background_task(text: str, line_user_id: str | None, logger, push_fn: Callable[[str, Any], bool]) -> None:
    if not line_user_id:
        logger.warning("skip_async_e3 reason=missing_line_user_id")
        return

    _EXECUTOR.submit(_run_e3_background_task, text, line_user_id, logger, push_fn)


def _run_e3_background_task(text: str, line_user_id: str, logger, push_fn: Callable[[str, Any], bool]) -> None:
    try:
        logger.info("e3_background_started user=%s text=%s", line_user_id, text)
        with _HEAVY_TASK_SEMAPHORE:
            if is_async_e3_command(text):
                result = run_e3_async_command(text, logger, line_user_id)
            else:
                result = handle_e3_command(text, logger, line_user_id)
    except Exception as exc:
        logger.exception("e3_background_task_failed user=%s", line_user_id)
        result = f"E3 背景作業失敗：{exc}"
    finally:
        finish_background_command(line_user_id, text)

    logger.info("e3_background_completed user=%s text=%s", line_user_id, text)
    pushed = push_fn(line_user_id, result)
    logger.info("e3_background_pushed user=%s text=%s ok=%s", line_user_id, text, pushed)
