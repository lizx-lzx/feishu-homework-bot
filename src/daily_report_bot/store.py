from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import AttendanceRecord, StoredMessage


class LocalStore:
    """持久化群消息与汇总投递状态。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS group_messages (
                    message_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    sender_open_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    create_time_ms INTEGER NOT NULL,
                    parent_id TEXT NOT NULL DEFAULT '',
                    root_id TEXT NOT NULL DEFAULT '',
                    thread_id TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_group_messages_chat_time
                    ON group_messages(chat_id, create_time_ms);
                CREATE TABLE IF NOT EXISTS known_chats (
                    chat_id TEXT PRIMARY KEY,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sent_welcome_guides (
                    chat_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL DEFAULT '',
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sent_summaries (
                    report_date TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (report_date, chat_id)
                );
                CREATE TABLE IF NOT EXISTS daily_attendance (
                    report_date TEXT NOT NULL,
                    member_key TEXT NOT NULL,
                    sender_open_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    assignment_label TEXT NOT NULL,
                    homework_status TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    homework_source TEXT NOT NULL,
                    homework_message_ids TEXT NOT NULL DEFAULT '[]',
                    review_message_ids TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (report_date, member_key)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_attendance_date_status
                    ON daily_attendance(report_date, homework_status, review_status);
                CREATE TABLE IF NOT EXISTS sent_reminders (
                    report_date TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (report_date, chat_id)
                );
                CREATE TABLE IF NOT EXISTS sent_missing_lists (
                    report_date TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (report_date, chat_id)
                );
                CREATE TABLE IF NOT EXISTS sent_final_statuses (
                    report_date TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (report_date, chat_id)
                );
                CREATE TABLE IF NOT EXISTS sent_makeup_reminders (
                    report_date TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (report_date, chat_id)
                );
                CREATE TABLE IF NOT EXISTS sent_makeup_summaries (
                    report_date TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (report_date, chat_id)
                );
                CREATE TABLE IF NOT EXISTS base_sync_records (
                    record_key TEXT PRIMARY KEY,
                    record_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS iteration_events (
                    message_id TEXT PRIMARY KEY,
                    report_date TEXT NOT NULL,
                    assignment_label TEXT NOT NULL,
                    member_key TEXT NOT NULL,
                    member_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor_open_id TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    event_time_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_iteration_member_date
                    ON iteration_events(report_date, member_key, event_time_ms);
                CREATE TABLE IF NOT EXISTS homework_verifications (
                    report_date TEXT NOT NULL,
                    member_key TEXT NOT NULL,
                    sender_open_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    claim_message_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_message_ids TEXT NOT NULL DEFAULT '[]',
                    evidence_time_ms INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (report_date, member_key)
                );
                CREATE INDEX IF NOT EXISTS idx_homework_verifications_date
                    ON homework_verifications(report_date, evidence_time_ms);
                CREATE TABLE IF NOT EXISTS attendance_overrides (
                    message_id TEXT PRIMARY KEY,
                    report_date TEXT NOT NULL,
                    member_key TEXT NOT NULL,
                    member_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor_open_id TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    event_time_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_attendance_overrides_date_member
                    ON attendance_overrides(report_date, member_key, event_time_ms);
                CREATE TABLE IF NOT EXISTS homework_reactions (
                    message_id TEXT PRIMARY KEY,
                    report_date TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    emoji_type TEXT NOT NULL,
                    reaction_id TEXT NOT NULL DEFAULT '',
                    reacted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS social_chat_actions (
                    message_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    response TEXT NOT NULL DEFAULT '',
                    outbound_message_id TEXT NOT NULL DEFAULT '',
                    event_time_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_social_chat_actions_chat_time
                    ON social_chat_actions(chat_id, event_time_ms);
                """
            )
            existing_columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(group_messages)")
            }
            for column in ("parent_id", "root_id", "thread_id"):
                if column not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE group_messages ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_group_messages_thread "
                "ON group_messages(chat_id, thread_id, create_time_ms)"
            )

    def add_message(self, message: StoredMessage) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO group_messages(
                    message_id, chat_id, sender_open_id, sender_name,
                    message_type, content, create_time_ms, parent_id, root_id, thread_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.chat_id,
                    message.sender_open_id,
                    message.sender_name,
                    message.message_type,
                    message.content,
                    message.create_time_ms,
                    message.parent_id,
                    message.root_id,
                    message.thread_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO known_chats(chat_id) VALUES (?)
                ON CONFLICT(chat_id) DO UPDATE SET last_seen_at = CURRENT_TIMESTAMP
                """,
                (message.chat_id,),
            )
            return cursor.rowcount == 1

    def list_messages(
        self, chat_id: str, start_ms: int, end_ms: int, limit: int
    ) -> List[StoredMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, chat_id, sender_open_id, sender_name,
                       message_type, content, create_time_ms,
                       parent_id, root_id, thread_id
                FROM group_messages
                WHERE chat_id = ? AND create_time_ms >= ? AND create_time_ms < ?
                ORDER BY create_time_ms ASC
                LIMIT ?
                """,
                (chat_id, start_ms, end_ms, limit),
            ).fetchall()
        return [StoredMessage(**dict(row)) for row in rows]

    def list_recent_messages(
        self,
        chat_id: str,
        through_ms: int,
        limit: int = 12,
    ) -> List[StoredMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, chat_id, sender_open_id, sender_name,
                       message_type, content, create_time_ms,
                       parent_id, root_id, thread_id
                FROM group_messages
                WHERE chat_id = ? AND create_time_ms <= ?
                ORDER BY create_time_ms DESC
                LIMIT ?
                """,
                (chat_id, through_ms, limit),
            ).fetchall()
        return [StoredMessage(**dict(row)) for row in reversed(rows)]

    def thread_roots(self, chat_id: str, thread_ids: Sequence[str]) -> Dict[str, StoredMessage]:
        """返回话题的根消息，用于把跨日回复归到话题发布的那次作业。"""
        values = tuple(sorted({value for value in thread_ids if value}))
        if not values:
            return {}
        placeholders = ",".join("?" for _ in values)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT message_id, chat_id, sender_open_id, sender_name,
                       message_type, content, create_time_ms,
                       parent_id, root_id, thread_id
                FROM group_messages
                WHERE chat_id = ? AND thread_id IN ({placeholders})
                ORDER BY
                    CASE WHEN parent_id = '' AND root_id = '' THEN 0 ELSE 1 END,
                    create_time_ms ASC
                """,
                (chat_id, *values),
            ).fetchall()
        roots: Dict[str, StoredMessage] = {}
        for row in rows:
            message = StoredMessage(**dict(row))
            roots.setdefault(message.thread_id, message)
        return roots

    def list_chats(self) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT chat_id FROM known_chats ORDER BY chat_id").fetchall()
        return [str(row["chat_id"]) for row in rows]

    def chat_known(self, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM known_chats WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return row is not None

    def welcome_guide_sent(self, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_welcome_guides WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return row is not None

    def mark_welcome_guide_sent(self, chat_id: str, message_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sent_welcome_guides(chat_id, message_id)
                VALUES (?, ?)
                """,
                (chat_id, message_id),
            )

    def summary_sent(self, report_date: str, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_summaries WHERE report_date = ? AND chat_id = ?",
                (report_date, chat_id),
            ).fetchone()
        return row is not None

    def mark_summary_sent(self, report_date: str, chat_id: str, message_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sent_summaries(report_date, chat_id, message_id)
                VALUES (?, ?, ?)
                """,
                (report_date, chat_id, message_id),
            )

    def reminder_sent(self, report_date: str, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_reminders WHERE report_date = ? AND chat_id = ?",
                (report_date, chat_id),
            ).fetchone()
        return row is not None

    def mark_reminder_sent(self, report_date: str, chat_id: str, message_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sent_reminders(report_date, chat_id, message_id)
                VALUES (?, ?, ?)
                """,
                (report_date, chat_id, message_id),
            )

    def missing_list_sent(self, report_date: str, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_missing_lists WHERE report_date = ? AND chat_id = ?",
                (report_date, chat_id),
            ).fetchone()
        return row is not None

    def mark_missing_list_sent(self, report_date: str, chat_id: str, message_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sent_missing_lists(report_date, chat_id, message_id)
                VALUES (?, ?, ?)
                """,
                (report_date, chat_id, message_id),
            )

    def final_status_sent(self, report_date: str, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_final_statuses WHERE report_date = ? AND chat_id = ?",
                (report_date, chat_id),
            ).fetchone()
        return row is not None

    def mark_final_status_sent(self, report_date: str, chat_id: str, message_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sent_final_statuses(
                    report_date, chat_id, message_id
                ) VALUES (?, ?, ?)
                """,
                (report_date, chat_id, message_id),
            )

    def makeup_reminder_sent(self, report_date: str, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_makeup_reminders WHERE report_date = ? AND chat_id = ?",
                (report_date, chat_id),
            ).fetchone()
        return row is not None

    def mark_makeup_reminder_sent(self, report_date: str, chat_id: str, message_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sent_makeup_reminders(
                    report_date, chat_id, message_id
                ) VALUES (?, ?, ?)
                """,
                (report_date, chat_id, message_id),
            )

    def makeup_summary_sent(self, report_date: str, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_makeup_summaries WHERE report_date = ? AND chat_id = ?",
                (report_date, chat_id),
            ).fetchone()
        return row is not None

    def mark_makeup_summary_sent(self, report_date: str, chat_id: str, message_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sent_makeup_summaries(
                    report_date, chat_id, message_id
                ) VALUES (?, ?, ?)
                """,
                (report_date, chat_id, message_id),
            )

    def replace_daily_attendance(self, records: List[AttendanceRecord]) -> None:
        if not records:
            return
        report_dates = {record.report_date for record in records}
        if len(report_dates) != 1:
            raise ValueError("一次只能写入同一天的打卡记录")
        report_date = records[0].report_date
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM daily_attendance WHERE report_date = ?", (report_date,))
            conn.executemany(
                """
                INSERT INTO daily_attendance(
                    report_date, member_key, sender_open_id, sender_name,
                    assignment_label, homework_status, review_status, homework_source,
                    homework_message_ids, review_message_ids, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                [
                    (
                        record.report_date,
                        record.member_key,
                        record.sender_open_id,
                        record.sender_name,
                        record.assignment_label,
                        record.homework_status,
                        record.review_status,
                        record.homework_source,
                        json.dumps(record.homework_message_ids, ensure_ascii=False),
                        json.dumps(record.review_message_ids, ensure_ascii=False),
                    )
                    for record in records
                ],
            )

    def delete_attendance_date(self, report_date: str) -> int:
        """删除不再代表独立作业的旧日期记录及其 Base 同步状态。"""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM daily_attendance WHERE report_date = ?", (report_date,)
            )
            conn.execute(
                "DELETE FROM base_sync_records WHERE record_key LIKE ?",
                (f"{report_date}|%",),
            )
            return cursor.rowcount

    def list_daily_attendance(self, report_date: str) -> List[AttendanceRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT report_date, member_key, sender_open_id, sender_name,
                       assignment_label, homework_status, review_status, homework_source,
                       homework_message_ids, review_message_ids
                FROM daily_attendance
                WHERE report_date = ?
                ORDER BY rowid
                """,
                (report_date,),
            ).fetchall()
        return [
            AttendanceRecord(
                report_date=str(row["report_date"]),
                member_key=str(row["member_key"]),
                sender_open_id=str(row["sender_open_id"]),
                sender_name=str(row["sender_name"]),
                assignment_label=str(row["assignment_label"]),
                homework_status=str(row["homework_status"]),
                review_status=str(row["review_status"]),
                homework_source=str(row["homework_source"]),
                homework_message_ids=tuple(json.loads(row["homework_message_ids"])),
                review_message_ids=tuple(json.loads(row["review_message_ids"])),
            )
            for row in rows
        ]

    def list_member_attendance(
        self, member_key: str, member_name: str, through_date: str
    ) -> List[AttendanceRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT report_date, member_key, sender_open_id, sender_name,
                       assignment_label, homework_status, review_status, homework_source,
                       homework_message_ids, review_message_ids
                FROM daily_attendance
                WHERE (member_key = ? OR sender_name = ?) AND report_date <= ?
                ORDER BY report_date ASC
                """,
                (member_key, member_name, through_date),
            ).fetchall()
        return [
            AttendanceRecord(
                report_date=str(row["report_date"]),
                member_key=str(row["member_key"]),
                sender_open_id=str(row["sender_open_id"]),
                sender_name=str(row["sender_name"]),
                assignment_label=str(row["assignment_label"]),
                homework_status=str(row["homework_status"]),
                review_status=str(row["review_status"]),
                homework_source=str(row["homework_source"]),
                homework_message_ids=tuple(json.loads(row["homework_message_ids"])),
                review_message_ids=tuple(json.loads(row["review_message_ids"])),
            )
            for row in rows
        ]

    def attendance_totals(self, member_key: str, through_date: str) -> Tuple[int, int, int]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN homework_status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN homework_status = 'late' THEN 1 ELSE 0 END) AS late,
                    SUM(CASE WHEN homework_status = 'missing' THEN 1 ELSE 0 END) AS missing
                FROM daily_attendance
                WHERE member_key = ? AND report_date <= ?
                """,
                (member_key, through_date),
            ).fetchone()
        return (
            int(row["completed"] or 0),
            int(row["late"] or 0),
            int(row["missing"] or 0),
        )

    def message_time_ms(self, message_ids: Sequence[str]) -> int:
        ids = [value for value in message_ids if value]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT MIN(create_time_ms) AS value FROM group_messages WHERE message_id IN ({placeholders})",
                ids,
            ).fetchone()
        return int(row["value"] or 0)

    def save_homework_verification(
        self,
        *,
        report_date: str,
        member_key: str,
        sender_open_id: str,
        sender_name: str,
        claim_message_id: str,
        status: str,
        evidence_message_ids: Sequence[str],
        evidence_time_ms: int,
    ) -> None:
        """保存本人补交核验结果。

        同一成员、同一次作业只保留一条审核记录；重复发起核验时用
        最新核验结果覆盖，但始终保留实际证据消息 ID 与时间。
        """
        if status not in {"completed", "late"}:
            raise ValueError("作业核验状态只能是 completed 或 late")
        evidence_ids = tuple(dict.fromkeys(value for value in evidence_message_ids if value))
        if not evidence_ids or evidence_time_ms <= 0:
            raise ValueError("作业核验必须包含真实证据消息和时间")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO homework_verifications(
                    report_date, member_key, sender_open_id, sender_name,
                    claim_message_id, status, evidence_message_ids, evidence_time_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_date, member_key) DO UPDATE SET
                    sender_open_id = excluded.sender_open_id,
                    sender_name = excluded.sender_name,
                    claim_message_id = excluded.claim_message_id,
                    status = excluded.status,
                    evidence_message_ids = excluded.evidence_message_ids,
                    evidence_time_ms = excluded.evidence_time_ms,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    report_date,
                    member_key,
                    sender_open_id,
                    sender_name,
                    claim_message_id,
                    status,
                    json.dumps(evidence_ids, ensure_ascii=False),
                    evidence_time_ms,
                ),
            )

    def list_homework_verifications(self, report_date: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT report_date, member_key, sender_open_id, sender_name,
                       claim_message_id, status, evidence_message_ids, evidence_time_ms
                FROM homework_verifications
                WHERE report_date = ?
                ORDER BY evidence_time_ms ASC
                """,
                (report_date,),
            ).fetchall()
        return [
            {
                **dict(row),
                "evidence_message_ids": tuple(json.loads(row["evidence_message_ids"])),
            }
            for row in rows
        ]

    def add_attendance_override(
        self,
        *,
        message_id: str,
        report_date: str,
        member_key: str,
        member_name: str,
        status: str,
        actor_open_id: str,
        actor_name: str,
        event_time_ms: int,
    ) -> bool:
        """记录组长通过群命令修改作业状态的审计事件。"""
        if status not in {"completed", "late", "missing"}:
            raise ValueError("组长代改状态只能是 completed、late 或 missing")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO attendance_overrides(
                    message_id, report_date, member_key, member_name, status,
                    actor_open_id, actor_name, event_time_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    report_date,
                    member_key,
                    member_name,
                    status,
                    actor_open_id,
                    actor_name,
                    event_time_ms,
                ),
            )
        return cursor.rowcount == 1

    def list_attendance_overrides(self, report_date: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, report_date, member_key, member_name, status,
                       actor_open_id, actor_name, event_time_ms
                FROM attendance_overrides
                WHERE report_date = ?
                ORDER BY event_time_ms ASC
                """,
                (report_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def homework_reaction_sent(self, message_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM homework_reactions WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return row is not None

    def mark_homework_reaction_sent(
        self,
        *,
        message_id: str,
        report_date: str,
        chat_id: str,
        emoji_type: str,
        reaction_id: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO homework_reactions(
                    message_id, report_date, chat_id, emoji_type, reaction_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (message_id, report_date, chat_id, emoji_type, reaction_id),
            )

    def social_chat_action_sent(self, message_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM social_chat_actions WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return row is not None

    def social_chat_action_count(self, chat_id: str, since_ms: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM social_chat_actions
                WHERE chat_id = ? AND event_time_ms >= ?
                """,
                (chat_id, since_ms),
            ).fetchone()
        return int(row["total"] or 0)

    def social_chat_parent_known(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM social_chat_actions WHERE outbound_message_id = ?",
                (message_id,),
            ).fetchone()
        return row is not None

    def last_social_chat_action_ms(self, chat_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(event_time_ms) AS latest
                FROM social_chat_actions
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        return int(row["latest"] or 0)

    def list_recent_social_chat_actions(
        self,
        chat_id: str,
        through_ms: int,
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT action, response, event_time_ms
                FROM social_chat_actions
                WHERE chat_id = ? AND event_time_ms <= ?
                ORDER BY event_time_ms DESC
                LIMIT ?
                """,
                (chat_id, through_ms, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def mark_social_chat_action(
        self,
        *,
        message_id: str,
        chat_id: str,
        action: str,
        response: str,
        outbound_message_id: str,
        event_time_ms: int,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO social_chat_actions(
                    message_id, chat_id, action, response,
                    outbound_message_id, event_time_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    chat_id,
                    action,
                    response,
                    outbound_message_id,
                    event_time_ms,
                ),
            )

    def base_sync_state(self, record_key: str) -> Optional[Tuple[str, str]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT record_id, payload_hash FROM base_sync_records WHERE record_key = ?",
                (record_key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["record_id"]), str(row["payload_hash"])

    def save_base_sync_state(self, record_key: str, record_id: str, payload_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO base_sync_records(record_key, record_id, payload_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(record_key) DO UPDATE SET
                    record_id = excluded.record_id,
                    payload_hash = excluded.payload_hash,
                    synced_at = CURRENT_TIMESTAMP
                """,
                (record_key, record_id, payload_hash),
            )

    def add_iteration_event(
        self,
        *,
        message_id: str,
        report_date: str,
        assignment_label: str,
        member_key: str,
        member_name: str,
        status: str,
        actor_open_id: str,
        actor_name: str,
        event_time_ms: int,
    ) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO iteration_events(
                    message_id, report_date, assignment_label, member_key, member_name,
                    status, actor_open_id, actor_name, event_time_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    report_date,
                    assignment_label,
                    member_key,
                    member_name,
                    status,
                    actor_open_id,
                    actor_name,
                    event_time_ms,
                ),
            )
        return cursor.rowcount == 1

    def latest_iteration(self, report_date: str, member_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT assignment_label, status, actor_name, event_time_ms
                FROM iteration_events
                WHERE report_date = ? AND member_key = ?
                ORDER BY event_time_ms DESC
                LIMIT 1
                """,
                (report_date, member_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def iteration_statuses(self, assignment_label: str) -> Dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT member_name, status, event_time_ms
                FROM iteration_events
                WHERE UPPER(REPLACE(assignment_label, ' ', '')) = ?
                ORDER BY event_time_ms ASC
                """,
                (assignment_label.upper().replace(" ", ""),),
            ).fetchall()
        return {str(row["member_name"]): str(row["status"]) for row in rows}
