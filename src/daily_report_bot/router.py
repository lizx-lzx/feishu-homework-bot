from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Settings
from .models import IncomingMessage, SummaryResult
from .service import GroupSummaryService
from .store import LocalStore


logger = logging.getLogger(__name__)


def _apply_group_profile(settings: Settings, profile: Dict[str, Any]) -> Settings:
    """把群级配置覆盖到全局默认值；名单和昵称映射按群完全隔离。"""
    updates: Dict[str, Any] = {}
    scalar_fields = {
        "report_title",
        "report_link",
        "base_token",
        "base_table_id",
        "send_enabled",
        "homework_reaction_enabled",
        "base_sync_enabled",
        "reminder_enabled",
        "missing_list_enabled",
        "makeup_reminder_enabled",
        "makeup_summary_enabled",
    }
    for key in scalar_fields:
        if key in profile:
            updates[key] = profile[key]
    if "report_members" in profile:
        updates["report_members"] = tuple(str(item) for item in profile["report_members"])
    if "member_aliases" in profile:
        updates["member_aliases"] = {
            str(key): str(value) for key, value in profile["member_aliases"].items()
        }
    if "visible_name_aliases" in profile:
        updates["visible_name_aliases"] = {
            str(key): str(value) for key, value in profile["visible_name_aliases"].items()
        }
    if "assignment_deadline_overrides" in profile:
        updates["assignment_deadline_overrides"] = {
            str(key): str(value) for key, value in profile["assignment_deadline_overrides"].items()
        }
    if "excluded_member_ids" in profile:
        updates["excluded_member_ids"] = tuple(str(item) for item in profile["excluded_member_ids"])
    elif "additional_excluded_member_ids" in profile:
        updates["excluded_member_ids"] = tuple(
            sorted(
                set(settings.excluded_member_ids)
                | {str(item) for item in profile["additional_excluded_member_ids"]}
            )
        )
    return replace(settings, **updates)


class GroupServiceRouter:
    """在一个飞书长连接中按群路由到彼此隔离的服务和 SQLite。"""

    def __init__(self, settings: Settings, api: object, summarizer: object):
        self.settings = settings
        self._services: Dict[str, GroupSummaryService] = {}
        self._stores: Dict[str, LocalStore] = {}
        self._default_service: Optional[GroupSummaryService] = None
        self._default_store: Optional[LocalStore] = None

        if not settings.group_databases:
            self._default_store = LocalStore(settings.db_path)
            self._default_service = GroupSummaryService(
                settings, api, summarizer, self._default_store
            )
            return

        capture_only = set(settings.capture_only_chat_ids)
        for chat_id, db_path in settings.group_databases.items():
            group_settings = replace(
                settings,
                chat_ids=(chat_id,),
                db_path=Path(db_path),
            )
            if profile := settings.group_profiles.get(chat_id):
                group_settings = _apply_group_profile(group_settings, profile)
            if chat_id in capture_only:
                group_settings = replace(
                    group_settings,
                    send_enabled=False,
                    homework_reaction_enabled=False,
                    base_sync_enabled=False,
                    reminder_enabled=False,
                    missing_list_enabled=False,
                    makeup_reminder_enabled=False,
                    makeup_summary_enabled=False,
                    report_members=(),
                    member_aliases={},
                    visible_name_aliases={},
                    assignment_deadline_overrides={},
                )
            store = LocalStore(group_settings.db_path)
            self._stores[chat_id] = store
            self._services[chat_id] = GroupSummaryService(group_settings, api, summarizer, store)
            logger.info(
                "群路由已加载：chat=%s db=%s 名单=%d 发送=%s Base=%s",
                chat_id,
                group_settings.db_path,
                len(group_settings.report_members),
                "开启" if group_settings.send_enabled else "关闭",
                "开启" if group_settings.base_sync_enabled else "关闭",
            )

    @property
    def stores(self) -> List[LocalStore]:
        if self._default_store is not None:
            return [self._default_store]
        return list(self._stores.values())

    def known_chat_ids(self) -> List[str]:
        chat_ids = set(self._services)
        for store in self.stores:
            chat_ids.update(store.list_chats())
        return sorted(chat_ids)

    def service_for_chat(self, chat_id: str) -> Optional[GroupSummaryService]:
        if self._default_service is not None:
            return self._default_service
        return self._services.get(chat_id)

    def handle_message(self, message: IncomingMessage) -> bool:
        service = self.service_for_chat(message.chat_id)
        if service is None:
            logger.warning("忽略未配置数据库的群消息：%s", message.chat_id)
            return False
        return service.handle_message(message)

    def send_due_summaries(self, report_date: Optional[str] = None) -> List[SummaryResult]:
        results: List[SummaryResult] = []
        for service in self._all_services():
            results.extend(service.send_due_summaries(report_date))
        return results

    def send_due_reminders(self, report_date: Optional[str] = None) -> List[str]:
        message_ids: List[str] = []
        for service in self._all_services():
            message_ids.extend(service.send_due_reminders(report_date))
        return message_ids

    def send_due_missing_lists(self, report_date: Optional[str] = None) -> List[str]:
        message_ids: List[str] = []
        for service in self._all_services():
            message_ids.extend(service.send_due_missing_lists(report_date))
        return message_ids

    def send_due_final_statuses(self, report_date: Optional[str] = None) -> List[str]:
        message_ids: List[str] = []
        for service in self._all_services():
            message_ids.extend(service.send_due_final_statuses(report_date))
        return message_ids

    def send_due_makeup_reminders(self, report_date: Optional[str] = None) -> List[str]:
        message_ids: List[str] = []
        for service in self._all_services():
            message_ids.extend(service.send_due_makeup_reminders(report_date))
        return message_ids

    def send_due_makeup_summaries(self, report_date: Optional[str] = None) -> List[str]:
        message_ids: List[str] = []
        for service in self._all_services():
            message_ids.extend(service.send_due_makeup_summaries(report_date))
        return message_ids

    def sync_assignment_deadline(self, report_date: str) -> int:
        return sum(
            service.sync_assignment_deadline(report_date) for service in self._all_services()
        )

    def _all_services(self) -> List[GroupSummaryService]:
        if self._default_service is not None:
            return [self._default_service]
        return list(self._services.values())
