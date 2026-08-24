from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Tuple, Union
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    pass


def _csv(name: str) -> Tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} 必须是 true 或 false")


def _json_string_map(name: str) -> Dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{name} 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        raise ConfigurationError(f"{name} 必须是字符串到字符串的 JSON 对象")
    return dict(parsed)


def _load_group_profiles() -> Dict[str, Dict[str, Any]]:
    raw = os.getenv("GROUP_PROFILES_JSON", "").strip()
    source = "GROUP_PROFILES_JSON"
    if not raw:
        path_value = os.getenv("GROUP_PROFILES_PATH", "").strip()
        if not path_value:
            return {}
        path = Path(path_value)
        source = f"GROUP_PROFILES_PATH={path}"
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(f"无法读取群配置文件 {path}：{exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{source} 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigurationError(f"{source} 必须是 chat_id 到配置对象的 JSON 对象")
    profiles: Dict[str, Dict[str, Any]] = {}
    for chat_id, profile in parsed.items():
        if not isinstance(chat_id, str) or not isinstance(profile, dict):
            raise ConfigurationError(f"{source} 中每个群配置都必须是 JSON 对象")
        profiles[chat_id] = dict(profile)
    return profiles


DEFAULT_MEMBER_ALIASES: Dict[str, str] = {}
DEFAULT_EXCLUDED_MEMBER_IDS: Tuple[str, ...] = ()
DEFAULT_REPORT_MEMBERS: Tuple[str, ...] = ()
DEFAULT_REPORT_LINK = ""


def _load_member_aliases() -> Dict[str, str]:
    aliases = dict(DEFAULT_MEMBER_ALIASES)
    raw = os.getenv("MEMBER_ALIASES_JSON")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"MEMBER_ALIASES_JSON 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigurationError("MEMBER_ALIASES_JSON 必须是 JSON 对象")
        for key, value in parsed.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ConfigurationError("MEMBER_ALIASES_JSON 的键和值都必须是字符串")
        aliases.update(parsed)
    return aliases


def _load_excluded_member_ids() -> Tuple[str, ...]:
    excluded = set(DEFAULT_EXCLUDED_MEMBER_IDS)
    excluded.update(_csv("EXCLUDED_MEMBER_IDS"))
    return tuple(sorted(excluded))


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    chat_ids: Tuple[str, ...]
    timezone: str
    summary_hour: int
    summary_minute: int
    summary_commands: Tuple[str, ...]
    send_enabled: bool
    max_messages: int
    max_chars_per_request: int
    db_path: Path
    log_level: str
    summary_schedule_enabled: bool = True
    homework_reaction_enabled: bool = False
    social_chat_enabled: bool = False
    social_chat_proactive_enabled: bool = False
    social_chat_cooldown_minutes: int = 8
    social_chat_hourly_limit: int = 4
    member_aliases: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MEMBER_ALIASES))
    visible_name_aliases: Dict[str, str] = field(default_factory=dict)
    excluded_member_ids: Tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_EXCLUDED_MEMBER_IDS
    )
    leader_member_ids: Tuple[str, ...] = ()
    report_title: str = "进阶营作业群・每日日报"
    report_members: Tuple[str, ...] = field(default_factory=lambda: DEFAULT_REPORT_MEMBERS)
    report_link: str = DEFAULT_REPORT_LINK
    review_tag: str = "#复盘"
    base_sync_enabled: bool = False
    base_token: str = ""
    base_table_id: str = ""
    reminder_enabled: bool = False
    reminder_hour: int = 20
    reminder_minute: int = 0
    missing_list_enabled: bool = False
    missing_list_hour: int = 20
    missing_list_minute: int = 0
    final_status_enabled: bool = False
    final_status_hour: int = 20
    final_status_minute: int = 0
    makeup_reminder_enabled: bool = False
    makeup_reminder_hour: int = 17
    makeup_reminder_minute: int = 0
    makeup_summary_enabled: bool = False
    makeup_summary_hour: int = 20
    makeup_summary_minute: int = 0
    assignment_cycle_start_date: str = ""
    assignment_cycle_days: int = 1
    assignment_publish_hour: int = 20
    assignment_publish_minute: int = 0
    assignment_due_hour: int = 20
    assignment_due_minute: int = 0
    assignment_deadline_overrides: Dict[str, str] = field(default_factory=dict)
    group_databases: Dict[str, str] = field(default_factory=dict)
    capture_only_chat_ids: Tuple[str, ...] = ()
    group_profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def assignment_deadline(self, report_date: str) -> datetime:
        cycle_start, due_day, _ = self.assignment_cycle(report_date)
        raw = self.assignment_deadline_overrides.get(cycle_start.isoformat(), "").strip()
        if not raw:
            return datetime.combine(
                due_day,
                time(self.assignment_due_hour, self.assignment_due_minute),
                tzinfo=self.tz,
            )
        try:
            deadline = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ConfigurationError(f"作业截止时间格式不合法：{report_date}={raw}") from exc
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=self.tz)
        return deadline.astimezone(self.tz)

    def assignment_cycle(self, value: Union[str, date]) -> Tuple[date, date, int]:
        day = value if isinstance(value, date) else datetime.strptime(value, "%Y-%m-%d").date()
        if not self.assignment_cycle_start_date or self.assignment_cycle_days <= 1:
            return day, day, 1
        course_start = datetime.strptime(self.assignment_cycle_start_date, "%Y-%m-%d").date()
        if day < course_start:
            return day, day, 1
        index = (day - course_start).days // self.assignment_cycle_days
        cycle_start = course_start + timedelta(days=index * self.assignment_cycle_days)
        cycle_end = cycle_start + timedelta(days=self.assignment_cycle_days - 1)
        return cycle_start, cycle_end, index + 1

    def assignment_report_date(self, value: Union[str, date]) -> str:
        return self.assignment_cycle(value)[0].isoformat()

    def is_assignment_due_day(self, value: Union[str, date]) -> bool:
        day = value if isinstance(value, date) else datetime.strptime(value, "%Y-%m-%d").date()
        return day == self.assignment_cycle(day)[1]

    def is_makeup_day(self, value: Union[str, date]) -> bool:
        day = value if isinstance(value, date) else datetime.strptime(value, "%Y-%m-%d").date()
        previous_day = day - timedelta(days=1)
        return day == self.assignment_cycle(previous_day)[1] + timedelta(days=1)

    def makeup_report_date(self, value: Union[str, date]) -> str:
        day = value if isinstance(value, date) else datetime.strptime(value, "%Y-%m-%d").date()
        previous_day = day - timedelta(days=1)
        cycle_start, due_day, _ = self.assignment_cycle(previous_day)
        if day == due_day + timedelta(days=1):
            return cycle_start.isoformat()
        return self.assignment_report_date(day)

    def validate(self, require_secrets: bool = True) -> None:
        required = {
            "FEISHU_APP_ID": self.app_id,
            "LLM_BASE_URL": self.llm_base_url,
            "LLM_MODEL": self.llm_model,
        }
        if require_secrets:
            required.update(
                {
                    "FEISHU_APP_SECRET": self.app_secret,
                    "LLM_API_KEY": self.llm_api_key,
                }
            )
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError("缺少配置：" + ", ".join(missing))
        if not 0 <= self.summary_hour <= 23 or not 0 <= self.summary_minute <= 59:
            raise ConfigurationError("汇总时间不合法")
        if not 0 <= self.reminder_hour <= 23 or not 0 <= self.reminder_minute <= 59:
            raise ConfigurationError("提醒时间不合法")
        if not 0 <= self.missing_list_hour <= 23 or not 0 <= self.missing_list_minute <= 59:
            raise ConfigurationError("未交名单发送时间不合法")
        for label, hour, minute in (
            ("最终汇总", self.final_status_hour, self.final_status_minute),
            ("补交提醒", self.makeup_reminder_hour, self.makeup_reminder_minute),
            ("补交汇总", self.makeup_summary_hour, self.makeup_summary_minute),
            ("作业发布", self.assignment_publish_hour, self.assignment_publish_minute),
            ("作业截止", self.assignment_due_hour, self.assignment_due_minute),
        ):
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ConfigurationError(f"{label}时间不合法")
        if self.assignment_cycle_days < 1:
            raise ConfigurationError("ASSIGNMENT_CYCLE_DAYS 必须大于等于 1")
        if self.social_chat_cooldown_minutes < 1:
            raise ConfigurationError("SOCIAL_CHAT_COOLDOWN_MINUTES 必须大于等于 1")
        if self.social_chat_hourly_limit < 1:
            raise ConfigurationError("SOCIAL_CHAT_HOURLY_LIMIT 必须大于等于 1")
        if self.assignment_cycle_start_date:
            try:
                datetime.strptime(self.assignment_cycle_start_date, "%Y-%m-%d")
            except ValueError as exc:
                raise ConfigurationError("ASSIGNMENT_CYCLE_START_DATE 必须是 YYYY-MM-DD") from exc
        if self.base_sync_enabled and (not self.base_token or not self.base_table_id):
            raise ConfigurationError("启用多维表格同步时必须配置 BASE_TOKEN 和 BASE_TABLE_ID")
        if self.max_messages < 1 or self.max_chars_per_request < 1000:
            raise ConfigurationError("消息数量或模型文本长度限制不合法")
        if not self.summary_commands:
            raise ConfigurationError("SUMMARY_COMMANDS 不能为空")
        try:
            self.tz
        except Exception as exc:
            raise ConfigurationError(f"时区不合法：{self.timezone}") from exc
        for report_date in self.assignment_deadline_overrides:
            try:
                self.assignment_deadline(report_date)
            except ValueError as exc:
                raise ConfigurationError(f"作业日期格式不合法：{report_date}") from exc
        unknown_capture_chats = set(self.capture_only_chat_ids) - set(self.group_databases)
        if self.group_databases and unknown_capture_chats:
            raise ConfigurationError(
                "CAPTURE_ONLY_CHAT_IDS 包含未配置数据库的群："
                + ", ".join(sorted(unknown_capture_chats))
            )
        unknown_profile_chats = set(self.group_profiles) - set(self.group_databases)
        if self.group_databases and unknown_profile_chats:
            raise ConfigurationError(
                "GROUP_PROFILES 包含未配置数据库的群：" + ", ".join(sorted(unknown_profile_chats))
            )


def load_settings(env_file: str = ".env") -> Settings:
    load_dotenv(env_file, override=False)
    return Settings(
        app_id=os.getenv("FEISHU_APP_ID", ""),
        app_secret=os.getenv("FEISHU_APP_SECRET", ""),
        llm_base_url=os.getenv("LLM_BASE_URL")
        or os.getenv("INFO_RADAR_AI_BASE_URL", "https://api.minimaxi.com/v1"),
        llm_api_key=os.getenv("LLM_API_KEY")
        or os.getenv("MINIMAX_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("INFO_RADAR_AI_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL") or os.getenv("INFO_RADAR_AI_MODEL", "MiniMax-M3"),
        chat_ids=_csv("FEISHU_CHAT_IDS"),
        timezone=os.getenv("SUMMARY_TIMEZONE", "Asia/Shanghai"),
        summary_hour=int(os.getenv("SUMMARY_HOUR", "23")),
        summary_minute=int(os.getenv("SUMMARY_MINUTE", "0")),
        summary_commands=_csv("SUMMARY_COMMANDS") or ("打开日报", "#总结", "#今日总结"),
        send_enabled=_bool("SUMMARY_SEND_ENABLED", True),
        summary_schedule_enabled=_bool("SUMMARY_SCHEDULE_ENABLED", True),
        homework_reaction_enabled=_bool("HOMEWORK_REACTION_ENABLED", False),
        social_chat_enabled=_bool("SOCIAL_CHAT_ENABLED", False),
        social_chat_proactive_enabled=_bool("SOCIAL_CHAT_PROACTIVE_ENABLED", False),
        social_chat_cooldown_minutes=int(os.getenv("SOCIAL_CHAT_COOLDOWN_MINUTES", "8")),
        social_chat_hourly_limit=int(os.getenv("SOCIAL_CHAT_HOURLY_LIMIT", "4")),
        max_messages=int(os.getenv("MAX_MESSAGES", "2000")),
        max_chars_per_request=int(os.getenv("MAX_CHARS_PER_REQUEST", "50000")),
        db_path=Path(os.getenv("SUMMARY_DB_PATH", "./data/group_messages.sqlite3")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        member_aliases=_load_member_aliases(),
        visible_name_aliases=_json_string_map("VISIBLE_NAME_ALIASES_JSON"),
        excluded_member_ids=_load_excluded_member_ids(),
        leader_member_ids=_csv("LEADER_MEMBER_IDS"),
        report_title=os.getenv("REPORT_TITLE", "进阶营作业群・每日日报"),
        report_members=_csv("REPORT_MEMBERS") or DEFAULT_REPORT_MEMBERS,
        report_link=os.getenv("REPORT_LINK", DEFAULT_REPORT_LINK),
        review_tag=os.getenv("REVIEW_TAG", "#复盘"),
        base_sync_enabled=_bool("BASE_SYNC_ENABLED", True),
        base_token=os.getenv("BASE_TOKEN", ""),
        base_table_id=os.getenv("BASE_TABLE_ID", ""),
        reminder_enabled=_bool("REMINDER_ENABLED", True),
        reminder_hour=int(os.getenv("REMINDER_HOUR", "17")),
        reminder_minute=int(os.getenv("REMINDER_MINUTE", "0")),
        missing_list_enabled=_bool("MISSING_LIST_ENABLED", True),
        missing_list_hour=int(os.getenv("MISSING_LIST_HOUR", "20")),
        missing_list_minute=int(os.getenv("MISSING_LIST_MINUTE", "0")),
        final_status_enabled=_bool("FINAL_STATUS_ENABLED", False),
        final_status_hour=int(os.getenv("FINAL_STATUS_HOUR", "20")),
        final_status_minute=int(os.getenv("FINAL_STATUS_MINUTE", "0")),
        makeup_reminder_enabled=_bool("MAKEUP_REMINDER_ENABLED", True),
        makeup_reminder_hour=int(os.getenv("MAKEUP_REMINDER_HOUR", "17")),
        makeup_reminder_minute=int(os.getenv("MAKEUP_REMINDER_MINUTE", "0")),
        makeup_summary_enabled=_bool("MAKEUP_SUMMARY_ENABLED", True),
        makeup_summary_hour=int(os.getenv("MAKEUP_SUMMARY_HOUR", "20")),
        makeup_summary_minute=int(os.getenv("MAKEUP_SUMMARY_MINUTE", "0")),
        assignment_cycle_start_date=os.getenv("ASSIGNMENT_CYCLE_START_DATE", ""),
        assignment_cycle_days=int(os.getenv("ASSIGNMENT_CYCLE_DAYS", "1")),
        assignment_publish_hour=int(os.getenv("ASSIGNMENT_PUBLISH_HOUR", "20")),
        assignment_publish_minute=int(os.getenv("ASSIGNMENT_PUBLISH_MINUTE", "0")),
        assignment_due_hour=int(os.getenv("ASSIGNMENT_DUE_HOUR", "20")),
        assignment_due_minute=int(os.getenv("ASSIGNMENT_DUE_MINUTE", "0")),
        assignment_deadline_overrides=_json_string_map("ASSIGNMENT_DEADLINE_OVERRIDES_JSON"),
        group_databases=_json_string_map("GROUP_DATABASES_JSON"),
        capture_only_chat_ids=_csv("CAPTURE_ONLY_CHAT_IDS"),
        group_profiles=_load_group_profiles(),
    )
