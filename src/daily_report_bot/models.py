from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    sender_type: str
    message_type: str
    content: str
    create_time_ms: int
    parent_id: Optional[str] = None
    root_id: Optional[str] = None
    thread_id: Optional[str] = None


@dataclass(frozen=True)
class ParsedContent:
    text: str = ""


@dataclass(frozen=True)
class StoredMessage:
    message_id: str
    chat_id: str
    sender_open_id: str
    sender_name: str
    message_type: str
    content: str
    create_time_ms: int
    parent_id: str = ""
    root_id: str = ""
    thread_id: str = ""


@dataclass(frozen=True)
class AttendanceRecord:
    report_date: str
    member_key: str
    sender_open_id: str
    sender_name: str
    assignment_label: str
    homework_status: str
    review_status: str
    homework_source: str
    homework_message_ids: Tuple[str, ...] = ()
    review_message_ids: Tuple[str, ...] = ()


@dataclass
class SummaryResult:
    chat_id: str
    report_date: str
    text: str
    message_count: int
    participant_count: int
    message_id: Optional[str] = None
    generated_at: datetime = field(default_factory=datetime.now)
