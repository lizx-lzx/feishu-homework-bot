from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union
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
class CoursePhase:
    name: str
    start_date: str
    end_date: str = ""
    cycle_days: int = 1
    publish_hour: int = 20
    publish_minute: int = 0
    due_hour: int = 20
    due_minute: int = 0
    due_day_offset: Optional[int] = None
    makeup_day_offset: int = 1
    makeup_hour: Optional[int] = None
    makeup_minute: Optional[int] = None

    @property
    def start_day(self) -> date:
        return datetime.strptime(self.start_date, "%Y-%m-%d").date()

    @property
    def end_day(self) -> Optional[date]:
        if not self.end_date:
            return None
        return datetime.strptime(self.end_date, "%Y-%m-%d").date()

    def contains(self, day: date) -> bool:
        return day >= self.start_day and (self.end_day is None or day <= self.end_day)


def _load_course_phases() -> Tuple[CoursePhase, ...]:
    raw = os.getenv("COURSE_PHASES_JSON", "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"COURSE_PHASES_JSON 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, list):
        raise ConfigurationError("COURSE_PHASES_JSON 必须是 JSON 数组")
    phases = []
    allowed = {
        "name",
        "start_date",
        "end_date",
        "cycle_days",
        "publish_hour",
        "publish_minute",
        "due_hour",
        "due_minute",
        "due_day_offset",
        "makeup_day_offset",
        "makeup_hour",
        "makeup_minute",
    }
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise ConfigurationError(f"COURSE_PHASES_JSON 第 {index} 项必须是 JSON 对象")
        unknown = set(item) - allowed
        if unknown:
            raise ConfigurationError(
                f"COURSE_PHASES_JSON 第 {index} 项包含未知字段：" + ", ".join(sorted(unknown))
            )
        try:
            phases.append(
                CoursePhase(
                    name=str(item.get("name") or "").strip(),
                    start_date=str(item.get("start_date") or "").strip(),
                    end_date=str(item.get("end_date") or "").strip(),
                    cycle_days=int(item.get("cycle_days", 1)),
                    publish_hour=int(item.get("publish_hour", 20)),
                    publish_minute=int(item.get("publish_minute", 0)),
                    due_hour=int(item.get("due_hour", 20)),
                    due_minute=int(item.get("due_minute", 0)),
                    due_day_offset=(
                        int(item["due_day_offset"])
                        if item.get("due_day_offset") is not None
                        else None
                    ),
                    makeup_day_offset=int(item.get("makeup_day_offset", 1)),
                    makeup_hour=(
                        int(item["makeup_hour"]) if item.get("makeup_hour") is not None else None
                    ),
                    makeup_minute=(
                        int(item["makeup_minute"])
                        if item.get("makeup_minute") is not None
                        else None
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"COURSE_PHASES_JSON 第 {index} 项的数字字段不合法") from exc
    return tuple(phases)


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
    makeup_deadline_hour: int = 12
    makeup_deadline_minute: int = 0
    makeup_summary_enabled: bool = False
    makeup_summary_hour: int = 22
    makeup_summary_minute: int = 0
    assignment_cycle_start_date: str = ""
    course_end_date: str = ""
    assignment_cycle_days: int = 1
    assignment_publish_hour: int = 20
    assignment_publish_minute: int = 0
    assignment_due_hour: int = 20
    assignment_due_minute: int = 0
    course_phases: Tuple[CoursePhase, ...] = ()
    assignment_deadline_overrides: Dict[str, str] = field(default_factory=dict)
    group_databases: Dict[str, str] = field(default_factory=dict)
    capture_only_chat_ids: Tuple[str, ...] = ()
    group_profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def assignment_deadline(self, report_date: str) -> datetime:
        cycle_start, cycle_end, _ = self.assignment_cycle(report_date)
        phase = self.course_phase(cycle_start)
        due_hour = phase.due_hour if phase else self.assignment_due_hour
        due_minute = phase.due_minute if phase else self.assignment_due_minute
        due_day = cycle_end
        if phase is not None and phase.due_day_offset is not None:
            due_day = cycle_start + timedelta(days=phase.due_day_offset)
        raw = self.assignment_deadline_overrides.get(cycle_start.isoformat(), "").strip()
        if not raw:
            return datetime.combine(
                due_day,
                time(due_hour, due_minute),
                tzinfo=self.tz,
            )
        try:
            deadline = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ConfigurationError(f"作业截止时间格式不合法：{report_date}={raw}") from exc
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=self.tz)
        return deadline.astimezone(self.tz)

    def makeup_deadline(self, report_date: str) -> datetime:
        normal_deadline = self.assignment_deadline(report_date)
        cycle_start, _, _ = self.assignment_cycle(report_date)
        phase = self.course_phase(cycle_start)
        day_offset = phase.makeup_day_offset if phase else 1
        hour = (
            phase.makeup_hour
            if phase is not None and phase.makeup_hour is not None
            else self.makeup_deadline_hour
        )
        minute = (
            phase.makeup_minute
            if phase is not None and phase.makeup_minute is not None
            else self.makeup_deadline_minute
        )
        return datetime.combine(
            normal_deadline.date() + timedelta(days=day_offset),
            time(hour, minute),
            tzinfo=self.tz,
        )

    def is_makeup_submission(self, report_date: str, submitted_at: datetime) -> bool:
        submitted_at = submitted_at.astimezone(self.tz)
        return (
            self.assignment_deadline(report_date) < submitted_at < self.makeup_deadline(report_date)
        )

    @property
    def course_end_day(self) -> Optional[date]:
        phase = self.course_phase_for_reference(datetime.now(tz=self.tz).date())
        return phase.end_day if phase else None

    @property
    def configured_course_phases(self) -> Tuple[CoursePhase, ...]:
        if self.course_phases:
            return self.course_phases
        if not self.assignment_cycle_start_date:
            return ()
        return (
            CoursePhase(
                name="技术周",
                start_date=self.assignment_cycle_start_date,
                end_date=self.course_end_date,
                cycle_days=self.assignment_cycle_days,
                publish_hour=self.assignment_publish_hour,
                publish_minute=self.assignment_publish_minute,
                due_hour=self.assignment_due_hour,
                due_minute=self.assignment_due_minute,
            ),
        )

    def course_phase(self, value: Union[str, date], name: str = "") -> Optional[CoursePhase]:
        day = value if isinstance(value, date) else datetime.strptime(value, "%Y-%m-%d").date()
        phases = self.configured_course_phases
        if name:
            return next((phase for phase in phases if phase.name == name), None)
        return next((phase for phase in phases if phase.contains(day)), None)

    def course_phase_for_reference(
        self,
        value: Union[str, date],
        name: str = "",
    ) -> Optional[CoursePhase]:
        day = value if isinstance(value, date) else datetime.strptime(value, "%Y-%m-%d").date()
        if name:
            return self.course_phase(day, name)
        if phase := self.course_phase(day):
            return phase
        previous = [phase for phase in self.configured_course_phases if phase.start_day <= day]
        return previous[-1] if previous else None

    def course_is_active(self, value: Union[str, date]) -> bool:
        return self.course_phase(value) is not None

    def course_has_ended(self, value: Union[str, date]) -> bool:
        day = value if isinstance(value, date) else datetime.strptime(value, "%Y-%m-%d").date()
        if self.course_phase(day) is not None:
            return False
        if any(phase.start_day > day for phase in self.configured_course_phases):
            return False
        phase = self.course_phase_for_reference(day)
        return phase is not None and phase.end_day is not None and day > phase.end_day

    def total_assignment_cycles(
        self,
        value: Optional[Union[str, date]] = None,
        course_name: str = "",
    ) -> Optional[int]:
        reference = value or datetime.now(tz=self.tz).date()
        phase = self.course_phase_for_reference(reference, course_name)
        if phase is None or phase.end_day is None:
            return None
        return (phase.end_day - phase.start_day).days // phase.cycle_days + 1

    @staticmethod
    def assignment_cycle_in_phase(day: date, phase: CoursePhase) -> Tuple[date, date, int]:
        effective_day = day
        if phase.end_day is not None and effective_day > phase.end_day:
            effective_day = phase.end_day
        index = max(0, (effective_day - phase.start_day).days // phase.cycle_days)
        cycle_start = phase.start_day + timedelta(days=index * phase.cycle_days)
        cycle_end = cycle_start + timedelta(days=phase.cycle_days - 1)
        if phase.end_day is not None:
            cycle_end = min(cycle_end, phase.end_day)
        return cycle_start, cycle_end, index + 1

    def assignment_cycle(self, value: Union[str, date]) -> Tuple[date, date, int]:
        day = value if isinstance(value, date) else datetime.strptime(value, "%Y-%m-%d").date()
        phase = self.course_phase(day)
        if phase is not None:
            return self.assignment_cycle_in_phase(day, phase)
        phases = self.configured_course_phases
        if not phases:
            return day, day, 1
        if any(item.start_day > day for item in phases):
            return day, day, 1
        previous = [item for item in phases if item.start_day <= day]
        if previous and previous[-1].end_day is not None and day > previous[-1].end_day:
            return self.assignment_cycle_in_phase(day, previous[-1])
        return day, day, 1

    def assignment_date_for_number(
        self,
        assignment_number: int,
        reference_day: Union[str, date],
        course_name: str = "",
    ) -> Optional[date]:
        phase = self.course_phase_for_reference(reference_day, course_name)
        if phase is None or assignment_number < 1:
            return None
        target = phase.start_day + timedelta(days=(assignment_number - 1) * phase.cycle_days)
        if phase.end_day is not None and target > phase.end_day:
            return None
        return target

    def assignment_cycle_days_for(self, value: Union[str, date]) -> int:
        phase = self.course_phase_for_reference(value)
        return phase.cycle_days if phase else self.assignment_cycle_days

    def assignment_publish_clock(self, value: Union[str, date]) -> Tuple[int, int]:
        phase = self.course_phase_for_reference(value)
        if phase:
            return phase.publish_hour, phase.publish_minute
        return self.assignment_publish_hour, self.assignment_publish_minute

    def assignment_due_clock(self, value: Union[str, date]) -> Tuple[int, int]:
        phase = self.course_phase_for_reference(value)
        if phase:
            return phase.due_hour, phase.due_minute
        return self.assignment_due_hour, self.assignment_due_minute

    def assignment_report_date(self, value: Union[str, date]) -> str:
        return self.assignment_cycle(value)[0].isoformat()

    def _report_date_for_deadline_day(
        self,
        value: Union[str, date],
        *,
        makeup: bool,
    ) -> Optional[str]:
        day = value if isinstance(value, date) else datetime.strptime(value, "%Y-%m-%d").date()
        phases = self.configured_course_phases
        if not phases:
            candidates = [day - timedelta(days=offset) for offset in range(4)]
        else:
            candidates = []
            for phase in reversed(phases):
                if makeup and phase.end_day is not None and day > phase.end_day:
                    continue
                due_offset = (
                    phase.due_day_offset
                    if phase.due_day_offset is not None
                    else phase.cycle_days - 1
                )
                lookback = phase.cycle_days + due_offset + phase.makeup_day_offset + 1
                candidates.extend(
                    candidate
                    for offset in range(lookback + 1)
                    if phase.contains(candidate := day - timedelta(days=offset))
                )

        seen: set[str] = set()
        for candidate in candidates:
            report_date = self.assignment_report_date(candidate)
            if report_date in seen:
                continue
            seen.add(report_date)
            deadline = (
                self.makeup_deadline(report_date)
                if makeup
                else self.assignment_deadline(report_date)
            )
            if deadline.date() == day:
                return report_date
        return None

    def assignment_due_report_date(self, value: Union[str, date]) -> Optional[str]:
        return self._report_date_for_deadline_day(value, makeup=False)

    def is_assignment_due_day(self, value: Union[str, date]) -> bool:
        return self.assignment_due_report_date(value) is not None

    def is_makeup_day(self, value: Union[str, date]) -> bool:
        return self._report_date_for_deadline_day(value, makeup=True) is not None

    def makeup_report_date(self, value: Union[str, date]) -> str:
        day = value if isinstance(value, date) else datetime.strptime(value, "%Y-%m-%d").date()
        return self._report_date_for_deadline_day(day, makeup=True) or self.assignment_report_date(
            day
        )

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
            ("补交截止", self.makeup_deadline_hour, self.makeup_deadline_minute),
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
                course_start = datetime.strptime(
                    self.assignment_cycle_start_date, "%Y-%m-%d"
                ).date()
            except ValueError as exc:
                raise ConfigurationError("ASSIGNMENT_CYCLE_START_DATE 必须是 YYYY-MM-DD") from exc
        else:
            course_start = None
        if self.course_end_date:
            try:
                course_end = datetime.strptime(self.course_end_date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ConfigurationError("COURSE_END_DATE 必须是 YYYY-MM-DD") from exc
            if course_start is None:
                raise ConfigurationError(
                    "配置 COURSE_END_DATE 时必须同时配置 ASSIGNMENT_CYCLE_START_DATE"
                )
            if course_end < course_start:
                raise ConfigurationError("COURSE_END_DATE 不能早于 ASSIGNMENT_CYCLE_START_DATE")
        previous_end: Optional[date] = None
        for index, phase in enumerate(self.configured_course_phases, start=1):
            if not phase.name or not phase.start_date:
                raise ConfigurationError(f"第 {index} 个课程阶段必须配置 name 和 start_date")
            try:
                phase_start = phase.start_day
                phase_end = phase.end_day
            except ValueError as exc:
                raise ConfigurationError(f"第 {index} 个课程阶段的日期必须是 YYYY-MM-DD") from exc
            if phase.cycle_days < 1:
                raise ConfigurationError(f"第 {index} 个课程阶段的 cycle_days 必须大于等于 1")
            if phase.due_day_offset is not None and phase.due_day_offset < 0:
                raise ConfigurationError(f"第 {index} 个课程阶段的 due_day_offset 不能小于 0")
            if phase.makeup_day_offset < 0:
                raise ConfigurationError(f"第 {index} 个课程阶段的 makeup_day_offset 不能小于 0")
            if phase_end is not None and phase_end < phase_start:
                raise ConfigurationError(f"第 {index} 个课程阶段的 end_date 不能早于 start_date")
            if previous_end is None and index > 1:
                raise ConfigurationError("无结束日的课程阶段必须放在最后")
            if previous_end is not None and phase_start <= previous_end:
                raise ConfigurationError("课程阶段的日期不能重叠")
            for label, hour, minute in (
                ("发布", phase.publish_hour, phase.publish_minute),
                ("截止", phase.due_hour, phase.due_minute),
            ):
                if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                    raise ConfigurationError(f"第 {index} 个课程阶段的{label}时间不合法")
            if phase.makeup_hour is not None and not 0 <= phase.makeup_hour <= 23:
                raise ConfigurationError(f"第 {index} 个课程阶段的补交小时不合法")
            if phase.makeup_minute is not None and not 0 <= phase.makeup_minute <= 59:
                raise ConfigurationError(f"第 {index} 个课程阶段的补交分钟不合法")
            if self.makeup_deadline(phase.start_date) <= self.assignment_deadline(phase.start_date):
                raise ConfigurationError(f"第 {index} 个课程阶段的补交截止必须晚于正常截止")
            previous_end = phase_end
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
        makeup_reminder_enabled=_bool("MAKEUP_REMINDER_ENABLED", False),
        makeup_reminder_hour=int(os.getenv("MAKEUP_REMINDER_HOUR", "17")),
        makeup_reminder_minute=int(os.getenv("MAKEUP_REMINDER_MINUTE", "0")),
        makeup_deadline_hour=int(os.getenv("MAKEUP_DEADLINE_HOUR", "12")),
        makeup_deadline_minute=int(os.getenv("MAKEUP_DEADLINE_MINUTE", "0")),
        makeup_summary_enabled=_bool("MAKEUP_SUMMARY_ENABLED", True),
        makeup_summary_hour=int(os.getenv("MAKEUP_SUMMARY_HOUR", "22")),
        makeup_summary_minute=int(os.getenv("MAKEUP_SUMMARY_MINUTE", "0")),
        assignment_cycle_start_date=os.getenv("ASSIGNMENT_CYCLE_START_DATE", ""),
        course_end_date=os.getenv("COURSE_END_DATE", ""),
        assignment_cycle_days=int(os.getenv("ASSIGNMENT_CYCLE_DAYS", "1")),
        assignment_publish_hour=int(os.getenv("ASSIGNMENT_PUBLISH_HOUR", "20")),
        assignment_publish_minute=int(os.getenv("ASSIGNMENT_PUBLISH_MINUTE", "0")),
        assignment_due_hour=int(os.getenv("ASSIGNMENT_DUE_HOUR", "20")),
        assignment_due_minute=int(os.getenv("ASSIGNMENT_DUE_MINUTE", "0")),
        course_phases=_load_course_phases(),
        assignment_deadline_overrides=_json_string_map("ASSIGNMENT_DEADLINE_OVERRIDES_JSON"),
        group_databases=_json_string_map("GROUP_DATABASES_JSON"),
        capture_only_chat_ids=_csv("CAPTURE_ONLY_CHAT_IDS"),
        group_profiles=_load_group_profiles(),
    )
