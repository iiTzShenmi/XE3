import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from agent.core.config import agent_db_path, legacy_agent_db_path


_DB_LOCAL = threading.local()


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    path = agent_db_path()
    legacy_path = legacy_agent_db_path()
    if not path.exists() and legacy_path.exists():
        legacy_path.replace(path)
    return str(path)


def _create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _thread_connection() -> sqlite3.Connection:
    conn: Optional[sqlite3.Connection] = getattr(_DB_LOCAL, "conn", None)
    if conn is None:
        conn = _create_connection()
        _DB_LOCAL.conn = conn
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _thread_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              line_user_id TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS e3_accounts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL UNIQUE,
              e3_account TEXT NOT NULL,
              encrypted_password TEXT,
              last_login_at TEXT,
              login_status TEXT NOT NULL DEFAULT 'unknown',
              last_error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events_cache (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              source TEXT NOT NULL DEFAULT 'e3',
              event_uid TEXT NOT NULL,
              event_type TEXT NOT NULL,
              course_id TEXT,
              course_name TEXT,
              title TEXT NOT NULL,
              due_at TEXT,
              payload_json TEXT,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active',
              UNIQUE(user_id, event_uid),
              FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        if not _has_column(conn, "events_cache", "course_name"):
            conn.execute("ALTER TABLE events_cache ADD COLUMN course_name TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_prefs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL UNIQUE,
              enabled INTEGER NOT NULL DEFAULT 1,
              timezone TEXT NOT NULL DEFAULT 'Asia/Taipei',
              schedule_json TEXT NOT NULL DEFAULT '["09:00","21:00"]',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              event_uid TEXT,
              notification_type TEXT NOT NULL,
              sent_at TEXT NOT NULL,
              result TEXT NOT NULL,
              details TEXT,
              FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS grade_items_cache (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              course_id TEXT NOT NULL,
              course_name TEXT,
              item_name TEXT NOT NULL,
              score TEXT NOT NULL,
              first_seen_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(user_id, course_id, item_name),
              FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_delivery_targets (
              user_id INTEGER PRIMARY KEY,
              channel_id TEXT,
              guild_id TEXT,
              updated_at TEXT NOT NULL,
              FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )


def upsert_user(line_user_id: str) -> int:
    now = _utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (line_user_id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(line_user_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (line_user_id, now, now),
        )
        row = conn.execute("SELECT id FROM users WHERE line_user_id=?", (line_user_id,)).fetchone()
        return row["id"]


def get_user_id(line_user_id: str) -> int | None:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE line_user_id=?", (line_user_id,)).fetchone()
        return row["id"] if row else None


def get_line_user_id_by_user_id(user_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT line_user_id FROM users WHERE id=?", (user_id,)).fetchone()
        return row["line_user_id"] if row else None


def upsert_discord_delivery_target(line_user_id: str, channel_id: str | None, guild_id: str | None = None) -> None:
    user_id = upsert_user(line_user_id)
    now = _utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO discord_delivery_targets (user_id, channel_id, guild_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              channel_id=excluded.channel_id,
              guild_id=excluded.guild_id,
              updated_at=excluded.updated_at
            """,
            (user_id, str(channel_id or "").strip() or None, str(guild_id or "").strip() or None, now),
        )


def get_discord_delivery_target(line_user_id: str):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT discord_delivery_targets.channel_id, discord_delivery_targets.guild_id, discord_delivery_targets.updated_at
            FROM users
            JOIN discord_delivery_targets ON discord_delivery_targets.user_id = users.id
            WHERE users.line_user_id=?
            LIMIT 1
            """,
            (line_user_id,),
        ).fetchone()


def upsert_e3_account(
    user_id: int,
    e3_account: str,
    encrypted_password: str,
    status: str = "ok",
    error: str | None = None,
) -> None:
    now = _utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO e3_accounts (
              user_id, e3_account, encrypted_password, last_login_at, login_status, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              e3_account=excluded.e3_account,
              encrypted_password=excluded.encrypted_password,
              last_login_at=excluded.last_login_at,
              login_status=excluded.login_status,
              last_error=excluded.last_error,
              updated_at=excluded.updated_at
            """,
            (user_id, e3_account, encrypted_password, now, status, error, now, now),
        )


def update_login_state(user_id: int, status: str, error: str | None = None) -> None:
    now = _utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE e3_accounts
            SET login_status=?, last_error=?, updated_at=?
            WHERE user_id=?
            """,
            (status, error, now, user_id),
        )


def get_e3_account_by_user_id(user_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT e3_account, encrypted_password, login_status, last_error, last_login_at FROM e3_accounts WHERE user_id=?",
            (user_id,),
        ).fetchone()


def delete_user_data(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM discord_delivery_targets WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM events_cache WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM grade_items_cache WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM reminder_prefs WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM notification_log WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM e3_accounts WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def upsert_event(
    user_id: int,
    event_uid: str,
    event_type: str,
    course_id: str | None,
    course_name: str | None,
    title: str,
    due_at: str | None,
    payload_json: str,
) -> None:
    now = _utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO events_cache (
              user_id, source, event_uid, event_type, course_id, course_name, title, due_at, payload_json,
              first_seen_at, last_seen_at, status
            ) VALUES (?, 'e3', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(user_id, event_uid) DO UPDATE SET
              event_type=excluded.event_type,
              course_id=excluded.course_id,
              course_name=excluded.course_name,
              title=excluded.title,
              due_at=excluded.due_at,
              payload_json=excluded.payload_json,
              last_seen_at=excluded.last_seen_at,
              status='active'
            """,
            (user_id, event_uid, event_type, course_id, course_name, title, due_at, payload_json, now, now),
        )


def mark_missing_events_inactive(user_id: int, active_event_uids: list[str]) -> None:
    with get_conn() as conn:
        if active_event_uids:
            placeholders = ",".join("?" for _ in active_event_uids)
            conn.execute(
                f"""
                UPDATE events_cache
                SET status='inactive'
                WHERE user_id=? AND source='e3' AND event_uid NOT IN ({placeholders})
                """,
                (user_id, *active_event_uids),
            )
            return
        conn.execute(
            """
            UPDATE events_cache
            SET status='inactive'
            WHERE user_id=? AND source='e3'
            """,
            (user_id,),
        )


def get_upcoming_events(user_id: int, limit: int = 10):
    now = _utc_now_iso()
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT event_uid, event_type, course_id, course_name, title, due_at
            FROM events_cache
            WHERE user_id=? AND status='active' AND due_at IS NOT NULL AND due_at >= ?
            ORDER BY due_at ASC
            LIMIT ?
            """,
            (user_id, now, limit),
        ).fetchall()


def get_timeline_events(user_id: int, limit: int = 30):
    now = _utc_now_iso()
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT event_uid, event_type, course_id, course_name, title, due_at
            FROM events_cache
            WHERE user_id=? AND status='active' AND due_at IS NOT NULL AND due_at >= ?
            ORDER BY due_at ASC
            LIMIT ?
            """,
            (user_id, now, limit),
        ).fetchall()


def get_timeline_event_detail(user_id: int, offset: int = 0):
    now = _utc_now_iso()
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT event_uid, event_type, course_id, course_name, title, due_at, payload_json
            FROM events_cache
            WHERE user_id=? AND status='active' AND due_at IS NOT NULL AND due_at >= ?
            ORDER BY due_at ASC
            LIMIT 1 OFFSET ?
            """,
            (user_id, now, offset),
        ).fetchone()


def get_timeline_event_details(user_id: int, limit: int = 50):
    now = _utc_now_iso()
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT event_uid, event_type, course_id, course_name, title, due_at, payload_json
            FROM events_cache
            WHERE user_id=? AND status='active' AND due_at IS NOT NULL AND due_at >= ?
            ORDER BY due_at ASC
            LIMIT ?
            """,
            (user_id, now, limit),
        ).fetchall()


def get_event_by_uid(user_id: int, event_uid: str):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT event_uid, event_type, course_id, course_name, title, due_at, payload_json
            FROM events_cache
            WHERE user_id=? AND event_uid=? AND status='active'
            LIMIT 1
            """,
            (user_id, event_uid),
        ).fetchone()


def ensure_reminder_prefs(user_id: int):
    now = _utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO reminder_prefs (user_id, enabled, timezone, schedule_json, created_at, updated_at)
            VALUES (?, 1, 'Asia/Taipei', '["09:00","21:00"]', ?, ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, now, now),
        )
        return conn.execute(
            "SELECT enabled, timezone, schedule_json, updated_at FROM reminder_prefs WHERE user_id=?",
            (user_id,),
        ).fetchone()


def get_reminder_prefs(user_id: int):
    prefs = ensure_reminder_prefs(user_id)
    return prefs


def get_reminder_prefs_by_line_user_id(line_user_id: str):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT
              users.id AS user_id,
              users.line_user_id AS line_user_id,
              reminder_prefs.enabled AS enabled,
              reminder_prefs.timezone AS timezone,
              reminder_prefs.schedule_json AS schedule_json,
              reminder_prefs.updated_at AS updated_at
            FROM users
            JOIN reminder_prefs ON reminder_prefs.user_id = users.id
            WHERE users.line_user_id=?
            """,
            (line_user_id,),
        ).fetchone()


def update_reminder_enabled(user_id: int, enabled: bool) -> None:
    now = _utc_now_iso()
    ensure_reminder_prefs(user_id)
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE reminder_prefs
            SET enabled=?, updated_at=?
            WHERE user_id=?
            """,
            (1 if enabled else 0, now, user_id),
        )


def update_reminder_schedule(user_id: int, schedule_list: list[str]) -> None:
    now = _utc_now_iso()
    ensure_reminder_prefs(user_id)
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE reminder_prefs
            SET schedule_json=?, updated_at=?
            WHERE user_id=?
            """,
            (json.dumps(schedule_list, ensure_ascii=False), now, user_id),
        )


def get_grade_items(user_id: int):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT course_id, course_name, item_name, score
            FROM grade_items_cache
            WHERE user_id=?
            ORDER BY course_name ASC, item_name ASC
            """,
            (user_id,),
        ).fetchall()


def upsert_grade_item(user_id: int, course_id: str, course_name: str, item_name: str, score: str) -> None:
    now = _utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO grade_items_cache (
              user_id, course_id, course_name, item_name, score, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, course_id, item_name) DO UPDATE SET
              course_name=excluded.course_name,
              score=excluded.score,
              updated_at=excluded.updated_at
            """,
            (user_id, course_id, course_name, item_name, score, now, now),
        )


def get_events_due_between(user_id: int, start_iso: str, end_iso: str, limit: int = 10):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT event_uid, event_type, course_id, course_name, title, due_at
            FROM events_cache
            WHERE user_id=? AND status='active' AND due_at IS NOT NULL
              AND due_at >= ? AND due_at <= ?
            ORDER BY due_at ASC
            LIMIT ?
            """,
            (user_id, start_iso, end_iso, limit),
        ).fetchall()


def list_reminder_targets():
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT
              users.id AS user_id,
              users.line_user_id AS line_user_id,
              e3_accounts.login_status AS login_status,
              e3_accounts.last_error AS last_error,
              e3_accounts.updated_at AS account_updated_at,
              reminder_prefs.enabled AS enabled,
              reminder_prefs.timezone AS timezone,
              reminder_prefs.schedule_json AS schedule_json
            FROM users
            JOIN e3_accounts ON e3_accounts.user_id = users.id
            JOIN reminder_prefs ON reminder_prefs.user_id = users.id
            WHERE reminder_prefs.enabled = 1
            """
        ).fetchall()


def list_sync_targets():
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT
              users.id AS user_id,
              users.line_user_id AS line_user_id,
              e3_accounts.login_status AS login_status,
              e3_accounts.last_error AS last_error
            FROM users
            JOIN e3_accounts ON e3_accounts.user_id = users.id
            WHERE e3_accounts.encrypted_password IS NOT NULL
            ORDER BY users.id ASC
            """
        ).fetchall()


def notification_sent(user_id: int, notification_type: str, details: str | None = None) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM notification_log
            WHERE user_id=? AND notification_type=? AND details=?
            LIMIT 1
            """,
            (user_id, notification_type, details),
        ).fetchone()
        return bool(row)


def notification_succeeded(user_id: int, notification_type: str, details: str | None = None) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM notification_log
            WHERE user_id=? AND notification_type=? AND details=? AND result='sent'
            LIMIT 1
            """,
            (user_id, notification_type, details),
        ).fetchone()
        return bool(row)


def log_notification(
    user_id: int,
    notification_type: str,
    result: str,
    details: str | None = None,
    event_uid: str | None = None,
) -> None:
    now = _utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notification_log (user_id, event_uid, notification_type, sent_at, result, details)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, event_uid, notification_type, now, result, details),
        )
