from __future__ import annotations

import logging
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from secrets import choice
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Settings
from .models import AttendanceRecord, IncomingMessage, StoredMessage, SummaryResult
from .parser import decode_content, extract_merged_children
from .store import LocalStore


logger = logging.getLogger(__name__)


_EXPLICIT_REVIEW_DATE = re.compile(
    r"(?<!\d)(?:20\d{2}[年./-]\s*)?\d{1,2}\s*[月./-]\s*\d{1,2}(?:\s*日)?(?!\d)"
    r"|(?<!\d)\d{4}(?!\d)"
)
_REVIEW_MARKER = re.compile(r"#\s*[^#\n]{0,24}?复盘(?:打卡|总结)?")
_COMPLETION_STATUS = (
    r"已完成补打卡|已完成补提交|已完成补交|已完成补卡|补交打卡|"
    r"已补打卡|补打卡|已补提交|补提交|已补交|补交|已补卡|补卡|"
    r"已提交|提交|已完成|完成"
)
_COMPLETION_STATUS_END = r"(?!情况|要求|格式|了吗|吗|么|不了|度|率)"
_DATED_COMPLETION_MARKER = re.compile(
    rf"#\s*(?P<mmdd>\d{{4}})\s*日?\s*(?P<label>[^#\n]{{0,30}}?)"
    rf"(?P<status>{_COMPLETION_STATUS})"
    rf"{_COMPLETION_STATUS_END}"
)
_NATURAL_DATED_COMPLETION_MARKER = re.compile(
    r"#\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日?\s*"
    rf"(?P<label>[^#\n]{{0,30}}?)(?P<status>{_COMPLETION_STATUS})"
    rf"{_COMPLETION_STATUS_END}"
)
_UNDATED_COMPLETION_MARKER = re.compile(
    rf"(?P<label>(?:第\s*)?(?:\d+|[一二三四五六七八九十]+)\s*(?:次\s*)?"
    rf"(?:视频|图片|文字|技术)?作业(?:视频|图片|文字|技术)?)\s*"
    rf"(?P<status>{_COMPLETION_STATUS}){_COMPLETION_STATUS_END}"
)
_DAY_HOMEWORK_MARKER = re.compile(r"#\s*作业\s+(?P<assignment>DAY\s*\d+)", re.IGNORECASE)
_LATE_MARKER = re.compile(r"(?:#\s*)?补(?:提交|交|卡|打卡)")
_MENTION_ONLY_PREFIX = re.compile(r"^(?:@[\w\u4e00-\u9fff.\-·]+ *){0,2}$")
_RESOURCE_TOKEN = re.compile(r"\[(?:图片|文件|视频)\]")
_MULTI_MERGE_PREFIX = "[多人合并转发]"
_NON_HOMEWORK_LABELS = ("复盘", "迭代", "补交", "补卡")
_LATE_TAG_WINDOW_MS = 10 * 60 * 1000
_THREAD_HOMEWORK_TYPES = {"image", "file", "media"}
_FIRST_DAY_HOMEWORK_REACTIONS = ("LOVE", "FISTBUMP", "LAUGH", "FINGERHEART")
_SECOND_DAY_HOMEWORK_REACTIONS = ("WITTY", "TRICK", "Get", "HIGHFIVE", "PARTY")
_MAKEUP_HOMEWORK_REACTIONS = ("THINKING", "OnIt", "WOW", "Get")
_THREAD_ASSIGNMENT_LABEL = re.compile(r"(?P<label>第\s*\d+\s*次\s*作业)")
_ASSIGNMENT_NUMBER = re.compile(
    r"(?:第\s*)?(?P<number>\d+|[一二三四五六七八九十]+)\s*"
    r"(?:次(?:\s*作业)?|作业)"
)
_ASSIGNMENT_RANGE = re.compile(
    r"第\s*(?P<start>\d+|[一二三四五六七八九十]+)\s*"
    r"(?:次(?:\s*作业)?)?\s*(?:到|至|[-—~～])\s*(?:第\s*)?"
    r"(?P<end>\d+|[一二三四五六七八九十]+)\s*次(?:\s*作业)?"
)
_FIRST_ASSIGNMENTS = re.compile(r"前\s*(?P<count>\d+|[一二三四五六七八九十]+)\s*次(?:\s*作业)?")
_RECENT_ASSIGNMENTS = re.compile(r"这\s*(?P<count>\d+|[一二三四五六七八九十]+)\s*次(?:\s*作业)?")
_TECH_WEEK_OVERVIEW = re.compile(r"技术周(?:整体|全部|总览|汇总|打卡情况|作业情况)?")
_VIDEO_WEEK_OVERVIEW = re.compile(r"视频周(?:整体|全部|总览|汇总|打卡情况|作业情况)?")
_ASSIGNMENT_LABEL_ONLY = re.compile(
    r"^(?:第\s*)?(?P<number>\d+|[一二三四五六七八九十]+)\s*(?:次\s*)?"
    r"(?:视频|图片|文字|技术)?作业(?:视频|图片|文字|技术)?$"
)
_THREAD_COMPLETION_TEXT = re.compile(r"^\s*(?:#\s*)?已完成\s*[.!！。]?\s*$")
_WEB_LINK = re.compile(r"https?://\S+", re.IGNORECASE)
_QUERY_FULL_DATE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})[年./-](?P<month>\d{1,2})[月./-](?P<day>\d{1,2})(?:日)?(?!\d)"
)
_QUERY_MONTH_DAY = re.compile(r"(?<!\d)(?P<month>\d{1,2})[月./-](?P<day>\d{1,2})(?:日)?(?!\d)")
_QUERY_MMDD = re.compile(r"(?<!\d)(?P<month>0[1-9]|1[0-2])(?P<day>[0-3]\d)(?!\d)")
_STATS_TOPIC = re.compile(r"作业|打卡|提交|没交|未交|完成|复盘|迭代")
_TABLE_LINK_INTENT = re.compile(
    r"(?:打开|查看|唤出|调出|发给我|给我).{0,8}(?:打卡表|表格)"
    r"|(?:打卡表|表格).{0,8}(?:链接|在哪|打开|查看)"
)
_MENU_INTENT = re.compile(r"^(?:菜单|帮助|功能|怎么玩|能做什么|使用说明)$")
_MY_STATS_INTENT = re.compile(r"我的战绩|我的打卡|我的作业|我还差什么|我还差啥|我这次.*(?:交|完成)")
_MY_MISSING_DETAIL_INTENT = re.compile(
    r"我.{0,8}(?:哪|什么).{0,6}(?:次|个).{0,8}(?:没交|未交|没完成|未完成)"
    r"|我.{0,8}(?:没交|未交|没完成|未完成).{0,8}(?:哪|什么).{0,6}(?:次|个)"
)
_WEEKLY_GROWTH_INTENT = re.compile(r"本周成长|成长卡|本周战报")
_REMINDER_EXPLANATION_INTENT = re.compile(
    r"(?:你这个|这个|刚才|自动)?(?:催交|催作业|补交|补卡|未交)?"
    r"(?:提醒|通知).{0,10}(?:是啥|是什么|什么意思|怎么回事|干嘛|规则)"
    r"|(?:催交|催作业|补交|补卡)(?:提醒|通知)(?:规则|机制)"
)
_FEEDBACK_REQUEST = re.compile(r"#\s*求反馈")
_MENU_SHORTCUTS = {
    "1": "本次作业情况",
    "2": "本次作业谁还没交",
    "3": "我的战绩",
    "4": "本次作业补卡情况",
    "5": "本周成长卡",
    "6": "打开打卡表",
    "7": "前三次作业复查",
}
_SEMANTIC_STATS_SIGNAL = re.compile(
    r"掉队|没跟上|还差|进度|战绩|第\s*[0-9一二三四五六七八九十]+\s*次"
    r"|谁.{0,8}(?:做|交|完成|复盘)|(?:全部|历史).{0,8}(?:记录|打卡|作业)"
)
_SOCIAL_PROACTIVE_SIGNAL = re.compile(
    r"怎么办|怎么弄|怎么做|怎么解决|为什么|为啥|不会|不懂|卡住|卡在|报错"
    r"|打不开|求助|帮我|有人知道|有没有办法|能不能|如何|哪位知道|终于|跑通了|搞定了"
    r"|部署好了|解决了|成功了"
)
_SOCIAL_WORK_DISCUSSION_SIGNAL = re.compile(
    r"作业|课程|项目|页面|网站|代码|程序|部署|Cloudflare|"
    r"Codex|GPT|MiniMax|AI\s*工具|提示词|排版|设计|参考图|工作流|自动化"
)
_SOCIAL_SKIP_SIGNAL = re.compile(
    r"#\s*(?:\d{4}|复盘|迭代|求反馈)|打开日报|已完成|已提交|补卡|补交|补提交"
)
_CONVERSATIONAL_HELP_INTENT = re.compile(
    r"怎么做|怎么弄|怎么解决|帮我看|帮我想|给我个思路|卡住|卡在|报错|打不开"
    r"|部署|代码|页面|文案|排版|参考图|提示词"
)
_EXPLICIT_STATS_QUERY_SIGNAL = re.compile(
    r"谁|名单|多少|几人|没交|未交|补卡|状态|进度|打卡|复盘"
    r"|完成了吗|交了吗|第\s*[0-9一二三四五六七八九十]+\s*次"
)
_SOCIAL_FORBIDDEN_REPLY = re.compile(r"已帮你|已标记|已修改|已更新打卡|数据库已|多维表格已同步")
_LEADER_OVERRIDE_STATUS = re.compile(
    r"(?P<status>未提交|没交|未完成|已完成补提交|已完成补交|已完成补卡|"
    r"已补提交|补提交|已补交|补交|"
    r"已补卡|补卡|正常提交|已提交|已完成|完成)\s*[。.!！]*$"
)
_MISSING_INTENT = re.compile(r"没交|未交|没完成|未完成|缺交|还差|还有谁")
_COMPLETED_INTENT = re.compile(r"谁(?:已经|已)?(?:交了|完成)|已交(?:人员|名单)?|完成人员")
_MAKEUP_DECLARATION = re.compile(r"补(?:打卡|提交|交|卡)")
_MAKEUP_QUERY_INTENT = re.compile(
    r"谁|哪些|哪几|名单|人员|成员|情况|状态|统计|查询|查一下|"
    r"多少|几人|为什么|怎么|如何|是否|能否|吗|么|[？?]"
)
_EXPLICIT_ASSIGNMENT_REFERENCE = re.compile(
    r"第\s*(?:\d+|[一二三四五六七八九十]+)\s*次|今天|昨天|前天"
    r"|(?<!\d)(?:20\d{2}[年./-]\s*)?\d{1,2}\s*[月./-]\s*\d{1,2}(?:\s*日)?(?!\d)"
    r"|(?<!\d)(?:0[1-9]|1[0-2])[0-3]\d(?!\d)"
)
_HOMEWORK_EVIDENCE_DETAIL = re.compile(
    r"成果链接|作品链接|作业链接|作业说明|技术作业|"
    r"课程\s*[:：]|作品说明|交付成果"
)
_HOMEWORK_EVIDENCE_CONTEXT = re.compile(r"作业|打卡|作品|成果|课程")
_TEXT_ARTIFACT_FIELD = re.compile(
    r"(?:交付成果|作品正文|成品内容|剧本正文|分镜正文|完整提示词|代码正文)"
    r"\s*[:：]\s*(?=\S)"
)
_MEMBER_HISTORY_INTENT = re.compile(
    r"(?:全部|所有|历史).*(?:打卡|作业|复盘|记录|状态|情况)"
    r"|(?:打卡|作业|复盘).*(?:全部|所有|历史)"
)
_ITERATION_MARKER = re.compile(r"#\s*迭代\s*(?P<label>DAY\s*\d+|\d{4})", re.IGNORECASE)
_ITERATION_COMPLETE = re.compile(r"已迭代|迭代完成|已完成")
_MENTION_NAME = re.compile(r"@([^\s，,。！!？?：:#]+)")
_MANUAL_ATTENDANCE_STATUS = {
    "不参与统计": "excluded",
    "正常提交": "completed",
    "已提交": "completed",
    "补卡": "late",
    "已补交": "late",
    "未提交": "missing",
}


def _day_range_ms(day: date, tz: Any) -> Tuple[int, int]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


class GroupSummaryService:
    def __init__(self, settings: Settings, api: Any, summarizer: Any, store: LocalStore):
        self.settings = settings
        self.api = api
        self.summarizer = summarizer
        self.store = store
        self._name_cache: Dict[Tuple[str, str], str] = {}
        self._bot_name = ""
        self._bot_name_loaded = False
        self._base_record_index: Dict[str, str] = {}
        self._base_index_loaded = False
        self._base_sync_lock = Lock()
        self._welcome_guide_lock = Lock()

    def _chat_allowed(self, chat_id: str) -> bool:
        return not self.settings.chat_ids or chat_id in self.settings.chat_ids

    def _is_excluded(self, open_id: str) -> bool:
        return open_id in self.settings.excluded_member_ids

    def _resolve_sender_name(self, chat_id: str, open_id: str) -> str:
        alias = self.settings.member_aliases.get(open_id)
        if alias:
            return alias
        key = (chat_id, open_id)
        if key not in self._name_cache:
            try:
                self._name_cache[key] = self.api.get_member_name(chat_id, open_id)
            except Exception:
                logger.exception("获取群成员昵称失败，使用成员 ID 兜底：chat=%s", chat_id)
                self._name_cache[key] = ""
        return self._name_cache[key] or f"成员-{open_id[-6:]}"

    def _message_text(self, message: IncomingMessage) -> str:
        if message.message_type == "merge_forward":
            try:
                return extract_merged_children(
                    self.api.get_message_items(message.message_id),
                    outer_sender_id=message.sender_open_id,
                ).text
            except Exception:
                logger.exception("展开合并转发失败：%s", message.message_id)
                return "[合并转发消息]"
        return decode_content(message.message_type, message.content).text

    def _implicit_submission_report_date(
        self,
        text: str,
        submitted_at: datetime,
    ) -> str:
        """无标签附件在新旧作业窗口重叠时，优先归入当天中午到期的作业。"""
        day = submitted_at.astimezone(self.settings.tz).date()
        if _LATE_MARKER.search(text):
            return self.settings.makeup_report_date(day)
        due_report_date = self.settings.assignment_due_report_date(day)
        if due_report_date and submitted_at <= self.settings.assignment_deadline(due_report_date):
            return due_report_date
        return self.settings.assignment_report_date(day)

    def _is_summary_command(self, text: str) -> bool:
        compact = text.strip()
        return any(
            compact == command or compact.startswith(command + " ")
            for command in self.settings.summary_commands
        )

    def _mentioned_query(self, text: str) -> Optional[str]:
        if "@" not in text:
            return None
        if not self._bot_name_loaded:
            self._bot_name_loaded = True
            try:
                self._bot_name = self.api.check_bot().strip()
            except Exception:
                logger.exception("获取机器人名称失败，暂不处理群内@问答")
        if not self._bot_name:
            return None
        mention = re.compile(rf"@{re.escape(self._bot_name)}(?=$|[\s，,。！？!?：:])")
        if not mention.search(text):
            return None
        return re.sub(r"\s+", " ", mention.sub("", text, count=1)).strip(" ，,。！？!?：:")

    @staticmethod
    def _parse_assignment_number(value: str) -> int:
        if value.isdigit():
            return int(value)
        digits = {
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
        }
        if value == "十":
            return 10
        if "十" in value:
            tens_text, ones_text = value.split("十", 1)
            tens = digits.get(tens_text, 1) if tens_text else 1
            ones = digits.get(ones_text, 0) if ones_text else 0
            return tens * 10 + ones
        return digits.get(value, 0)

    def _query_report_date(self, question: str, reference_day: date) -> str:
        assignment_match = _ASSIGNMENT_NUMBER.search(question)
        if assignment_match and self.settings.configured_course_phases:
            assignment_number = self._parse_assignment_number(assignment_match.group("number"))
            target = self.settings.assignment_date_for_number(
                assignment_number,
                reference_day,
                self._course_name_hint(question),
            )
            if target is not None:
                return target.isoformat()
        if "昨天" in question:
            return (reference_day - timedelta(days=1)).isoformat()
        if "前天" in question:
            return (reference_day - timedelta(days=2)).isoformat()

        match = _QUERY_FULL_DATE.search(question)
        if match:
            try:
                return date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                ).isoformat()
            except ValueError:
                return reference_day.isoformat()

        match = _QUERY_MONTH_DAY.search(question) or _QUERY_MMDD.search(question)
        if match:
            try:
                candidate = date(
                    reference_day.year,
                    int(match.group("month")),
                    int(match.group("day")),
                )
                if candidate > reference_day + timedelta(days=31):
                    candidate = date(
                        reference_day.year - 1,
                        candidate.month,
                        candidate.day,
                    )
                return candidate.isoformat()
            except ValueError:
                return reference_day.isoformat()
        return reference_day.isoformat()

    @staticmethod
    def _course_name_hint(text: str) -> str:
        if "技术周" in text:
            return "技术周"
        if "视频周" in text:
            return "视频周"
        return ""

    def _course_assignment_limit_notice(
        self,
        text: str,
        reference_day: date,
    ) -> Optional[str]:
        match = _ASSIGNMENT_NUMBER.search(text)
        if match is None:
            return None
        assignment_number = self._parse_assignment_number(match.group("number"))
        course_name = self._course_name_hint(text)
        phase = self.settings.course_phase_for_reference(reference_day, course_name)
        if phase is None:
            return None
        target = self.settings.assignment_date_for_number(
            assignment_number,
            reference_day,
            course_name,
        )
        if target is not None and target <= reference_day:
            return None
        total = self.settings.total_assignment_cycles(reference_day, course_name)
        if total is None:
            _, _, current_assignment = self.settings.assignment_cycle_in_phase(reference_day, phase)
            return (
                f"{phase.name}从{phase.start_date}开始，"
                f"目前进行到第{current_assignment}次作业，"
                f"第{assignment_number}次还没有开始。"
            )
        return (
            f"本轮{phase.name}已于{phase.end_date}结束，"
            f"共{total}次作业，不存在第{assignment_number}次作业。"
        )

    def _multi_cycle_assignment_numbers(
        self,
        question: str,
        reference_day: date,
    ) -> Optional[List[int]]:
        """把明确的多周期问法转成作业序号。

        “前 N 次”指从课程开始的前 N 期；“这 N 次”和“技术周”
        课程进行中只取当前周期之前的已结束周期，课程结束后则包含最后一期。
        """
        phase = self.settings.course_phase_for_reference(
            reference_day,
            self._course_name_hint(question),
        )
        if phase is None:
            return None
        _, _, current_assignment = self.settings.assignment_cycle_in_phase(reference_day, phase)
        phase_ended = phase.end_day is not None and reference_day > phase.end_day
        last_completed = current_assignment if phase_ended else current_assignment - 1

        match = _ASSIGNMENT_RANGE.search(question)
        if match:
            start = self._parse_assignment_number(match.group("start"))
            end = self._parse_assignment_number(match.group("end"))
            if start < 1 or end < start or end > current_assignment or end - start >= 12:
                return []
            return list(range(start, end + 1))

        match = _FIRST_ASSIGNMENTS.search(question)
        if match:
            count = self._parse_assignment_number(match.group("count"))
            if count < 1 or count > 12 or last_completed < 1:
                return []
            return list(range(1, min(count, last_completed) + 1))

        match = _RECENT_ASSIGNMENTS.search(question)
        if match:
            count = self._parse_assignment_number(match.group("count"))
            if count < 1 or count > 12 or last_completed < 1:
                return []
            start = max(1, last_completed - count + 1)
            return list(range(start, last_completed + 1))

        if (
            _TECH_WEEK_OVERVIEW.search(question) or _VIDEO_WEEK_OVERVIEW.search(question)
        ) and not _ASSIGNMENT_NUMBER.search(question):
            if last_completed < 1:
                return []
            return list(range(1, last_completed + 1))
        return None

    def _multi_cycle_base_statuses(
        self,
        report_dates: Sequence[str],
    ) -> Dict[Tuple[str, str], str]:
        """只读多维表格的系统/人工状态，不回写任何字段。"""
        if not self.settings.base_sync_enabled:
            return {}
        wanted = set(report_dates)
        system_statuses = {
            "已提交": "completed",
            "补卡": "late",
            "待核验": "pending",
            "未提交": "missing",
        }
        statuses: Dict[Tuple[str, str], str] = {}
        for record in self.api.list_base_records(
            self.settings.base_token,
            self.settings.base_table_id,
        ):
            fields = record.get("fields") or {}
            record_key = self._base_cell_text(fields.get("记录键"))
            if "|" not in record_key:
                continue
            report_date = record_key.split("|", 1)[0]
            if report_date not in wanted:
                continue
            name = self._base_cell_text(fields.get("组员姓名"))
            if not name:
                continue
            manual_value = self._base_cell_text(fields.get("人工状态"))
            system_value = self._base_cell_text(fields.get("作业状态"))
            status = _MANUAL_ATTENDANCE_STATUS.get(manual_value) or system_statuses.get(
                system_value
            )
            if status:
                statuses[(report_date, name)] = status
        return statuses

    def _answer_multi_cycle_stats(
        self,
        question: str,
        reference_day: date,
    ) -> Optional[str]:
        assignment_numbers = self._multi_cycle_assignment_numbers(question, reference_day)
        if assignment_numbers is None:
            return None
        phase = self.settings.course_phase_for_reference(
            reference_day,
            self._course_name_hint(question),
        )
        if phase is None:
            return "本群还没有配置对应的作业周期。"
        if not assignment_numbers:
            _, _, current_assignment = self.settings.assignment_cycle_in_phase(reference_day, phase)
            return f"当前最多只能复查到第{current_assignment}次作业，没有匹配的多周期范围。"

        report_dates = [
            (
                phase.start_day + timedelta(days=(assignment_number - 1) * phase.cycle_days)
            ).isoformat()
            for assignment_number in assignment_numbers
        ]
        try:
            base_statuses = self._multi_cycle_base_statuses(report_dates)
        except Exception:
            logger.exception("多周期复查读取多维表格失败")
            return "多维表格当前读取失败，为避免用旧数据误报，本次没有给出复查结果。"

        normal_total = 0
        late_total = 0
        pending_total = 0
        missing_total = 0
        slot_total = 0
        status_history: Dict[str, List[str]] = defaultdict(list)
        lines = [
            f"📊 第{assignment_numbers[0]}—{assignment_numbers[-1]}次作业复查（只读）",
            "",
        ]
        if phase.end_day is not None and reference_day > phase.end_day:
            lines.extend(
                [
                    f"{phase.name}已于{phase.end_date}结束，"
                    f"本轮共{self.settings.total_assignment_cycles(reference_day, phase.name)}次作业。",
                    "",
                ]
            )
        available_cycles = 0
        for assignment_number, report_date in zip(assignment_numbers, report_dates):
            records = self.store.list_daily_attendance(report_date)
            status_by_name = {
                record.sender_name: record.homework_status
                for record in records
                if record.sender_name in self.settings.report_members
            }
            status_by_name.update(
                {
                    name: status
                    for (row_date, name), status in base_statuses.items()
                    if row_date == report_date and name in self.settings.report_members
                }
            )
            if not status_by_name:
                lines.extend([f"第{assignment_number}次：暂无可核验的已存打卡数据。", ""])
                continue

            available_cycles += 1
            active_names = [
                name
                for name in self.settings.report_members
                if status_by_name.get(name, "missing") != "excluded"
            ]
            normal = [name for name in active_names if status_by_name.get(name) == "completed"]
            late = [name for name in active_names if status_by_name.get(name) == "late"]
            pending = [name for name in active_names if status_by_name.get(name) == "pending"]
            missing = [
                name for name in active_names if status_by_name.get(name, "missing") == "missing"
            ]
            for name in self.settings.report_members:
                status_history[name].append(status_by_name.get(name, "missing"))

            normal_total += len(normal)
            late_total += len(late)
            pending_total += len(pending)
            missing_total += len(missing)
            slot_total += len(active_names)
            cycle_start, cycle_end, _ = self.settings.assignment_cycle(report_date)
            period = (
                f"{cycle_start.month}月{cycle_start.day}日—{cycle_end.month}月{cycle_end.day}日"
            )
            lines.extend(
                [
                    f"第{assignment_number}次（{period}）：正常 {len(normal)}｜补卡 {len(late)}｜"
                    + (f"待核验 {len(pending)}｜" if pending else "")
                    + f"最终 {len(normal) + len(late)}/{len(active_names)}｜"
                    f"未交 {len(missing)}",
                    f"正常：{self._names(normal)}",
                    f"补卡：{self._names(late)}",
                ]
            )
            if pending:
                lines.append(f"待核验：{self._names(pending)}")
            lines.extend([f"未交：{self._names(missing)}", ""])

        if not available_cycles:
            return "所选作业周期暂无可核验的已存打卡数据。"

        fully_completed = [
            name
            for name in self.settings.report_members
            if len(status_history[name]) == available_cycles
            and all(status in {"completed", "late"} for status in status_history[name])
        ]
        pending_summary = f"待核验 {pending_total}｜" if pending_total else ""
        lines.extend(
            [
                "多周期总览",
                f"累计作业人次：正常 {normal_total}｜补卡 {late_total}｜"
                f"{pending_summary}最终完成 {normal_total + late_total}/{slot_total}｜"
                f"未交 {missing_total}",
                f"所选周期全部完成（{len(fully_completed)}人）：{self._names(fully_completed)}",
                "",
                (
                    "数据来源：本地打卡记录 + 多维表格当前状态；本次查询没有修改任何表格数据。"
                    if self.settings.base_sync_enabled
                    else "数据来源：本地打卡记录；本次查询没有修改任何数据。"
                ),
            ]
        )
        return "\n".join(lines)

    def _named_messages(self, report_date: str, chat_id: str) -> List[StoredMessage]:
        messages = self._load_messages(report_date, chat_id)
        return self._apply_member_aliases(messages)

    def _apply_member_aliases(self, messages: Sequence[StoredMessage]) -> List[StoredMessage]:
        return [
            message
            if not (alias := self.settings.member_aliases.get(message.sender_open_id))
            else StoredMessage(
                message_id=message.message_id,
                chat_id=message.chat_id,
                sender_open_id=message.sender_open_id,
                sender_name=alias,
                message_type=message.message_type,
                content=message.content,
                create_time_ms=message.create_time_ms,
                parent_id=message.parent_id,
                root_id=message.root_id,
                thread_id=message.thread_id,
            )
            for message in messages
        ]

    def _assignment_window_messages(
        self,
        report_date: str,
        chat_id: str,
        cutoff_hour: int,
        cutoff_minute: int,
    ) -> List[StoredMessage]:
        cycle_start, _, _ = self.settings.assignment_cycle(report_date)
        report_date = cycle_start.isoformat()
        due_day = self.settings.assignment_deadline(report_date).date()
        window_start_day = cycle_start
        if not self.settings.configured_course_phases:
            window_start_day -= timedelta(days=1)
        due_hour, due_minute = self.settings.assignment_due_clock(report_date)
        start = self.settings.assignment_window_start(report_date)
        if not self.settings.configured_course_phases:
            start = datetime.combine(
                window_start_day,
                start.timetz().replace(tzinfo=None),
                tzinfo=self.settings.tz,
            )
        end = datetime.combine(
            due_day,
            time(cutoff_hour, cutoff_minute),
            tzinfo=self.settings.tz,
        )
        assignment_deadline = self.settings.assignment_deadline(report_date)
        if assignment_deadline > end and (
            cutoff_hour,
            cutoff_minute,
        ) == (due_hour, due_minute):
            end = assignment_deadline
        else:
            end = min(end, assignment_deadline)
        messages = self.store.list_messages(
            chat_id,
            int(start.timestamp() * 1000),
            int(end.timestamp() * 1000) + 1,
            limit=self.settings.max_messages,
        )
        messages = [
            message
            for message in messages
            if message.sender_open_id not in self.settings.excluded_member_ids
        ]
        thread_ids = {message.thread_id for message in messages if message.thread_id}
        roots = self.store.thread_roots(chat_id, thread_ids)
        filtered: List[StoredMessage] = []
        for message in messages:
            if not message.thread_id or message.thread_id not in roots:
                filtered.append(message)
                continue
            root = roots[message.thread_id]
            root_time = datetime.fromtimestamp(root.create_time_ms / 1000, tz=self.settings.tz)
            thread_report_date = self.settings.assignment_report_date(root_time.date())
            if thread_report_date == report_date:
                filtered.append(message)
        messages = filtered
        return self._apply_member_aliases(messages)

    def _post_deadline_messages(self, report_date: str, chat_id: str) -> List[StoredMessage]:
        report_date = self.settings.assignment_report_date(report_date)
        start_ms = int(self.settings.assignment_deadline(report_date).timestamp() * 1000) + 1
        end_ms = int(datetime.now(tz=self.settings.tz).timestamp() * 1000) + 1
        if start_ms >= end_ms:
            return []
        messages = self.store.list_messages(
            chat_id, start_ms, end_ms, limit=self.settings.max_messages
        )
        messages = [
            message
            for message in messages
            if message.sender_open_id not in self.settings.excluded_member_ids
        ]
        return self._apply_member_aliases(messages)

    @staticmethod
    def _normalize_iteration_label(value: str) -> str:
        return re.sub(r"\s+", "", value).upper()

    def _iteration_report_date(self, label: str, reference_day: date) -> str:
        normalized = self._normalize_iteration_label(label)
        if normalized.startswith("DAY"):
            return reference_day.isoformat()
        return self._query_report_date(normalized, reference_day)

    def _record_iteration(self, text: str, message: StoredMessage) -> bool:
        match = _ITERATION_MARKER.search(text)
        if not match:
            return False
        label = self._normalize_iteration_label(match.group("label"))
        completed = bool(_ITERATION_COMPLETE.search(text[match.end() :]))
        mentions = [
            name
            for name in _MENTION_NAME.findall(text)
            if name != self._bot_name and name in self.settings.report_members
        ]
        if completed:
            target_name = mentions[0] if mentions else message.sender_name
        elif mentions:
            target_name = mentions[0]
        else:
            return False
        if target_name not in self.settings.report_members:
            return False
        open_id_by_name = {name: open_id for open_id, name in self.settings.member_aliases.items()}
        member_key = open_id_by_name.get(target_name, f"name:{target_name}")
        event_day = datetime.fromtimestamp(
            message.create_time_ms / 1000, tz=self.settings.tz
        ).date()
        report_date = self._iteration_report_date(label, event_day)
        inserted = self.store.add_iteration_event(
            message_id=message.message_id,
            report_date=report_date,
            assignment_label=label,
            member_key=member_key,
            member_name=target_name,
            status="completed" if completed else "pending",
            actor_open_id=message.sender_open_id,
            actor_name=message.sender_name,
            event_time_ms=message.create_time_ms,
        )
        if inserted:
            logger.info("已记录迭代状态：%s %s %s", label, target_name, report_date)
            self.sync_attendance_date(report_date, message.chat_id)
        return True

    def _answer_iteration_question(self, question: str) -> Optional[str]:
        match = _ITERATION_MARKER.search(question)
        if not match:
            loose = re.search(r"(?<![A-Z0-9])(DAY\s*\d+)(?![A-Z0-9])", question, re.I)
            if not loose:
                return "请带上迭代批次，例如：@知识库助手 #迭代 DAY5 状态。"
            label = self._normalize_iteration_label(loose.group(1))
        else:
            label = self._normalize_iteration_label(match.group("label"))
        statuses = self.store.iteration_statuses(label)
        pending = [name for name in self.settings.report_members if statuses.get(name) == "pending"]
        completed = [
            name for name in self.settings.report_members if statuses.get(name) == "completed"
        ]
        return (
            f"{label} 迭代状态：\n"
            f"待迭代（{len(pending)}人）：{self._names(pending)}\n"
            f"已迭代（{len(completed)}人）：{self._names(completed)}"
        )

    def _leader_override_request(self, question: str) -> Optional[Tuple[Tuple[str, ...], str]]:
        """解析“成员A、成员B已补交”这类组长代改命令。"""
        status_match = _LEADER_OVERRIDE_STATUS.search(question)
        if status_match is None:
            return None
        prefix = question[: status_match.start()].strip()
        if not prefix:
            return None
        candidates: List[Tuple[int, str]] = []
        for name in sorted(self.settings.report_members, key=len, reverse=True):
            if name == "，":
                continue
            position = prefix.find(name)
            if position >= 0:
                candidates.append((position, name))
        compact = prefix.lstrip("帮把将 ")
        comma_target = re.search(r"(?:^|[、,])\s*@?(，)\s*$", compact)
        if comma_target:
            candidates.append((comma_target.start(1), "，"))
        if not candidates:
            return None
        target_names = tuple(name for _, name in sorted(candidates))
        raw_status = status_match.group("status")
        if raw_status in {"未提交", "没交", "未完成"}:
            status = "missing"
        elif "补" in raw_status:
            status = "late"
        else:
            status = "completed"
        return target_names, status

    def _semantic_leader_override_request(
        self,
        question: str,
        reference_day: date,
    ) -> Optional[Tuple[Tuple[str, ...], str, str]]:
        """用模型理解灵活说法，但只接受通过白名单校验的代改结果。"""
        interpreter = getattr(self.summarizer, "interpret_leader_override", None)
        if not callable(interpreter):
            return None
        try:
            parsed = interpreter(question, self.settings.report_members)
        except Exception:
            logger.exception("MiniMax 组长指令语义解析失败")
            return None
        if not isinstance(parsed, dict):
            return None
        target_names = parsed.get("targets")
        status = parsed.get("status")
        assignment_number = parsed.get("assignment_number")
        if (
            not isinstance(target_names, tuple)
            or not target_names
            or status not in {"completed", "late", "missing"}
        ):
            return None
        if any(name not in self.settings.report_members for name in target_names):
            return None
        normalized_question = question
        if assignment_number is not None:
            _, _, current_assignment = self.settings.assignment_cycle(reference_day)
            if (
                isinstance(assignment_number, bool)
                or not isinstance(assignment_number, int)
                or assignment_number < 1
                or assignment_number > current_assignment
            ):
                return None
            normalized_question = f"第{assignment_number}次作业 {question}"
        return target_names, str(status), normalized_question

    def _answer_table_link(self) -> str:
        if not self.settings.report_link:
            return "本群还没有配置打卡表链接。"
        return f"本群打卡表：{self.settings.report_link}"

    @staticmethod
    def _format_clock(hour: int, minute: int) -> str:
        return f"{hour:02d}:{minute:02d}"

    def _answer_reminder_explanation(self) -> str:
        due_actions: List[str] = []
        if self.settings.reminder_enabled:
            due_actions.append(
                f"{self._format_clock(self.settings.reminder_hour, self.settings.reminder_minute)} @ 尚未提交的成员"
            )
        if self.settings.missing_list_enabled:
            due_actions.append(
                f"{self._format_clock(self.settings.missing_list_hour, self.settings.missing_list_minute)} 发未交名单"
            )
        if self.settings.final_status_enabled:
            due_actions.append(
                f"{self._format_clock(self.settings.final_status_hour, self.settings.final_status_minute)} 发完成/未完成汇总"
            )

        makeup_actions: List[str] = []
        if self.settings.makeup_reminder_enabled:
            makeup_actions.append(
                f"{self._format_clock(self.settings.makeup_reminder_hour, self.settings.makeup_reminder_minute)} @ 仍未补交的成员"
            )
        if self.settings.makeup_summary_enabled:
            makeup_actions.append(
                f"{self._format_clock(self.settings.makeup_summary_hour, self.settings.makeup_summary_minute)} 发补交汇总"
            )

        lines = ["这是机器人按当前作业周期自动发的催交，不是某个人手动点名。"]
        today = datetime.now(tz=self.settings.tz).date()
        if phase := self.settings.course_phase(today):
            if phase.due_day_offset is not None:
                due_day_label = (
                    "当日"
                    if phase.due_day_offset == 0
                    else "次日"
                    if phase.due_day_offset == 1
                    else f"第{phase.due_day_offset + 1}天"
                )
            else:
                due_day_label = f"第{phase.cycle_days}天"
            lines.append(
                f"当前为{phase.name}：每{phase.cycle_days}天一次作业，"
                f"{self._format_clock(phase.publish_hour, phase.publish_minute)}开始，"
                f"{due_day_label}{self._format_clock(phase.due_hour, phase.due_minute)}正常截止。"
            )
        if due_actions:
            lines.append("截止日：" + "；".join(due_actions) + "。")
        if makeup_actions:
            lines.append("补交阶段：" + "；".join(makeup_actions) + "。")
        lines.append("名单根据群内已识别的提交和打卡表状态生成；请假或误判可由组长在表里修正。")
        return "\n".join(lines)

    def _answer_menu(self) -> str:
        menu = (
            "🎮 作业助教菜单\n\n"
            "@ 我后发送数字即可：\n"
            "1  本次作业进度\n"
            "2  谁还没交\n"
            "3  我的战绩\n"
            "4  补卡情况\n"
            "5  本周成长卡\n"
            "6  打开打卡表\n"
            "7  前三次作业复查\n\n"
            "也可直接说：“第三次还有谁掉队了”。\n"
            "交作业时加 #求反馈，我会根据文字给三行点评。"
        )
        phases = self.settings.configured_course_phases
        if not phases:
            return menu
        today = datetime.now(tz=self.settings.tz).date()
        course_lines: List[str] = []
        for phase in phases:
            if phase.end_day is not None and today > phase.end_day:
                total = self.settings.total_assignment_cycles(today, phase.name)
                course_lines.append(
                    f"{phase.name}：{phase.start_date}—{phase.end_date}（已结束，共{total}次作业）"
                )
            elif phase.contains(today):
                course_lines.append(
                    f"{phase.name}：{phase.start_date}起进行中，每{phase.cycle_days}天一次作业"
                )
            else:
                course_lines.append(f"{phase.name}：{phase.start_date}起开始")
        return f"{menu}\n\n" + "\n".join(course_lines) + "。"

    def _answer_my_stats(self, message: StoredMessage, reference_day: date) -> str:
        member_name = message.sender_name
        if member_name not in self.settings.report_members:
            return "你不在本群的打卡名单中，暂时没有个人战绩。"
        current_report_date = self.settings.assignment_report_date(reference_day)
        try:
            self.sync_attendance_date(current_report_date, message.chat_id)
        except Exception:
            logger.exception("查询个人战绩前刷新打卡状态失败")
        records = [
            record
            for record in self.store.list_member_attendance(
                message.sender_open_id,
                member_name,
                reference_day.isoformat(),
            )
            if record.homework_status != "excluded"
            and self._attendance_record_is_in_scope(record.report_date)
            and not (
                record.homework_status == "missing"
                and self.settings.assignment_deadline(record.report_date).date() > reference_day
            )
        ]
        if not records:
            return f"{member_name}还没有可查询的打卡记录。"

        normal = sum(record.homework_status == "completed" for record in records)
        late = sum(record.homework_status == "late" for record in records)
        pending = sum(record.homework_status == "pending" for record in records)
        missing = sum(record.homework_status == "missing" for record in records)
        reviewed = sum(record.review_status == "completed" for record in records)
        normal_streak = 0
        for record in reversed(records):
            if record.homework_status != "completed":
                break
            normal_streak += 1
        current = next(
            (record for record in reversed(records) if record.report_date == current_report_date),
            records[-1],
        )
        status_label = {
            "completed": "✅ 正常提交",
            "late": "🟡 补卡",
            "pending": "🟠 待核验",
            "missing": "❌ 未提交",
        }.get(current.homework_status, current.homework_status)
        review_label = "✅ 已复盘" if current.review_status == "completed" else "❌ 未复盘"
        badges: List[str] = []
        if normal >= 3:
            badges.append("🎯 稳定交付")
        if normal_streak >= 3:
            badges.append("🔥 连续准时")
        if reviewed >= 3:
            badges.append("📝 复盘连击")
        if late:
            badges.append("🟡 补卡归队")
        pending_summary = f"｜待核验 {pending} 次" if pending else ""
        return (
            f"🎮 {member_name}・我的战绩\n\n"
            f"累计：正常 {normal} 次｜补卡 {late} 次{pending_summary}｜"
            f"未交 {missing} 次\n"
            f"复盘：{reviewed}/{len(records)}｜连续正常提交 {normal_streak} 次\n\n"
            f"最近一次：{status_label}｜{review_label}\n"
            f"徽章：{self._names(badges)}"
        )

    def _attendance_record_is_in_scope(self, report_date: str) -> bool:
        """保留课程阶段内记录，并兼容第一阶段开始前的旧历史数据。"""
        if self.settings.assignment_is_paused(report_date):
            return False
        phases = self.settings.configured_course_phases
        if not phases:
            return True
        day = datetime.strptime(report_date, "%Y-%m-%d").date()
        if day < phases[0].start_day:
            return True
        return any(phase.contains(day) for phase in phases)

    def _answer_my_missing_details(
        self,
        message: StoredMessage,
        reference_day: date,
    ) -> str:
        member_name = message.sender_name
        if member_name not in self.settings.report_members:
            return "你不在本群的打卡名单中，暂时没有个人记录。"
        current_report_date = self.settings.assignment_report_date(reference_day)
        try:
            self.sync_attendance_date(current_report_date, message.chat_id)
        except Exception:
            logger.exception("查询个人未交作业前刷新打卡状态失败")
        records = [
            record
            for record in self.store.list_member_attendance(
                message.sender_open_id,
                member_name,
                reference_day.isoformat(),
            )
            if record.homework_status == "missing"
            and self._attendance_record_is_in_scope(record.report_date)
            and self.settings.assignment_deadline(record.report_date).date() <= reference_day
        ]
        if not records:
            return f"{member_name}目前没有未交作业。"
        lines = [f"{member_name}未交作业（{len(records)}次）："]
        for record in records:
            lines.append(f"• {self._assignment_period_label(record.report_date)}")
        return "\n".join(lines)

    def _answer_weekly_growth(self, reference_day: date, chat_id: str) -> str:
        phase = self.settings.course_phase_for_reference(reference_day)
        if phase is None:
            return "本群还没有配置作业周期，暂时无法生成成长卡。"
        course_start = phase.start_day
        window_start = max(course_start, reference_day - timedelta(days=6))
        cycle_day = datetime.strptime(
            self.settings.assignment_report_date(window_start),
            "%Y-%m-%d",
        ).date()
        current_cycle = datetime.strptime(
            self.settings.assignment_report_date(reference_day),
            "%Y-%m-%d",
        ).date()
        try:
            self.sync_attendance_date(current_cycle.isoformat(), chat_id)
        except Exception:
            logger.exception("生成本周成长卡前刷新打卡状态失败")
        cycles: List[Tuple[str, List[AttendanceRecord]]] = []
        while cycle_day <= current_cycle:
            report_date = cycle_day.isoformat()
            records = self.store.list_daily_attendance(report_date)
            if records:
                cycles.append((report_date, records))
            cycle_day += timedelta(days=phase.cycle_days)
        if not cycles:
            return "本周还没有可用的打卡数据。"

        statuses: Dict[str, List[str]] = defaultdict(list)
        reviews: Dict[str, List[str]] = defaultdict(list)
        for _, records in cycles:
            for record in records:
                if (
                    record.sender_name not in self.settings.report_members
                    or record.homework_status == "excluded"
                ):
                    continue
                statuses[record.sender_name].append(record.homework_status)
                reviews[record.sender_name].append(record.review_status)
        cycle_count = len(cycles)
        on_time = [
            name
            for name in self.settings.report_members
            if len(statuses[name]) == cycle_count
            and all(status == "completed" for status in statuses[name])
        ]
        full_attendance = [
            name
            for name in self.settings.report_members
            if len(statuses[name]) == cycle_count
            and all(status in {"completed", "late"} for status in statuses[name])
        ]
        review_streak = [
            name
            for name in self.settings.report_members
            if len(reviews[name]) == cycle_count
            and all(status == "completed" for status in reviews[name])
        ]
        makeup_returned = [
            name
            for name in self.settings.report_members
            if any(status == "late" for status in statuses[name])
        ]
        completed_count = sum(
            status in {"completed", "late"} for values in statuses.values() for status in values
        )
        total = sum(len(values) for values in statuses.values())
        start_day = datetime.strptime(cycles[0][0], "%Y-%m-%d").date()
        end_day = datetime.strptime(cycles[-1][0], "%Y-%m-%d").date()
        return (
            "🌟 本周成长卡\n\n"
            f"统计周期：{start_day.month}月{start_day.day}日—{end_day.month}月{end_day.day}日\n"
            f"作业完成：{completed_count}/{total}\n\n"
            f"🎯 全部准时：{self._names(on_time)}\n"
            f"👣 本周全勤：{self._names(full_attendance)}\n"
            f"📝 复盘连击：{self._names(review_streak)}\n"
            f"🟡 补卡归队：{self._names(makeup_returned)}"
        )

    def _semantic_query_answer(
        self,
        question: str,
        reference_day: date,
        chat_id: str,
    ) -> Optional[Tuple[str, bool]]:
        interpreter = getattr(self.summarizer, "interpret_query", None)
        if not callable(interpreter):
            return None
        try:
            parsed = interpreter(question, self.settings.report_members)
        except Exception:
            logger.exception("MiniMax 统计查询语义解析失败")
            return None
        if not isinstance(parsed, dict):
            return None
        assignment_number = parsed.get("assignment_number")
        if assignment_number is not None:
            _, _, current_assignment = self.settings.assignment_cycle(reference_day)
            if (
                isinstance(assignment_number, bool)
                or not isinstance(assignment_number, int)
                or assignment_number < 1
                or assignment_number > current_assignment
            ):
                return None
        if parsed.get("intent") == "member_history":
            target = parsed.get("target")
            if target not in self.settings.report_members or target == "，":
                return None
            return self._answer_member_history(
                f"查询{target}全部打卡记录",
                reference_day,
            ), True
        if parsed.get("intent") != "attendance_query":
            return None
        prefix = f"第{assignment_number}次" if assignment_number else ""
        topic = "复盘" if parsed.get("topic") == "review" else "作业"
        mode = parsed.get("mode")
        suffix = "谁没完成" if mode == "missing" else "谁完成了" if mode == "completed" else "情况"
        answer = self._answer_stats_question(
            f"{prefix}{topic}{suffix}",
            reference_day,
            chat_id,
        )
        return (answer, False) if answer is not None else None

    def _maybe_answer_feedback(
        self,
        message: StoredMessage,
        report_dates: Sequence[str],
    ) -> bool:
        if (
            not self.settings.send_enabled
            or not report_dates
            or message.sender_name not in self.settings.report_members
        ):
            return False
        feedback = getattr(self.summarizer, "feedback_homework", None)
        if not callable(feedback):
            return False
        homework_text = _FEEDBACK_REQUEST.sub("", message.content).strip()
        if len(homework_text) < 20:
            reply = (
                "已收到 #求反馈，但当前只有很少可读文字。"
                "补充作业说明、收获或卡点后，我才能给具体反馈。"
            )
        else:
            try:
                reply = feedback(message.sender_name, homework_text)
            except Exception:
                logger.exception("MiniMax 作业反馈生成失败：%s", message.message_id)
                reply = "这次反馈生成没有通过格式校验，作业打卡已正常记录。"
        self.api.reply_post(
            message.message_id,
            reply,
            f"homework-feedback-{message.message_id}",
        )
        return True

    def _social_chat_context(self, message: StoredMessage) -> List[str]:
        timeline: List[Tuple[int, str]] = []
        for recent in self.store.list_recent_messages(
            message.chat_id,
            message.create_time_ms,
            limit=12,
        ):
            content = " ".join(recent.content.split())[:300]
            if content:
                timeline.append((recent.create_time_ms, f"{recent.sender_name}：{content}"))
        for action in self.store.list_recent_social_chat_actions(
            message.chat_id,
            message.create_time_ms,
            limit=3,
        ):
            if action.get("action") == "reply" and action.get("response"):
                timeline.append((int(action["event_time_ms"]) + 1, f"助教：{action['response']}"))
        return [line for _, line in sorted(timeline)[-12:]]

    def _maybe_social_chat(
        self,
        message: StoredMessage,
        *,
        prompt_text: str,
        direct: bool,
    ) -> bool:
        if not self.settings.send_enabled or not self.settings.social_chat_enabled:
            return False
        if message.message_type not in {"text", "post"}:
            return False
        if self.store.social_chat_action_sent(message.message_id):
            return False
        if not direct:
            if not self.settings.social_chat_proactive_enabled:
                return False
            sent_at = datetime.fromtimestamp(message.create_time_ms / 1000, tz=self.settings.tz)
            if sent_at.hour >= 23 or sent_at.hour < 8:
                return False
            if _SOCIAL_SKIP_SIGNAL.search(prompt_text):
                return False
            if _STATS_TOPIC.search(prompt_text) and _EXPLICIT_STATS_QUERY_SIGNAL.search(
                prompt_text
            ):
                return False
            if not (
                _SOCIAL_PROACTIVE_SIGNAL.search(prompt_text)
                or _SOCIAL_WORK_DISCUSSION_SIGNAL.search(prompt_text)
            ):
                return False
            cooldown_ms = self.settings.social_chat_cooldown_minutes * 60 * 1000
            last_action_ms = self.store.last_social_chat_action_ms(message.chat_id)
            if last_action_ms and message.create_time_ms - last_action_ms < cooldown_ms:
                return False
            if (
                self.store.social_chat_action_count(
                    message.chat_id,
                    message.create_time_ms - 60 * 60 * 1000,
                )
                >= self.settings.social_chat_hourly_limit
            ):
                return False

        decider = getattr(self.summarizer, "decide_social_response", None)
        if not callable(decider):
            return False
        try:
            decision = decider(
                message.sender_name,
                prompt_text,
                self._social_chat_context(message),
                direct=direct,
            )
        except Exception:
            logger.exception("MiniMax 群聊参与决策失败：%s", message.message_id)
            return False
        if not isinstance(decision, dict):
            return False
        action = decision.get("action")
        confidence = decision.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return False
        threshold = 0.50 if direct else 0.85 if action == "react" else 0.68
        if float(confidence) < threshold or action == "silent":
            return False

        response = ""
        outbound_message_id = ""
        try:
            if action == "reply":
                response = str(decision.get("reply") or "").strip()
                if not response or _SOCIAL_FORBIDDEN_REPLY.search(response):
                    return False
                outbound_message_id = (
                    self.api.reply_text(
                        message.message_id,
                        response,
                        f"social-chat-{message.message_id}",
                    )
                    or ""
                )
            elif action == "react":
                response = str(decision.get("emoji") or "")
                if not response:
                    return False
                self.api.add_reaction(message.message_id, response)
            else:
                return False
        except Exception:
            logger.exception("MiniMax 群聊回应发送失败：%s", message.message_id)
            return False
        self.store.mark_social_chat_action(
            message_id=message.message_id,
            chat_id=message.chat_id,
            action=str(action),
            response=response,
            outbound_message_id=outbound_message_id,
            event_time_ms=message.create_time_ms,
        )
        logger.info(
            "群助教已克制参与：chat=%s message=%s action=%s",
            message.chat_id,
            message.message_id,
            action,
        )
        return True

    def _answer_leader_attendance_override(
        self,
        *,
        target_names: Sequence[str],
        status: str,
        question: str,
        reference_day: date,
        message: StoredMessage,
    ) -> str:
        if message.sender_open_id not in self.settings.leader_member_ids:
            return "只有本群已配置的组长可以代替其他成员修改作业状态。"
        if not self.settings.base_sync_enabled:
            return "本群尚未启用多维表格同步，暂时不能修改作业状态。"
        if notice := self._course_assignment_limit_notice(question, reference_day):
            return notice

        requested_day = self._query_report_date(question, reference_day)
        report_date = self.settings.assignment_report_date(requested_day)
        has_explicit_period = bool(
            _ASSIGNMENT_NUMBER.search(question)
            or _QUERY_FULL_DATE.search(question)
            or _QUERY_MONTH_DAY.search(question)
            or _QUERY_MMDD.search(question)
            or "昨天" in question
            or "前天" in question
        )
        if status == "late" and not has_explicit_period:
            submitted_at = datetime.fromtimestamp(
                message.create_time_ms / 1000,
                tz=self.settings.tz,
            )
            current_report_date = self.settings.assignment_report_date(reference_day)
            if submitted_at < self.settings.assignment_deadline(current_report_date):
                current_cycle_start, _, _ = self.settings.assignment_cycle(current_report_date)
                report_date = (
                    current_cycle_start
                    - timedelta(days=self.settings.assignment_cycle_days_for(current_report_date))
                ).isoformat()
        self.sync_attendance_date(report_date, message.chat_id)
        records = self.api.list_base_records(
            self.settings.base_token,
            self.settings.base_table_id,
        )
        key_prefix = f"{report_date}|"
        records_by_name = {
            self._base_cell_text((item.get("fields") or {}).get("组员姓名")): item
            for item in records
            if str((item.get("fields") or {}).get("记录键") or "").startswith(key_prefix)
            and item.get("record_id")
        }
        missing_names = [name for name in target_names if name not in records_by_name]
        if missing_names:
            return (
                f"没有找到{self._names(missing_names)}在"
                f"{self._assignment_period_label(report_date)}的表格记录。"
            )

        manual_values = {
            "completed": "正常提交",
            "late": "补卡",
            "missing": "未提交",
        }
        manual_value = manual_values[status]
        for target_name in target_names:
            self.api.update_base_record(
                self.settings.base_token,
                self.settings.base_table_id,
                str(records_by_name[target_name]["record_id"]),
                {"人工状态": manual_value},
            )
        open_id_by_name = {name: open_id for open_id, name in self.settings.member_aliases.items()}
        member_keys = [open_id_by_name.get(name, f"name:{name}") for name in target_names]
        self.store.add_attendance_override(
            message_id=message.message_id,
            report_date=report_date,
            member_key="、".join(member_keys),
            member_name="、".join(target_names),
            status=status,
            actor_open_id=message.sender_open_id,
            actor_name=message.sender_name,
            event_time_ms=message.create_time_ms,
        )
        self.sync_attendance_date(report_date, message.chat_id)
        return (
            f"已由组长{message.sender_name}把{self._names(target_names)}的"
            f"{self._assignment_period_label(report_date)}标记为{manual_value}。\n"
            "数据库和多维表格已同步。"
        )

    def _answer_stats_question(
        self, question: str, reference_day: date, chat_id: str
    ) -> Optional[str]:
        if not _STATS_TOPIC.search(question):
            return None
        if notice := self._course_assignment_limit_notice(question, reference_day):
            return notice
        if "迭代" in question:
            return self._answer_iteration_question(question)
        if _MEMBER_HISTORY_INTENT.search(question):
            return self._answer_member_history(question, reference_day)
        requested_date = self._query_report_date(question, reference_day)
        if self.settings.course_has_ended(requested_date):
            phase = self.settings.course_phase_for_reference(
                requested_date,
                self._course_name_hint(question),
            )
            if phase is not None and phase.end_date:
                requested_date = phase.end_date
        report_date = self.settings.assignment_report_date(requested_date)
        messages = self._named_messages(requested_date, chat_id)
        due_hour, due_minute = self.settings.assignment_due_clock(report_date)
        homework_messages = self._assignment_window_messages(
            report_date,
            chat_id,
            due_hour,
            due_minute,
        )
        late_messages = self._post_deadline_messages(report_date, chat_id)
        if not messages and not homework_messages and not late_messages:
            day = datetime.strptime(report_date, "%Y-%m-%d").date()
            return f"{day.month}月{day.day}日还没有收到可统计的群消息。"

        facts = self._completion_facts(report_date, messages, homework_messages=homework_messages)
        self._persist_attendance(report_date, homework_messages or late_messages or messages, facts)
        roster = facts["roster"]
        total = len(roster)
        day_label = self._assignment_period_label(report_date)
        phase = self.settings.course_phase_for_reference(
            report_date,
            self._course_name_hint(question),
        )
        if phase is not None and phase.end_day is not None and reference_day > phase.end_day:
            day_label = f"{phase.name}已于{phase.end_date}结束；以下为历史记录。\n{day_label}"
        wants_review = "复盘" in question
        wants_homework = bool(re.search(r"作业|打卡|提交|没交|未交", question))
        if wants_review and not wants_homework:
            completed = facts["review_members"]
            label = "复盘"
        else:
            completed = facts["homework_members"]
            label = facts["assignment_label"]

        completed_set = set(completed)
        pending = (
            [] if wants_review and not wants_homework else list(facts.get("pending_members", ()))
        )
        pending_set = set(pending)
        missing = [name for name in roster if name not in completed_set and name not in pending_set]
        if not (wants_review and not wants_homework):
            late = list(facts["late_members"])
            late_set = set(late)
            normal = [name for name in completed if name not in late_set]
            if _COMPLETED_INTENT.search(question) and not _MISSING_INTENT.search(question):
                reply = (
                    f"{day_label}{label}已提交 {len(completed)}/{total}。\n"
                    f"正常提交（{len(normal)}人）：{self._names(normal)}\n"
                    f"已补交（{len(late)}人）：{self._names(late)}"
                )
                if pending:
                    reply += f"\n待核验（{len(pending)}人）：{self._names(pending)}"
                return reply
            reply = (
                f"{day_label}{label}已提交 {len(completed)}/{total}"
                f"（正常提交 {len(normal)}，已补交 {len(late)}）。\n"
                f"已补交（{len(late)}人）：{self._names(late)}"
            )
            if pending:
                reply += f"\n待核验（{len(pending)}人）：{self._names(pending)}"
            return reply + f"\n仍未交（{len(missing)}人）：{self._names(missing)}"
        if _COMPLETED_INTENT.search(question) and not _MISSING_INTENT.search(question):
            return (
                f"{day_label}{label}已完成 {len(completed)}/{total}。\n"
                f"完成人员（{len(completed)}人）：{self._names(completed)}"
            )
        return (
            f"{day_label}{label}已完成 {len(completed)}/{total}。\n"
            f"未完成（{len(missing)}人）：{self._names(missing)}"
        )

    def _self_makeup_report_date(self, question: str, reference_day: date) -> Optional[str]:
        if _EXPLICIT_ASSIGNMENT_REFERENCE.search(question):
            requested = self._query_report_date(question, reference_day)
            return self.settings.assignment_report_date(requested)
        if self.settings.is_makeup_day(reference_day):
            return self.settings.makeup_report_date(reference_day)
        return None

    @staticmethod
    def _is_makeup_verification_request(question: str) -> bool:
        """@机器人后的补交陈述默认归属于真实发送人；查询句仍走统计。"""
        return bool(_MAKEUP_DECLARATION.search(question)) and not bool(
            _MAKEUP_QUERY_INTENT.search(question)
        )

    def _message_targets_assignment(
        self,
        message: StoredMessage,
        *,
        report_date: str,
        claim_message: StoredMessage,
        next_assignment_publish_ms: int,
        thread_root: Optional[StoredMessage],
    ) -> bool:
        message_day = datetime.fromtimestamp(
            message.create_time_ms / 1000, tz=self.settings.tz
        ).date()
        explicit_dates = self._submission_report_dates(
            message.content,
            message_day,
            allow_embedded=(
                message.message_type == "merge_forward"
                and not message.content.startswith(_MULTI_MERGE_PREFIX)
            ),
        )
        if explicit_dates:
            return report_date in explicit_dates

        assignment_match = _ASSIGNMENT_NUMBER.search(message.content)
        if assignment_match and self.settings.configured_course_phases:
            assignment_number = self._parse_assignment_number(assignment_match.group("number"))
            target = self.settings.assignment_date_for_number(
                assignment_number,
                message_day,
                self._course_name_hint(message.content),
            )
            if target is not None:
                return target.isoformat() == report_date

        if thread_root is not None:
            root_day = datetime.fromtimestamp(
                thread_root.create_time_ms / 1000, tz=self.settings.tz
            ).date()
            root_dates = self._submission_report_dates(
                thread_root.content,
                root_day,
                allow_embedded=(
                    thread_root.message_type == "merge_forward"
                    and not thread_root.content.startswith(_MULTI_MERGE_PREFIX)
                ),
            )
            if root_dates:
                return report_date in root_dates
            root_assignment = _ASSIGNMENT_NUMBER.search(thread_root.content)
            if root_assignment:
                return self._query_report_date(root_assignment.group(0), root_day) == report_date
            if self.settings.assignment_report_date(root_day) == report_date:
                return True

        if message.message_id == claim_message.message_id:
            return True
        if message.create_time_ms < next_assignment_publish_ms:
            return True
        return abs(message.create_time_ms - claim_message.create_time_ms) <= _LATE_TAG_WINDOW_MS

    def _homework_evidence_kind(
        self,
        message: StoredMessage,
        *,
        strict: bool = False,
    ) -> str:
        if message.message_type == "image" or "[图片]" in message.content:
            return "作业图片"
        if (
            message.message_type in {"file", "media", "merge_forward"}
            or "[文件]" in message.content
            or "[视频]" in message.content
        ) and not message.content.startswith(_MULTI_MERGE_PREFIX):
            return "作业文件"
        if _WEB_LINK.search(message.content) and (
            _HOMEWORK_EVIDENCE_CONTEXT.search(message.content)
            or _HOMEWORK_EVIDENCE_DETAIL.search(message.content)
        ):
            return "作业链接"
        if _TEXT_ARTIFACT_FIELD.search(message.content):
            return "文字作品正文"
        if strict:
            return ""
        if _HOMEWORK_EVIDENCE_DETAIL.search(message.content):
            return "完整作业正文"
        if self._is_thread_homework(message):
            return "话题作业"
        return ""

    def _requires_submission_artifact(self, report_date: str) -> bool:
        """视频周的“已完成/已提交”只是声明，必须有真实作品证据。"""
        phase = self.settings.course_phase(report_date)
        return bool(phase and "视频" in phase.name)

    def _submission_declarations(
        self,
        messages: Sequence[StoredMessage],
    ) -> List[Tuple[StoredMessage, str]]:
        declarations: List[Tuple[StoredMessage, str]] = []
        for message in messages:
            message_day = datetime.fromtimestamp(
                message.create_time_ms / 1000,
                tz=self.settings.tz,
            ).date()
            allow_embedded = (
                message.message_type == "merge_forward"
                and not message.content.startswith(_MULTI_MERGE_PREFIX)
            )
            for report_date in self._submission_report_dates(
                message.content,
                message_day,
                allow_embedded=allow_embedded,
            ):
                declarations.append((message, report_date))
        return declarations

    def _associated_artifact_messages(
        self,
        declaration: StoredMessage,
        report_date: str,
        messages: Sequence[StoredMessage],
        declarations: Sequence[Tuple[StoredMessage, str]],
    ) -> List[StoredMessage]:
        """把分开发送的作品归给最近的同人作业声明，避免一份作品跨期重复计数。"""
        artifacts: List[StoredMessage] = []
        for candidate in messages:
            if candidate.sender_open_id != declaration.sender_open_id:
                continue
            if candidate.content.startswith(_MULTI_MERGE_PREFIX):
                continue
            if not self._homework_evidence_kind(candidate, strict=True):
                continue

            candidate_day = datetime.fromtimestamp(
                candidate.create_time_ms / 1000,
                tz=self.settings.tz,
            ).date()
            allow_embedded = candidate.message_type == "merge_forward"
            explicit_dates = self._submission_report_dates(
                candidate.content,
                candidate_day,
                allow_embedded=allow_embedded,
            )
            if explicit_dates:
                if report_date in explicit_dates:
                    artifacts.append(candidate)
                continue

            if (
                declaration.thread_id
                and candidate.thread_id
                and declaration.thread_id == candidate.thread_id
            ):
                artifacts.append(candidate)
                continue

            nearby = [
                (other, other_report_date)
                for other, other_report_date in declarations
                if other.sender_open_id == candidate.sender_open_id
                and abs(other.create_time_ms - candidate.create_time_ms) <= _LATE_TAG_WINDOW_MS
            ]
            if not nearby:
                continue
            nearest, nearest_report_date = min(
                nearby,
                key=lambda item: (
                    abs(item[0].create_time_ms - candidate.create_time_ms),
                    item[0].create_time_ms,
                    item[0].message_id,
                ),
            )
            if nearest.message_id == declaration.message_id and nearest_report_date == report_date:
                artifacts.append(candidate)
        return list({message.message_id: message for message in artifacts}.values())

    def _find_self_makeup_evidence(
        self,
        report_date: str,
        claim_message: StoredMessage,
    ) -> Tuple[List[StoredMessage], str]:
        cycle_start, _, _ = self.settings.assignment_cycle(report_date)
        start = self.settings.assignment_window_start(report_date)
        makeup_end = self.settings.makeup_deadline(report_date)
        candidates = self.store.list_messages(
            claim_message.chat_id,
            int(start.timestamp() * 1000),
            int(makeup_end.timestamp() * 1000),
            self.settings.max_messages,
        )
        candidates = [
            message
            for message in candidates
            if message.sender_open_id == claim_message.sender_open_id
        ]
        thread_ids = {message.thread_id for message in candidates if message.thread_id}
        roots = self.store.thread_roots(claim_message.chat_id, thread_ids)
        next_cycle_start = cycle_start + timedelta(
            days=self.settings.assignment_cycle_days_for(report_date)
        )
        next_publish_hour, next_publish_minute = self.settings.assignment_publish_clock(
            next_cycle_start
        )
        next_publish = datetime.combine(
            next_cycle_start,
            time(next_publish_hour, next_publish_minute),
            tzinfo=self.settings.tz,
        )
        evidence: List[StoredMessage] = []
        kinds: List[str] = []
        for message in candidates:
            kind = self._homework_evidence_kind(message)
            if not kind:
                continue
            if not self._message_targets_assignment(
                message,
                report_date=report_date,
                claim_message=claim_message,
                next_assignment_publish_ms=int(next_publish.timestamp() * 1000),
                thread_root=roots.get(message.thread_id),
            ):
                continue
            evidence.append(message)
            kinds.append(kind)
        evidence.sort(key=lambda item: item.create_time_ms)
        return evidence, "、".join(dict.fromkeys(kinds))

    def _answer_self_makeup_verification(
        self,
        question: str,
        reference_day: date,
        message: StoredMessage,
    ) -> str:
        if message.sender_name not in self.settings.report_members:
            return "你不在本群的打卡名单中，无法登记作业状态。"
        if notice := self._course_assignment_limit_notice(question, reference_day):
            return notice
        report_date = self._self_makeup_report_date(question, reference_day)
        if report_date is None:
            return "请说明是第几次作业，例如：@知识库助手 补交第2次作业。"

        evidence, evidence_kind = self._find_self_makeup_evidence(report_date, message)
        _, _, assignment_number = self.settings.assignment_cycle(report_date)
        if not evidence:
            return (
                f"已收到第{assignment_number}次作业的核验请求，但没有找到你本人的"
                "作业链接、图片、文件或完整作业正文，当前状态没有修改。"
                "请先发送作业内容，再@我核验。"
            )

        evidence_time_ms = evidence[0].create_time_ms
        deadline = self.settings.assignment_deadline(report_date)
        status = "completed" if evidence_time_ms <= int(deadline.timestamp() * 1000) else "late"
        self.store.save_homework_verification(
            report_date=report_date,
            member_key=message.sender_open_id,
            sender_open_id=message.sender_open_id,
            sender_name=message.sender_name,
            claim_message_id=message.message_id,
            status=status,
            evidence_message_ids=[item.message_id for item in evidence],
            evidence_time_ms=evidence_time_ms,
        )
        self.sync_attendance_date(report_date, message.chat_id)

        submitted_at = datetime.fromtimestamp(evidence_time_ms / 1000, tz=self.settings.tz)
        result = "正常提交" if status == "completed" else "补卡"
        timing_note = (
            "按实际提交时间判定，即使消息中写了“补交”也不改为补卡"
            if status == "completed"
            else f"正常截止时间为{deadline.month}月{deadline.day}日 {deadline:%H:%M}"
        )
        sync_note = (
            "数据库已刷新，多维表格已触发同步。"
            if self.settings.base_sync_enabled
            else "数据库已刷新。"
        )
        return (
            f"已核验：{message.sender_name}于{submitted_at.month}月{submitted_at.day}日 "
            f"{submitted_at:%H:%M}提交第{assignment_number}次作业。\n"
            f"判定结果：{result}（{timing_note}）。\n"
            f"核验依据：{evidence_kind}。{sync_note}"
        )

    def _assignment_period_label(self, report_date: str) -> str:
        cycle_start, cycle_end, assignment_number = self.settings.assignment_cycle(report_date)
        if cycle_start == cycle_end:
            return f"{cycle_start.month}月{cycle_start.day}日"
        return (
            f"第{assignment_number}次作业（{cycle_start.month}月{cycle_start.day}日—"
            f"{cycle_end.month}月{cycle_end.day}日）"
        )

    def _answer_member_history(self, question: str, reference_day: date) -> str:
        member_name = next(
            (
                name
                for name in sorted(self.settings.report_members, key=len, reverse=True)
                if name != "，" and name in question
            ),
            "",
        )
        if not member_name:
            return "没有找到要查询的成员，请使用群内昵称，例如：查询小李全部打卡记录。"

        member_key = next(
            (
                open_id
                for open_id, alias in self.settings.member_aliases.items()
                if alias == member_name
            ),
            member_name,
        )
        records = self.store.list_member_attendance(
            member_key, member_name, reference_day.isoformat()
        )
        records = [
            record
            for record in records
            if self._attendance_record_is_in_scope(record.report_date)
            and (
                record.report_date != reference_day.isoformat()
                or record.homework_status != "missing"
                or record.review_status != "missing"
            )
        ]
        if not records:
            return f"{member_name}还没有可查询的打卡记录。"

        homework_labels = {
            "completed": "✅ 正常打卡",
            "late": "🟡 补卡",
            "pending": "🟠 待核验",
            "missing": "❌ 未打卡",
        }
        normal = sum(record.homework_status == "completed" for record in records)
        late = sum(record.homework_status == "late" for record in records)
        pending = sum(record.homework_status == "pending" for record in records)
        missing = sum(record.homework_status == "missing" for record in records)
        reviewed = sum(record.review_status == "completed" for record in records)
        first_day = datetime.strptime(records[0].report_date, "%Y-%m-%d").date()
        last_day = datetime.strptime(records[-1].report_date, "%Y-%m-%d").date()
        cumulative = f"累计：正常 {normal} 次｜补卡 {late} 次"
        if pending:
            cumulative += f"｜待核验 {pending} 次"
        cumulative += f"｜未打卡 {missing} 次｜复盘 {reviewed}/{len(records)}"
        lines = [
            f"{member_name}・全部打卡记录",
            "",
            f"统计范围：{first_day.month}月{first_day.day}日—{last_day.month}月{last_day.day}日",
            cumulative,
            "",
            "📋 逐次记录",
            "",
        ]
        for record in records:
            day = datetime.strptime(record.report_date, "%Y-%m-%d").date()
            homework = homework_labels.get(record.homework_status, record.homework_status)
            review = "✅ 已复盘" if record.review_status == "completed" else "❌ 未复盘"
            lines.append(
                f"{day.month}月{day.day}日（{record.assignment_label}）：{homework}｜{review}"
            )
        return "\n".join(lines)

    @staticmethod
    def _valid_date_markers(day: date) -> set[str]:
        return {
            f"{day:%m%d}",
            f"{day.month}{day.day:02d}",
            f"{day.month}月{day.day}日",
            f"{day.month}月{day.day}",
            f"{day.month}/{day.day}",
            f"{day.month}-{day.day}",
            f"{day:%Y-%m-%d}",
            f"{day.year}/{day.month}/{day.day}",
            f"{day.year}年{day.month}月{day.day}日",
        }

    def _is_review_for_day(self, text: str, report_date: str) -> bool:
        markers = list(_REVIEW_MARKER.finditer(text))
        if not markers:
            return False
        day = datetime.strptime(report_date, "%Y-%m-%d").date()
        valid_markers = self._valid_date_markers(day)
        for marker in markers:
            nearby = text[marker.start() : marker.end() + 24]
            explicit_dates = _EXPLICIT_REVIEW_DATE.findall(nearby)
            if not explicit_dates:
                return True
            compact = re.sub(r"\s+", "", nearby)
            if any(value in compact for value in valid_markers):
                return True
        return False

    @staticmethod
    def _clean_assignment_label(value: str) -> str:
        label = re.sub(r"\s+", "", value).strip("：:，,。！!")
        assignment = _ASSIGNMENT_LABEL_ONLY.fullmatch(label)
        if assignment:
            return f"第{assignment.group('number')}次作业"
        return label or "作业"

    def _assignment_routing_override(
        self,
        text: str,
        reference_day: date,
        label: str,
    ) -> Any:
        """明确且有效的作业序号优先；只有缓冲日/无效序号才由内容纠正。"""
        route = self.settings.assignment_route_for_text(text, reference_day)
        if route is None:
            return None
        assignment_match = _ASSIGNMENT_NUMBER.search(label)
        if assignment_match is None:
            return route
        assignment_number = self._parse_assignment_number(assignment_match.group("number"))
        target = self.settings.assignment_date_for_number(
            assignment_number,
            reference_day,
            self._course_name_hint(text),
        )
        if target is None or self.settings.assignment_is_paused(target):
            return route
        return route if route.report_date == target.isoformat() else None

    def _completion_markers_for_day(
        self,
        text: str,
        report_date: str,
        *,
        reference_day: Optional[date] = None,
    ) -> List[Tuple[str, int]]:
        report_date = self.settings.assignment_report_date(report_date)
        day = datetime.strptime(report_date, "%Y-%m-%d").date()
        marker_reference_day = reference_day or day
        markers: List[Tuple[str, int]] = []
        for marker_day, label, position, _is_late in self._dated_completion_markers(
            text, marker_reference_day
        ):
            route = self._assignment_routing_override(text, marker_day, label)
            if self._marker_report_date(marker_day, label, text) != report_date:
                continue
            markers.append((route.label if route else label, position))
        for target_day, label, position, _is_late in self._undated_completion_markers(
            text, marker_reference_day
        ):
            if target_day.isoformat() == report_date:
                markers.append((label, position))
        if not _LATE_MARKER.search(text):
            for match in _DAY_HOMEWORK_MARKER.finditer(text):
                assignment = re.sub(r"\s+", "", match.group("assignment")).upper()
                markers.append((f"{assignment}作业", match.start()))
        return markers

    def _undated_completion_markers(
        self,
        text: str,
        reference_day: date,
    ) -> List[Tuple[date, str, int, bool]]:
        markers: List[Tuple[date, str, int, bool]] = []
        dated_spans = [
            match.span()
            for pattern in (_DATED_COMPLETION_MARKER, _NATURAL_DATED_COMPLETION_MARKER)
            for match in pattern.finditer(text)
        ]
        for match in _UNDATED_COMPLETION_MARKER.finditer(text):
            if any(start <= match.start() < end for start, end in dated_spans):
                continue
            label = self._clean_assignment_label(match.group("label"))
            route = self._assignment_routing_override(text, reference_day, label)
            if route is not None:
                markers.append(
                    (
                        route.report_day,
                        route.label,
                        match.start(),
                        bool(_LATE_MARKER.search(match.group("status"))),
                    )
                )
                continue
            assignment_match = _ASSIGNMENT_NUMBER.search(label)
            if assignment_match is None:
                continue
            assignment_number = self._parse_assignment_number(assignment_match.group("number"))
            target = self.settings.assignment_date_for_number(
                assignment_number,
                reference_day,
                self._course_name_hint(text),
            )
            if target is None or target > reference_day:
                continue
            markers.append(
                (
                    target,
                    label,
                    match.start(),
                    bool(_LATE_MARKER.search(match.group("status"))),
                )
            )
        return markers

    def _marker_report_date(self, marker_day: date, label: str, text: str = "") -> str:
        """显式的「第 N 次作业」优先决定作业归属。

        补卡日的日期标签往往写实际提交日，不能再用该日期反推
        作业周期；没有作业序号时，仍保留原有按日期归属的规则。
        """
        if text and (
            route := self._assignment_routing_override(text, marker_day, label)
        ) is not None:
            return route.report_date
        match = _ASSIGNMENT_NUMBER.search(label)
        if match and self.settings.configured_course_phases:
            assignment_number = self._parse_assignment_number(match.group("number"))
            target = self.settings.assignment_date_for_number(
                assignment_number,
                marker_day,
                self._course_name_hint(label),
            )
            if target is not None:
                return target.isoformat()
        return self.settings.assignment_report_date(marker_day)

    def _dated_completion_markers(
        self, text: str, reference_day: date
    ) -> List[Tuple[date, str, int, bool]]:
        markers: List[Tuple[date, str, int, bool]] = []
        for match in _DATED_COMPLETION_MARKER.finditer(text):
            mmdd = match.group("mmdd")
            try:
                candidate = date(reference_day.year, int(mmdd[:2]), int(mmdd[2:]))
            except ValueError:
                continue
            if candidate > reference_day + timedelta(days=31):
                candidate = date(reference_day.year - 1, candidate.month, candidate.day)
            label = self._clean_assignment_label(match.group("label"))
            if any(blocked in label for blocked in _NON_HOMEWORK_LABELS):
                continue
            markers.append(
                (candidate, label, match.start(), bool(_LATE_MARKER.search(match.group("status"))))
            )
        for match in _NATURAL_DATED_COMPLETION_MARKER.finditer(text):
            try:
                candidate = date(
                    reference_day.year,
                    int(match.group("month")),
                    int(match.group("day")),
                )
            except ValueError:
                continue
            if candidate > reference_day + timedelta(days=31):
                candidate = date(reference_day.year - 1, candidate.month, candidate.day)
            label = self._clean_assignment_label(match.group("label"))
            if any(blocked in label for blocked in _NON_HOMEWORK_LABELS):
                continue
            markers.append(
                (candidate, label, match.start(), bool(_LATE_MARKER.search(match.group("status"))))
            )
        return markers

    def _submission_report_dates(
        self,
        text: str,
        reference_day: date,
        *,
        allow_embedded: bool = False,
    ) -> List[str]:
        report_dates: set[str] = set()
        for candidate, label, position, _is_late in self._dated_completion_markers(
            text, reference_day
        ):
            if not allow_embedded and not self._is_submission_marker(text, position):
                continue
            route = self._assignment_routing_override(text, candidate, label)
            if route is not None:
                report_dates.add(route.report_date)
                continue
            assignment_match = _ASSIGNMENT_NUMBER.search(label)
            if assignment_match:
                assignment_number = self._parse_assignment_number(assignment_match.group("number"))
                target = self.settings.assignment_date_for_number(
                    assignment_number,
                    candidate,
                    self._course_name_hint(label),
                )
                if target is None or target > candidate:
                    continue
            elif self.settings.configured_course_phases and not self.settings.course_is_active(
                candidate
            ):
                continue
            report_dates.add(self._marker_report_date(candidate, label, text))
        for candidate, _label, position, _is_late in self._undated_completion_markers(
            text, reference_day
        ):
            if not allow_embedded and not self._is_submission_marker(text, position):
                continue
            report_dates.add(candidate.isoformat())
        return sorted(report_dates)

    @staticmethod
    def _is_submission_marker(text: str, marker_start: int) -> bool:
        prefix = text[:marker_start].strip()
        prefix = _WEB_LINK.sub("", prefix).strip()
        if prefix.startswith(_MULTI_MERGE_PREFIX):
            return False
        if _MENTION_ONLY_PREFIX.fullmatch(prefix):
            return True
        if not prefix:
            return True
        segments = [segment.strip() for segment in re.split(r"[；;\n]", prefix)]
        return all(not segment or bool(_RESOURCE_TOKEN.search(segment)) for segment in segments)

    @staticmethod
    def _late_image_message_ids(messages: Sequence[StoredMessage]) -> set[str]:
        recent_images: Dict[str, List[StoredMessage]] = defaultdict(list)
        late_message_ids: set[str] = set()
        for message in messages:
            if "[图片]" in message.content:
                recent_images[message.sender_open_id].append(message)
            if not _LATE_MARKER.search(message.content):
                continue
            for candidate in reversed(recent_images[message.sender_open_id]):
                delta = message.create_time_ms - candidate.create_time_ms
                if 0 <= delta <= _LATE_TAG_WINDOW_MS:
                    late_message_ids.add(candidate.message_id)
                    break
        return late_message_ids

    @staticmethod
    def _is_thread_homework(message: StoredMessage) -> bool:
        """话题中的附件、作品链接或明确完成回复算作业。"""
        if not message.thread_id:
            return False
        return (
            message.message_type in _THREAD_HOMEWORK_TYPES
            or "[图片]" in message.content
            or bool(_WEB_LINK.search(message.content))
            or bool(_THREAD_COMPLETION_TEXT.fullmatch(message.content))
        )

    @staticmethod
    def _thread_assignment_label(messages: Sequence[StoredMessage]) -> str:
        labels: Counter[str] = Counter()
        for message in messages:
            if not message.thread_id:
                continue
            for match in _THREAD_ASSIGNMENT_LABEL.finditer(message.content):
                labels[re.sub(r"\s+", "", match.group("label"))] += 1
        return labels.most_common(1)[0][0] if labels else ""

    def _completion_facts(
        self,
        report_date: str,
        messages: Sequence[StoredMessage],
        homework_messages: Optional[Sequence[StoredMessage]] = None,
    ) -> Dict[str, Any]:
        roster = list(self.settings.report_members)
        roster_set = set(roster)
        image_count = sum(message.content.count("[图片]") for message in messages)
        homework_source_messages = (
            list(messages) if homework_messages is None else list(homework_messages)
        )
        review_counts: Counter[str] = Counter()
        review_evidence: Dict[str, List[str]] = defaultdict(list)
        marker_labels: Counter[str] = Counter()
        marker_evidence: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
        marker_declarations: List[Tuple[StoredMessage, str]] = []
        pending_evidence: Dict[str, List[str]] = defaultdict(list)
        strict_artifact_required = self._requires_submission_artifact(report_date)
        report_route = self.settings.assignment_route_for_report_date(report_date)
        declaration_required = bool(report_route and report_route.declaration_required)
        all_declarations = self._submission_declarations(homework_source_messages)
        thread_homework = [
            message for message in homework_source_messages if self._is_thread_homework(message)
        ]
        thread_assignment_label = self._thread_assignment_label(homework_source_messages)
        for message in messages:
            if self._is_review_for_day(message.content, report_date):
                review_counts[message.sender_name] += 1
                review_evidence[message.sender_name].append(message.message_id)
        for message in homework_source_messages:
            message_day = datetime.fromtimestamp(
                message.create_time_ms / 1000, tz=self.settings.tz
            ).date()
            allow_embedded = (
                message.message_type == "merge_forward"
                and not message.content.startswith(_MULTI_MERGE_PREFIX)
            )
            for label, position in self._completion_markers_for_day(
                message.content,
                report_date,
                reference_day=message_day,
            ):
                if allow_embedded or self._is_submission_marker(message.content, position):
                    marker_labels[label] += 1
                    marker_evidence[message.sender_name][label].append(message.message_id)
                    marker_declarations.append((message, label))

        homework_evidence: Dict[str, List[str]] = defaultdict(list)
        if marker_labels:
            assignment_label = marker_labels.most_common(1)[0][0]
            homework_source = "tag+thread" if thread_homework else "tag"
            if strict_artifact_required:
                for declaration, label in marker_declarations:
                    if label != assignment_label:
                        continue
                    artifacts = self._associated_artifact_messages(
                        declaration,
                        report_date,
                        homework_source_messages,
                        all_declarations,
                    )
                    if artifacts:
                        evidence_ids = [declaration.message_id]
                        evidence_ids.extend(message.message_id for message in artifacts)
                        homework_evidence[declaration.sender_name].extend(evidence_ids)
                    else:
                        pending_evidence[declaration.sender_name].append(declaration.message_id)
            else:
                for name, labels in marker_evidence.items():
                    homework_evidence[name].extend(labels.get(assignment_label, []))
            for message in thread_homework:
                homework_evidence[message.sender_name].append(message.message_id)
        elif thread_homework:
            assignment_label = thread_assignment_label or "话题作业"
            homework_source = "thread"
            for message in homework_source_messages:
                if self._is_thread_homework(message) or "[图片]" in message.content:
                    homework_evidence[message.sender_name].append(message.message_id)
        else:
            assignment_label = report_route.label if declaration_required else "图片作业"
            homework_source = "tag" if declaration_required else "image"
            if not declaration_required:
                late_image_ids = self._late_image_message_ids(homework_source_messages)
                for message in homework_source_messages:
                    if (
                        "[图片]" in message.content
                        and message.message_id not in late_image_ids
                        and not message.parent_id
                        and not message.root_id
                    ):
                        homework_evidence[message.sender_name].append(message.message_id)

        for message in homework_source_messages:
            if (
                message.message_type != "merge_forward"
                or message.content.startswith(_MULTI_MERGE_PREFIX)
                or not _RESOURCE_TOKEN.search(message.content)
                or _LATE_MARKER.search(message.content)
            ):
                continue
            message_day = datetime.fromtimestamp(
                message.create_time_ms / 1000, tz=self.settings.tz
            ).date()
            explicit_dates = self._submission_report_dates(
                message.content,
                message_day,
                allow_embedded=True,
            )
            if explicit_dates and report_date not in explicit_dates:
                continue
            if not explicit_dates:
                submitted_at = datetime.fromtimestamp(
                    message.create_time_ms / 1000,
                    tz=self.settings.tz,
                )
                if (
                    self._implicit_submission_report_date(
                        message.content,
                        submitted_at,
                    )
                    != report_date
                ):
                    continue
            if message.message_id not in homework_evidence[message.sender_name]:
                homework_evidence[message.sender_name].append(message.message_id)
            if marker_labels:
                homework_source = "tag+merge"
            elif homework_source == "image":
                cycle_number = self.settings.assignment_cycle(report_date)[2]
                assignment_label = f"第{cycle_number}次作业"
                homework_source = "merge"

        homework_members_set = {
            name for name, evidence in homework_evidence.items() if evidence and name in roster_set
        }
        pending_members_set = {
            name
            for name, evidence in pending_evidence.items()
            if evidence and name in roster_set and name not in homework_members_set
        }
        review_members_set = {
            name for name, evidence in review_evidence.items() if evidence and name in roster_set
        }
        for name, evidence_ids in homework_evidence.items():
            homework_evidence[name] = list(dict.fromkeys(evidence_ids))
        homework_members = [name for name in roster if name in homework_members_set]
        review_members = [name for name in roster if name in review_members_set]
        facts = {
            "roster": roster,
            "assignment_label": assignment_label,
            "homework_source": homework_source,
            "assignment_detected": bool(
                marker_labels or thread_assignment_label or homework_evidence
            ),
            "image_count": image_count,
            "homework_members": homework_members,
            "late_members": [],
            "pending_members": [name for name in roster if name in pending_members_set],
            "review_members": review_members,
            "review_counts": review_counts,
            "homework_evidence": homework_evidence,
            "pending_evidence": pending_evidence,
            "review_evidence": review_evidence,
            "both": [
                name
                for name in roster
                if name in homework_members_set and name in review_members_set
            ],
            "only_homework": [
                name
                for name in roster
                if name in homework_members_set and name not in review_members_set
            ],
            "only_reviews": [
                name
                for name in roster
                if name not in homework_members_set and name in review_members_set
            ],
            "none": [],
        }
        self._rebuild_completion_groups(facts)
        return facts

    @staticmethod
    def _rebuild_completion_groups(facts: Dict[str, Any]) -> None:
        roster = list(facts["roster"])
        completed = set(facts["homework_members"])
        pending = set(facts.get("pending_members", ())) - completed
        reviews = set(facts["review_members"])
        facts["pending_members"] = [name for name in roster if name in pending]
        facts["both"] = [name for name in roster if name in completed and name in reviews]
        facts["only_homework"] = [
            name for name in roster if name in completed and name not in reviews
        ]
        facts["only_reviews"] = [
            name for name in roster if name not in completed and name in reviews
        ]
        facts["none"] = [
            name
            for name in roster
            if name not in completed and name not in reviews and name not in pending
        ]

    def _apply_late_completions(
        self, report_date: str, messages: Sequence[StoredMessage], facts: Dict[str, Any]
    ) -> None:
        report_date = self.settings.assignment_report_date(report_date)
        if not messages:
            return
        start_ms = int(self.settings.assignment_deadline(report_date).timestamp() * 1000) + 1
        late_stage_end = self.settings.makeup_deadline(report_date)
        now = datetime.now(tz=self.settings.tz)
        end_ms = min(
            int(now.timestamp() * 1000) + 1,
            int(late_stage_end.timestamp() * 1000),
        )
        if start_ms >= end_ms:
            return
        later_messages = self.store.list_messages(
            messages[0].chat_id, start_ms, end_ms, self.settings.max_messages
        )
        later_messages = self._apply_member_aliases(later_messages)
        roster = facts["roster"]
        roster_set = set(roster)
        completed = set(facts["homework_members"])
        late_members: set[str] = set()
        late_labels: Counter[str] = Counter()
        expected_label = facts["assignment_label"]
        expected_thread_ids = {message.thread_id for message in messages if message.thread_id}
        if self._requires_submission_artifact(report_date):
            submission_window_start = self.settings.assignment_window_start(report_date)
            combined_messages = self.store.list_messages(
                messages[0].chat_id,
                int(submission_window_start.timestamp() * 1000),
                end_ms,
                self.settings.max_messages,
            )
            combined_messages = self._apply_member_aliases(combined_messages)
            declarations = self._submission_declarations(combined_messages)
            routed_labels: Counter[str] = Counter()
            pending = set(facts.get("pending_members", ())) if now <= late_stage_end else set()
            pending_evidence = facts.setdefault("pending_evidence", defaultdict(list))
            deadline_ms = int(self.settings.assignment_deadline(report_date).timestamp() * 1000)
            for declaration, declaration_report_date in declarations:
                if declaration_report_date != report_date:
                    continue
                declaration_day = datetime.fromtimestamp(
                    declaration.create_time_ms / 1000,
                    tz=self.settings.tz,
                ).date()
                if route := self.settings.assignment_route_for_text(
                    declaration.content,
                    declaration_day,
                ):
                    routed_labels[route.label] += 1
                name = self.settings.member_aliases.get(
                    declaration.sender_open_id,
                    declaration.sender_name,
                )
                if name not in roster_set:
                    continue
                artifacts = self._associated_artifact_messages(
                    declaration,
                    report_date,
                    combined_messages,
                    declarations,
                )
                if not artifacts:
                    if now <= late_stage_end:
                        pending.add(name)
                        pending_evidence[name].append(declaration.message_id)
                    continue
                evidence_ids = [declaration.message_id]
                evidence_ids.extend(message.message_id for message in artifacts)
                facts["homework_evidence"][name].extend(evidence_ids)
                completed.add(name)
                pending.discard(name)
                evidence_time_ms = min(message.create_time_ms for message in artifacts)
                if evidence_time_ms > deadline_ms:
                    late_members.add(name)

            for raw_message in later_messages:
                if raw_message.thread_id in expected_thread_ids and self._is_thread_homework(
                    raw_message
                ):
                    name = self.settings.member_aliases.get(
                        raw_message.sender_open_id,
                        raw_message.sender_name,
                    )
                    if name in roster_set:
                        facts["homework_evidence"][name].append(raw_message.message_id)
                        completed.add(name)
                        pending.discard(name)
                        late_members.add(name)

            facts["homework_members"] = [name for name in roster if name in completed]
            facts["late_members"] = [name for name in roster if name in late_members]
            if expected_label in {"图片作业", "话题作业"} and routed_labels:
                facts["assignment_label"] = routed_labels.most_common(1)[0][0]
                facts["homework_source"] = "tag"
            facts["pending_members"] = [
                name for name in roster if name in pending and name not in completed
            ]
            for name, evidence_ids in facts["homework_evidence"].items():
                facts["homework_evidence"][name] = list(dict.fromkeys(evidence_ids))
            self._rebuild_completion_groups(facts)
            return
        for raw_message in later_messages:
            if raw_message.sender_open_id in self.settings.excluded_member_ids:
                continue
            name = self.settings.member_aliases.get(
                raw_message.sender_open_id, raw_message.sender_name
            )
            if name not in roster_set or name in completed:
                continue
            if raw_message.thread_id in expected_thread_ids and self._is_thread_homework(
                raw_message
            ):
                facts["homework_evidence"][name].append(raw_message.message_id)
                late_members.add(name)
                continue
            message_day = datetime.fromtimestamp(
                raw_message.create_time_ms / 1000, tz=self.settings.tz
            ).date()
            allow_embedded = (
                raw_message.message_type == "merge_forward"
                and not raw_message.content.startswith(_MULTI_MERGE_PREFIX)
            )
            matched_marker = False
            for label, position in self._completion_markers_for_day(
                raw_message.content,
                report_date,
                reference_day=message_day,
            ):
                if not allow_embedded and not self._is_submission_marker(
                    raw_message.content, position
                ):
                    continue
                if expected_label not in {"图片作业", "话题作业"} and label != expected_label:
                    continue
                if not self._homework_evidence_kind(raw_message):
                    continue
                late_labels[label] += 1
                facts["homework_evidence"][name].append(raw_message.message_id)
                late_members.add(name)
                matched_marker = True
                break
            if matched_marker:
                continue
            if not self.settings.configured_course_phases:
                for marker_day, label, position, is_late in self._dated_completion_markers(
                    raw_message.content,
                    message_day,
                ):
                    if not is_late or marker_day != message_day:
                        continue
                    if not self._is_submission_marker(raw_message.content, position):
                        continue
                    if expected_label not in {"图片作业", "话题作业"} and label != expected_label:
                        continue
                    if not self._homework_evidence_kind(raw_message):
                        continue
                    late_labels[label] += 1
                    facts["homework_evidence"][name].append(raw_message.message_id)
                    late_members.add(name)
                    matched_marker = True
                    break
            if matched_marker:
                continue
            if (
                raw_message.message_type == "merge_forward"
                and not raw_message.content.startswith(_MULTI_MERGE_PREFIX)
                and _RESOURCE_TOKEN.search(raw_message.content)
                and not self._submission_report_dates(
                    raw_message.content,
                    message_day,
                    allow_embedded=True,
                )
                and self._implicit_submission_report_date(
                    raw_message.content,
                    datetime.fromtimestamp(
                        raw_message.create_time_ms / 1000,
                        tz=self.settings.tz,
                    ),
                )
                == report_date
            ):
                facts["homework_evidence"][name].append(raw_message.message_id)
                late_members.add(name)
        if expected_label == "图片作业" and late_labels:
            facts["assignment_label"] = late_labels.most_common(1)[0][0]
            facts["homework_source"] = "tag"
        facts["late_members"] = [name for name in roster if name in late_members]
        completed.update(late_members)
        review_members = set(facts["review_members"])
        facts["homework_members"] = [name for name in roster if name in completed]
        facts["both"] = [name for name in roster if name in completed and name in review_members]
        facts["only_homework"] = [
            name for name in roster if name in completed and name not in review_members
        ]
        facts["only_reviews"] = [
            name for name in roster if name not in completed and name in review_members
        ]
        facts["none"] = [
            name for name in roster if name not in completed and name not in review_members
        ]

    def _apply_verified_completions(self, report_date: str, facts: Dict[str, Any]) -> None:
        verifications = self.store.list_homework_verifications(report_date)
        if not verifications:
            return
        roster = facts["roster"]
        roster_set = set(roster)
        completed = set(facts["homework_members"])
        late = set(facts["late_members"])
        pending = set(facts.get("pending_members", ()))
        normal = completed - late
        for verification in verifications:
            name = self.settings.member_aliases.get(
                str(verification["member_key"]),
                str(verification["sender_name"]),
            )
            if name not in roster_set:
                continue
            evidence_ids = facts["homework_evidence"][name]
            evidence_ids.extend(verification["evidence_message_ids"])
            facts["homework_evidence"][name] = list(dict.fromkeys(evidence_ids))
            status = str(verification["status"])
            if status == "completed":
                completed.add(name)
                normal.add(name)
                late.discard(name)
                pending.discard(name)
            elif name not in normal:
                completed.add(name)
                late.add(name)
                pending.discard(name)

        facts["homework_members"] = [name for name in roster if name in completed]
        facts["late_members"] = [name for name in roster if name in late]
        facts["pending_members"] = [
            name for name in roster if name in pending and name not in completed
        ]
        self._rebuild_completion_groups(facts)

    @staticmethod
    def _base_cell_text(value: Any) -> str:
        """兼容多维表格单选字段的几种返回形式。"""
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return GroupSummaryService._base_cell_text(value[0]) if value else ""
        if isinstance(value, dict):
            for key in ("name", "text", "value"):
                if value.get(key) is not None:
                    return str(value[key]).strip()
        return str(value).strip()

    def _manual_attendance_overrides(self, report_date: str) -> Dict[str, str]:
        """从今日打卡表读取组长人工状态。

        读取失败时不吞掉异常：定时催交和日报应当停止发送，
        避免把已经由组长排除的成员又发到群里。
        """
        if not self.settings.base_sync_enabled:
            return {}
        records = self.api.list_base_records(self.settings.base_token, self.settings.base_table_id)
        overrides: Dict[str, str] = {}
        key_prefix = f"{report_date}|"
        for record in records:
            fields = record.get("fields") or {}
            record_key = self._base_cell_text(fields.get("记录键"))
            if not record_key.startswith(key_prefix):
                continue
            name = self._base_cell_text(fields.get("组员姓名"))
            manual_value = self._base_cell_text(fields.get("人工状态"))
            status = _MANUAL_ATTENDANCE_STATUS.get(manual_value)
            if name and status:
                overrides[name] = status
        return overrides

    def _apply_manual_attendance_overrides(
        self, report_date: str, facts: Dict[str, Any]
    ) -> List[str]:
        """把人工状态叠加到系统识别结果，并返回全量名单。"""
        full_roster = list(facts["roster"])
        overrides = self._manual_attendance_overrides(report_date)
        completed = set(facts["homework_members"])
        late = set(facts["late_members"])
        pending = set(facts.get("pending_members", ()))
        excluded: set[str] = set()
        for name, status in overrides.items():
            if name not in full_roster:
                continue
            if status == "excluded":
                excluded.add(name)
                completed.discard(name)
                late.discard(name)
                pending.discard(name)
            elif status == "completed":
                completed.add(name)
                late.discard(name)
                pending.discard(name)
            elif status == "late":
                completed.add(name)
                late.add(name)
                pending.discard(name)
            elif status == "missing":
                completed.discard(name)
                late.discard(name)
                pending.discard(name)

        facts["manual_statuses"] = overrides
        facts["excluded_members"] = [name for name in full_roster if name in excluded]
        facts["homework_members"] = [name for name in full_roster if name in completed]
        facts["late_members"] = [name for name in full_roster if name in late]
        facts["pending_members"] = [name for name in full_roster if name in pending]
        self._rebuild_completion_groups(facts)
        return full_roster

    @staticmethod
    def _hide_excluded_members(facts: Dict[str, Any]) -> None:
        """对外统计不暴露请假/豁免成员的姓名、原因或人数。"""
        excluded = set(facts.get("excluded_members", ()))
        if not excluded:
            return
        roster = [name for name in facts["roster"] if name not in excluded]
        completed = set(facts["homework_members"]) - excluded
        late = set(facts["late_members"]) - excluded
        pending = set(facts.get("pending_members", ())) - excluded
        reviews = set(facts["review_members"]) - excluded
        facts["roster"] = roster
        facts["homework_members"] = [name for name in roster if name in completed]
        facts["late_members"] = [name for name in roster if name in late]
        facts["pending_members"] = [name for name in roster if name in pending]
        facts["review_members"] = [name for name in roster if name in reviews]
        GroupSummaryService._rebuild_completion_groups(facts)

    def _persist_attendance(
        self, report_date: str, messages: Sequence[StoredMessage], facts: Dict[str, Any]
    ) -> None:
        report_date = self.settings.assignment_report_date(report_date)
        self._apply_late_completions(report_date, messages, facts)
        self._apply_verified_completions(report_date, facts)
        full_roster = self._apply_manual_attendance_overrides(report_date, facts)
        open_id_by_name = {name: open_id for open_id, name in self.settings.member_aliases.items()}
        open_id_by_name.update(
            {message.sender_name: message.sender_open_id for message in messages}
        )
        homework_members = set(facts["homework_members"])
        late_members = set(facts["late_members"])
        pending_members = set(facts.get("pending_members", ()))
        review_members = set(facts["review_members"])
        excluded_members = set(facts.get("excluded_members", ()))
        records = []
        for name in full_roster:
            open_id = open_id_by_name.get(name, "")
            records.append(
                AttendanceRecord(
                    report_date=report_date,
                    member_key=open_id or f"name:{name}",
                    sender_open_id=open_id,
                    sender_name=name,
                    assignment_label=facts["assignment_label"],
                    homework_status=(
                        "excluded"
                        if name in excluded_members
                        else "late"
                        if name in late_members
                        else "completed"
                        if name in homework_members
                        else "pending"
                        if name in pending_members
                        else "missing"
                    ),
                    review_status="completed" if name in review_members else "missing",
                    homework_source=facts["homework_source"],
                    homework_message_ids=tuple(facts["homework_evidence"].get(name, ())),
                    review_message_ids=tuple(facts["review_evidence"].get(name, ())),
                )
            )
        self.store.replace_daily_attendance(records)
        if self.settings.base_sync_enabled:
            try:
                self._sync_attendance_to_base(records)
            except Exception:
                logger.exception("同步多维表格失败：%s", report_date)
        self._hide_excluded_members(facts)

    @staticmethod
    def _format_time_ms(value: int, tz: Any) -> str:
        if not value:
            return ""
        return datetime.fromtimestamp(value / 1000, tz=tz).strftime("%Y-%m-%d %H:%M:%S")

    def _totals_through_date(self, report_date: str) -> str:
        report_day = datetime.strptime(report_date, "%Y-%m-%d").date()
        now = datetime.now(tz=self.settings.tz)
        cutoff_reached = (now.hour, now.minute) >= (
            self.settings.summary_hour,
            self.settings.summary_minute,
        )
        if report_day < now.date() or (report_day == now.date() and cutoff_reached):
            return report_date
        return (report_day - timedelta(days=1)).isoformat()

    def _load_base_record_index(self) -> None:
        if self._base_index_loaded:
            return
        records = self.api.list_base_records(self.settings.base_token, self.settings.base_table_id)
        self._base_record_index = {
            str(record["fields"].get("记录键")): str(record["record_id"])
            for record in records
            if record.get("record_id") and record.get("fields", {}).get("记录键")
        }
        self._base_index_loaded = True

    def _base_fields_for_attendance(self, record: AttendanceRecord) -> Dict[str, Any]:
        homework_time = self.store.message_time_ms(record.homework_message_ids)
        review_time = self.store.message_time_ms(record.review_message_ids)
        iteration = self.store.latest_iteration(record.report_date, record.member_key)
        cycle_start, _, assignment_number = self.settings.assignment_cycle(record.report_date)
        current_cycle_start = self.settings.assignment_cycle(
            self.settings.current_assignment_report_date()
        )[0]
        completed, late, missing = self.store.attendance_totals(
            record.member_key, self._totals_through_date(record.report_date)
        )
        fields: Dict[str, Any] = {
            "记录键": f"{record.report_date}|{record.member_key}",
            "日期": f"{record.report_date} 00:00:00",
            "作业序号": assignment_number,
            "作业周期": self._assignment_period_label(record.report_date),
            "周期状态": "当前周期" if cycle_start == current_cycle_start else "历史周期",
            "作业名称": record.assignment_label,
            "组员姓名": record.sender_name,
            "飞书OpenID": record.sender_open_id,
            "复盘状态": "已复盘" if record.review_status == "completed" else "未复盘",
            "迭代状态": (
                "待迭代"
                if iteration and iteration["status"] == "pending"
                else "已迭代"
                if iteration and iteration["status"] == "completed"
                else "无需迭代"
            ),
            "迭代发起人": str(iteration["actor_name"]) if iteration else "",
            "作业证据消息ID": "、".join(record.homework_message_ids),
            "复盘证据消息ID": "、".join(record.review_message_ids),
            "正常提交累计": completed,
            "补卡累计": late,
            "旷卡累计": missing,
        }
        status_value = {
            "completed": "已提交",
            "late": "补卡",
            "pending": "待核验",
            "missing": "未提交",
        }.get(record.homework_status)
        if status_value:
            fields["作业状态"] = status_value
        if homework_time:
            fields["提交时间"] = self._format_time_ms(homework_time, self.settings.tz)
        if review_time:
            fields["复盘时间"] = self._format_time_ms(review_time, self.settings.tz)
        if iteration:
            fields["迭代时间"] = self._format_time_ms(
                int(iteration["event_time_ms"]), self.settings.tz
            )
        return fields

    def _refresh_base_cycle_statuses(self, current_report_date: str) -> int:
        """把上一次作业从当前周期转为历史周期。"""
        current_report_date = self.settings.assignment_report_date(current_report_date)
        records = self.api.list_base_records(self.settings.base_token, self.settings.base_table_id)
        updated = 0
        for record in records:
            fields = record.get("fields") or {}
            record_key = self._base_cell_text(fields.get("记录键"))
            record_id = str(record.get("record_id") or "")
            if not record_key or not record_id or "|" not in record_key:
                continue
            row_date = record_key.split("|", 1)[0]
            desired = "当前周期" if row_date == current_report_date else "历史周期"
            actual = self._base_cell_text(fields.get("周期状态"))
            if actual == desired:
                continue
            self.api.update_base_record(
                self.settings.base_token,
                self.settings.base_table_id,
                record_id,
                {"周期状态": desired},
            )
            updated += 1
        return updated

    def _sync_attendance_to_base(self, records: Sequence[AttendanceRecord]) -> Dict[str, int]:
        with self._base_sync_lock:
            return self._sync_attendance_to_base_locked(records)

    def _sync_attendance_to_base_locked(
        self,
        records: Sequence[AttendanceRecord],
    ) -> Dict[str, int]:
        if not self.settings.base_sync_enabled:
            return {"created": 0, "updated": 0, "skipped": len(records)}
        self._load_base_record_index()
        counts = {"created": 0, "updated": 0, "skipped": 0}
        for record in records:
            fields = self._base_fields_for_attendance(record)
            record_key = str(fields["记录键"])
            payload_hash = hashlib.sha256(
                json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            state = self.store.base_sync_state(record_key)
            record_id = state[0] if state else self._base_record_index.get(record_key, "")
            if state and state[1] == payload_hash:
                counts["skipped"] += 1
                continue
            payload = dict(fields)
            payload["最后同步时间"] = datetime.now(tz=self.settings.tz).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            if record_id:
                self.api.update_base_record(
                    self.settings.base_token,
                    self.settings.base_table_id,
                    record_id,
                    payload,
                )
                counts["updated"] += 1
            else:
                record_id = self.api.create_base_record(
                    self.settings.base_token, self.settings.base_table_id, payload
                )
                if not record_id:
                    self._base_index_loaded = False
                    self._load_base_record_index()
                    record_id = self._base_record_index.get(record_key, "")
                if not record_id:
                    raise RuntimeError(f"多维表格创建记录后没有返回 record_id：{record_key}")
                self._base_record_index[record_key] = record_id
                counts["created"] += 1
            self.store.save_base_sync_state(record_key, record_id, payload_hash)
        current_report_date = self.settings.current_assignment_report_date()
        if records and records[0].report_date == current_report_date:
            self._refresh_base_cycle_statuses(current_report_date)
        return counts

    def sync_attendance_date(self, report_date: str, chat_id: str) -> int:
        if not self._attendance_record_is_in_scope(report_date):
            if self.settings.assignment_is_paused(report_date):
                removed = self.store.delete_attendance_date(report_date)
                logger.info("缓冲日不建立全员作业记录：%s %s，清理 %d 条", report_date, chat_id, removed)
                return 0
            logger.info("日期位于课程阶段空档，跳过打卡同步：%s %s", report_date, chat_id)
            return 0
        report_date = self.settings.assignment_report_date(report_date)
        cycle_start, cycle_end, _ = self.settings.assignment_cycle(report_date)
        start_ms, _ = _day_range_ms(cycle_start, self.settings.tz)
        _, end_ms = _day_range_ms(cycle_end, self.settings.tz)
        messages = self.store.list_messages(
            chat_id, start_ms, end_ms, limit=self.settings.max_messages
        )
        messages = self._apply_member_aliases(
            [
                message
                for message in messages
                if message.sender_open_id not in self.settings.excluded_member_ids
            ]
        )
        homework_messages = self._assignment_window_messages(
            report_date,
            chat_id,
            *self.settings.assignment_due_clock(report_date),
        )
        late_messages = self._post_deadline_messages(report_date, chat_id)
        if not messages and not homework_messages and not late_messages:
            logger.info("没有可同步消息，跳过：%s %s", report_date, chat_id)
            return 0
        facts = self._completion_facts(report_date, messages, homework_messages=homework_messages)
        self._persist_attendance(report_date, homework_messages or late_messages or messages, facts)
        return len(facts["roster"])

    def sync_assignment_deadline(self, report_date: str) -> int:
        total = 0
        for chat_id in sorted(set(self.settings.chat_ids) | set(self.store.list_chats())):
            try:
                total += self.sync_attendance_date(report_date, chat_id)
            except Exception:
                logger.exception("截止时间同步打卡状态失败：%s %s", report_date, chat_id)
        logger.info("截止时间打卡状态已刷新：%s 共 %d 条", report_date, total)
        return total

    def sync_stored_attendance_date(self, report_date: str) -> int:
        records = self.store.list_daily_attendance(report_date)
        if not records:
            logger.info("没有已存打卡状态，跳过：%s", report_date)
            return 0
        self._sync_attendance_to_base(records)
        return len(records)

    def _homework_reaction_pool(
        self,
        report_date: str,
        submitted_at: datetime,
        homework_status: str,
    ) -> Tuple[str, ...]:
        deadline = self.settings.assignment_deadline(report_date)
        if homework_status == "late" or submitted_at > deadline:
            return _MAKEUP_HOMEWORK_REACTIONS
        cycle_start, _, _ = self.settings.assignment_cycle(report_date)
        if submitted_at.date() == cycle_start:
            return _FIRST_DAY_HOMEWORK_REACTIONS
        return _SECOND_DAY_HOMEWORK_REACTIONS

    def _maybe_react_to_homework(
        self,
        message: StoredMessage,
        report_dates: Sequence[str],
        submitted_at: datetime,
    ) -> bool:
        if not self.settings.send_enabled or not self.settings.homework_reaction_enabled:
            return False
        if self.store.homework_reaction_sent(message.message_id):
            return False
        for raw_report_date in report_dates:
            report_date = self.settings.assignment_report_date(raw_report_date)
            phase = self.settings.course_phase(report_date)
            if phase is not None and phase.end_day is not None:
                if submitted_at.date() > phase.end_day:
                    continue
            for record in self.store.list_daily_attendance(report_date):
                if message.message_id not in record.homework_message_ids:
                    continue
                if record.homework_status not in {"completed", "late"}:
                    continue
                emoji_type = choice(
                    self._homework_reaction_pool(
                        report_date,
                        submitted_at,
                        record.homework_status,
                    )
                )
                try:
                    reaction_id = self.api.add_reaction(message.message_id, emoji_type)
                except Exception:
                    logger.exception(
                        "作业已识别，但添加表情回复失败：%s %s",
                        report_date,
                        message.message_id,
                    )
                    return False
                self.store.mark_homework_reaction_sent(
                    message_id=message.message_id,
                    report_date=report_date,
                    chat_id=message.chat_id,
                    emoji_type=emoji_type,
                    reaction_id=reaction_id,
                )
                logger.info(
                    "已为作业添加表情回复：%s %s %s",
                    report_date,
                    message.message_id,
                    emoji_type,
                )
                return True
        return False

    @staticmethod
    def _names(names: Sequence[str]) -> str:
        return "、".join(names) if names else "无"

    def _report_context(self, facts: Dict[str, Any]) -> str:
        review_counts: Counter[str] = facts["review_counts"]
        review_detail = "、".join(
            f"{name}（{review_counts[name]} 条）" for name in facts["review_members"]
        )
        return (
            f"群成员总数：{len(facts['roster'])} 人\n"
            f"{facts['assignment_label']}完成人员：{self._names(facts['homework_members'])}\n"
            f"当日有效复盘人员及条数：{review_detail or '无'}\n"
            f"有效复盘名单JSON：{json.dumps(facts['review_members'], ensure_ascii=False)}\n"
            "只把带复盘标签且归属报告日期的消息整理进每日复盘。"
        )

    @staticmethod
    def _fallback_analysis(messages: Sequence[StoredMessage], facts: Dict[str, Any]) -> str:
        """Keep the report usable without allowing model failure to alter facts."""
        by_id = {message.message_id: message for message in messages}
        lines = [f"📝 每日复盘（{len(facts['review_members'])} 人）", ""]
        if not facts["review_members"]:
            lines.append("无")
        for index, name in enumerate(facts["review_members"], start=1):
            evidence = [
                by_id[message_id]
                for message_id in facts["review_evidence"].get(name, ())
                if message_id in by_id
            ]
            title = f"{index}. {name}"
            if len(evidence) > 1:
                title += f"（{len(evidence)} 条）"
            lines.extend([title])
            for item_index, message in enumerate(evidence, start=1):
                content = _REVIEW_MARKER.sub("", message.content, count=1).strip(" ：:")
                if not content:
                    content = "（仅识别到复盘标签，未读取到可整理正文）"
                prefix = f"第 {item_index} 条：" if len(evidence) > 1 else ""
                lines.append(prefix + content[:2000])
            lines.append("")
        lines.extend(
            [
                "💬 群内反馈",
                "",
                "MiniMax 暂时未返回可用结果，本栏未自动整理。",
                "",
                "🔍 方法与待解决",
                "",
                "方法沉淀：MiniMax 暂时未返回可用结果。",
                "待解决问题：MiniMax 暂时未返回可用结果。",
            ]
        )
        return "\n".join(lines).strip()

    def _render_report(
        self,
        report_date: str,
        messages: Sequence[StoredMessage],
        facts: Dict[str, Any],
        analysis: str,
        generated_at: datetime,
    ) -> str:
        day = datetime.strptime(report_date, "%Y-%m-%d").date()
        total = len(facts["roster"])
        assignment_label = facts["assignment_label"]
        assignment_short = "图片" if assignment_label == "图片作业" else assignment_label
        both = facts["both"]
        only_homework = facts["only_homework"]
        only_reviews = facts["only_reviews"]
        none = facts["none"]
        pending = list(facts.get("pending_members", ()))
        pending_set = set(pending)
        missing_review = [name for name in facts["roster"] if name not in facts["review_members"]]
        missing_homework = [
            name
            for name in facts["roster"]
            if name not in facts["homework_members"] and name not in pending_set
        ]
        cutoff = (
            generated_at
            if generated_at.date() == day
            else max(
                datetime.fromtimestamp(message.create_time_ms / 1000, tz=self.settings.tz)
                for message in messages
            )
        )
        lines = [
            self.settings.report_title,
            "",
            f"日期：{day.year} 年 {day.month} 月 {day.day} 日（截至 {cutoff:%H:%M}）",
            "",
            "",
            "📊 今日总览",
            "",
            f"群成员总数：{total} 人",
            f"群内消息：{len(messages)} 条（含图片 {facts['image_count']} 张）",
            f"完成{assignment_label}：{len(facts['homework_members'])}/{total}",
            f"完成复盘作业：{len(facts['review_members'])}/{total}",
            f"两项均完成：{len(both)}/{total}",
            f"未完成任意一项：{total - len(both)} 人",
            "",
            "",
            "✅ 完成情况",
            "",
            f"{assignment_label}（{len(facts['homework_members'])}/{total}）",
            "",
            f"复盘作业（{len(facts['review_members'])}/{total}）",
            "",
            f"两项均完成（{len(both)} 人）：",
            self._names(both),
            "",
            f"仅完成{assignment_short}（{len(only_homework)} 人）：",
            self._names(only_homework),
            "",
            f"仅完成复盘（{len(only_reviews)} 人）：",
            self._names(only_reviews),
            "",
            "",
            "⚠️ 未完成人员",
            "",
            f"缺复盘（{len(missing_review)} 人）：",
            self._names(missing_review),
            "",
            f"缺{assignment_short}（{len(missing_homework)} 人）：",
            self._names(missing_homework),
            "",
            f"两项均未完成（{len(none)} 人）：",
            self._names(none),
        ]
        if pending:
            overview_index = lines.index(
                f"完成{assignment_label}：{len(facts['homework_members'])}/{total}"
            )
            lines.insert(overview_index + 1, f"待核验：{len(pending)}/{total}")
            completion_index = lines.index(f"复盘作业（{len(facts['review_members'])}/{total}）")
            lines[completion_index + 1 : completion_index + 1] = [
                "",
                f"待核验（{len(pending)} 人）：",
                self._names(pending),
            ]
        if not only_reviews:
            lines.extend(["", f"说明：今日无“仅缺{assignment_short}但已交复盘”的人员。"])
        lines.extend(
            [
                "",
                "",
                analysis.strip() or f"📝 每日复盘（{len(facts['review_members'])} 人）\n\n无",
                "",
                "",
                "📎 打卡表链接",
                "",
                f"[点击查看打卡表]({self.settings.report_link})",
                "",
                "",
                "本日报由系统自动生成，仅统计可读文字内容，图片仅计数量不识别内容。",
            ]
        )
        return "\n".join(lines)

    def _welcome_guide_text(self) -> str:
        lines = [
            "知识库助手・群内使用指南",
            "",
            "👋 我会在本群记录作业、复盘和提交时间，并同步到本群独立的打卡表。",
            "",
            "✅ 怎么交作业",
            "• “已完成 / 已提交 / 作业提交”都可作为声明；请同时或 10 分钟内发图片、视频、文件、成果链接或完整作品正文。",
            "• 只有声明没有作品时先记为“待核验”；补上证据后自动改为正常提交或补卡。",
            "• 复盘请带 #复盘；想要文字点评可再加 #求反馈。",
            "• 补交按作业证据的真实发送时间判定；只说“我补交了”不会直接改状态。",
            "• 图片会记录为作业证据，但不识别图片内容。",
            "",
            "🔎 @我可以问",
            "• “谁还没交”、“第2次作业”、“前三次作业”",
            "• “查询某人全部打卡记录”、“我的战绩”",
            "• “打开打卡表”、“菜单”",
            "",
            "📌 需要我回复时请直接 @知识库助手；普通群聊默认只做记录。",
        ]
        if self.settings.report_link:
            lines.extend(
                [
                    "",
                    "📎 本群打卡表",
                    f"[点击查看打卡表]({self.settings.report_link})",
                ]
            )
        return "\n".join(lines)

    def send_welcome_guide(self, chat_id: str, *, new_group_only: bool = True) -> str:
        """机器人首次进入新群时发送一次指南。

        历史数据库中已出现过的群不补发，避免上线功能时打扰现有群。
        """
        with self._welcome_guide_lock:
            if not self.settings.send_enabled or not self._chat_allowed(chat_id):
                return ""
            if self.store.welcome_guide_sent(chat_id):
                return ""
            if new_group_only and self.store.chat_known(chat_id):
                self.store.mark_welcome_guide_sent(chat_id, "")
                logger.info("已有历史消息，不补发入群指南：chat=%s", chat_id)
                return ""
            message_id = self.api.send_post(
                chat_id,
                self._welcome_guide_text(),
                f"welcome-guide-{chat_id}",
            )
            self.store.mark_welcome_guide_sent(chat_id, message_id)
            logger.info("已发送新群使用指南：chat=%s message=%s", chat_id, message_id)
            return message_id

    def handle_message(self, message: IncomingMessage) -> bool:
        if message.sender_type != "user" or message.chat_type != "group":
            return False
        if not self._chat_allowed(message.chat_id):
            return False
        if self._is_excluded(message.sender_open_id):
            logger.info("忽略被排除成员的消息：%s", message.message_id)
            return False

        text = self._message_text(message).strip()
        if not text:
            return False
        submitted_at = datetime.fromtimestamp(message.create_time_ms / 1000, tz=self.settings.tz)

        mentioned_query = self._mentioned_query(text)
        if mentioned_query in _MENU_SHORTCUTS:
            mentioned_query = _MENU_SHORTCUTS[mentioned_query]
        command_text = mentioned_query if mentioned_query is not None else text
        if self._is_summary_command(command_text) or (
            mentioned_query is not None and "日报" in mentioned_query
        ):
            if not self.settings.send_enabled:
                logger.info("发送已关闭，忽略群内总结指令：%s", message.message_id)
                return False
            if notice := self._course_assignment_limit_notice(command_text, submitted_at.date()):
                self.api.reply_text(
                    message.message_id,
                    notice,
                    f"summary-course-limit-{message.message_id}",
                )
                return True
            requested_report_date = self._query_report_date(command_text, submitted_at.date())
            if self.settings.course_has_ended(requested_report_date):
                phase = self.settings.course_phase_for_reference(
                    requested_report_date,
                    self._course_name_hint(command_text),
                )
                phase_name = phase.name if phase is not None else "课程"
                phase_end = phase.end_date if phase is not None else ""
                self.api.reply_text(
                    message.message_id,
                    (
                        f"本轮{phase_name}已于{phase_end}结束，"
                        "结束日之后不再生成每日日报。"
                        f"可以问我“{phase_name}整体”或指定历史作业。"
                    ),
                    f"summary-course-ended-{message.message_id}",
                )
                return True
            result = self.build_summary(requested_report_date, message.chat_id)
            if result is None:
                reply = f"{requested_report_date} 还没有收到可总结的群消息。"
            else:
                reply = result.text
            self.api.reply_post(message.message_id, reply, f"summary-command-{message.message_id}")
            return True

        stored = StoredMessage(
            message_id=message.message_id,
            chat_id=message.chat_id,
            sender_open_id=message.sender_open_id,
            sender_name=self._resolve_sender_name(message.chat_id, message.sender_open_id),
            message_type=message.message_type,
            content=text,
            create_time_ms=message.create_time_ms,
            parent_id=message.parent_id or "",
            root_id=message.root_id or "",
            thread_id=message.thread_id or "",
        )
        inserted = self.store.add_message(stored)
        if inserted:
            logger.info("已收集群消息：chat=%s message=%s", message.chat_id, message.message_id)
        elif _FEEDBACK_REQUEST.search(text):
            return False
        if self._record_iteration(text, stored):
            return True
        report_dates: List[str] = []
        if inserted:
            report_dates = self._submission_report_dates(
                text,
                submitted_at.date(),
                allow_embedded=(
                    stored.message_type == "merge_forward"
                    and not stored.content.startswith(_MULTI_MERGE_PREFIX)
                ),
            )
            has_artifact = bool(self._homework_evidence_kind(stored, strict=True))
            attachment_trigger = (
                stored.message_type in _THREAD_HOMEWORK_TYPES
                or bool(_RESOURCE_TOKEN.search(stored.content))
                or self._is_thread_homework(stored)
            )
            if report_dates or has_artifact:
                nearby_messages = self.store.list_messages(
                    stored.chat_id,
                    stored.create_time_ms - _LATE_TAG_WINDOW_MS,
                    stored.create_time_ms + _LATE_TAG_WINDOW_MS + 1,
                    self.settings.max_messages,
                )
                nearby_messages = self._apply_member_aliases(
                    [
                        candidate
                        for candidate in nearby_messages
                        if candidate.sender_open_id == stored.sender_open_id
                    ]
                )
                report_dates = sorted(
                    set(report_dates)
                    | {
                        nearby_report_date
                        for _declaration, nearby_report_date in self._submission_declarations(
                            nearby_messages
                        )
                    }
                )
            if (
                not report_dates
                and attachment_trigger
                and not stored.content.startswith(_MULTI_MERGE_PREFIX)
                and (
                    not self.settings.configured_course_phases
                    or self.settings.course_is_active(submitted_at.date())
                )
            ):
                report_dates = [self._implicit_submission_report_date(text, submitted_at)]
            for report_date in report_dates:
                try:
                    self.sync_attendance_date(report_date, message.chat_id)
                except Exception:
                    logger.exception(
                        "新提交消息触发打卡同步失败：%s %s",
                        report_date,
                        message.message_id,
                    )
            self._maybe_react_to_homework(stored, report_dates, submitted_at)
            if _FEEDBACK_REQUEST.search(text):
                if self._maybe_answer_feedback(stored, report_dates):
                    return True
        if mentioned_query is not None and self.settings.send_enabled:
            if _MENU_INTENT.search(mentioned_query):
                self.api.reply_post(
                    message.message_id,
                    self._answer_menu(),
                    f"menu-{message.message_id}",
                )
                return True
            if _TABLE_LINK_INTENT.search(mentioned_query):
                self.api.reply_text(
                    message.message_id,
                    self._answer_table_link(),
                    f"table-link-{message.message_id}",
                )
                return True
            if _MY_MISSING_DETAIL_INTENT.search(mentioned_query):
                self.api.reply_post(
                    message.message_id,
                    self._answer_my_missing_details(stored, submitted_at.date()),
                    f"my-missing-details-{message.message_id}",
                )
                return True
            if _MY_STATS_INTENT.search(mentioned_query):
                self.api.reply_post(
                    message.message_id,
                    self._answer_my_stats(stored, submitted_at.date()),
                    f"my-stats-{message.message_id}",
                )
                return True
            if _WEEKLY_GROWTH_INTENT.search(mentioned_query):
                self.api.reply_post(
                    message.message_id,
                    self._answer_weekly_growth(submitted_at.date(), message.chat_id),
                    f"weekly-growth-{message.message_id}",
                )
                return True
            multi_cycle_reply = self._answer_multi_cycle_stats(
                mentioned_query,
                submitted_at.date(),
            )
            if multi_cycle_reply is not None:
                self.api.reply_post(
                    message.message_id,
                    multi_cycle_reply,
                    f"multi-cycle-stats-{message.message_id}",
                )
                return True
            if _REMINDER_EXPLANATION_INTENT.search(mentioned_query):
                self.api.reply_text(
                    message.message_id,
                    self._answer_reminder_explanation(),
                    f"reminder-explanation-{message.message_id}",
                )
                return True
            leader_override = self._leader_override_request(mentioned_query)
            override_question = mentioned_query
            if leader_override is None and stored.sender_open_id in self.settings.leader_member_ids:
                semantic_override = self._semantic_leader_override_request(
                    mentioned_query,
                    submitted_at.date(),
                )
                if semantic_override is not None:
                    target_names, status, override_question = semantic_override
                    leader_override = target_names, status
            if leader_override is not None:
                target_names, status = leader_override
                reply = self._answer_leader_attendance_override(
                    target_names=target_names,
                    status=status,
                    question=override_question,
                    reference_day=submitted_at.date(),
                    message=stored,
                )
                self.api.reply_text(
                    message.message_id,
                    reply,
                    f"leader-override-{message.message_id}",
                )
                return True
            if self._is_makeup_verification_request(mentioned_query):
                reply = self._answer_self_makeup_verification(
                    mentioned_query,
                    submitted_at.date(),
                    stored,
                )
                self.api.reply_text(
                    message.message_id,
                    reply,
                    f"self-makeup-{message.message_id}",
                )
                return True
            if _CONVERSATIONAL_HELP_INTENT.search(
                mentioned_query
            ) and not _EXPLICIT_STATS_QUERY_SIGNAL.search(mentioned_query):
                if self._maybe_social_chat(
                    stored,
                    prompt_text=mentioned_query,
                    direct=True,
                ):
                    return True
            reply = self._answer_stats_question(
                mentioned_query, submitted_at.date(), message.chat_id
            )
            use_post = bool(_MEMBER_HISTORY_INTENT.search(mentioned_query))
            if reply is None and _SEMANTIC_STATS_SIGNAL.search(mentioned_query):
                semantic_answer = self._semantic_query_answer(
                    mentioned_query,
                    submitted_at.date(),
                    message.chat_id,
                )
                if semantic_answer is not None:
                    reply, use_post = semantic_answer
            if reply is None:
                if self._maybe_social_chat(
                    stored,
                    prompt_text=mentioned_query,
                    direct=True,
                ):
                    return True
                if self.settings.social_chat_enabled:
                    reply = (
                        "我在。刚才这句我没理解准，你换个说法再问一次；"
                        "作业统计、群规则、项目卡点或普通问题都可以直接问。"
                    )
                else:
                    reply = "我目前只支持查询作业、复盘、未交名单和日报。"
            if use_post:
                self.api.reply_post(
                    message.message_id,
                    reply,
                    f"member-history-{message.message_id}",
                )
            else:
                self.api.reply_text(
                    message.message_id,
                    reply,
                    f"stats-question-{message.message_id}",
                )
            return True
        if inserted:
            replying_to_bot = self.store.social_chat_parent_known(
                stored.parent_id
            ) or self.store.social_chat_parent_known(stored.root_id)
            if self._maybe_social_chat(
                stored,
                prompt_text=text,
                direct=replying_to_bot,
            ):
                return True
        return inserted

    def _load_messages(self, report_date: str, chat_id: str) -> List[StoredMessage]:
        day = datetime.strptime(report_date, "%Y-%m-%d").date()
        start_ms, end_ms = _day_range_ms(day, self.settings.tz)
        messages = self.store.list_messages(
            chat_id, start_ms, end_ms, limit=self.settings.max_messages
        )
        return [
            message
            for message in messages
            if message.sender_open_id not in self.settings.excluded_member_ids
        ]

    @staticmethod
    def _canonicalize_visible_names(content: str, aliases: Dict[str, str]) -> str:
        for old_name, new_name in aliases.items():
            content = content.replace(f"@{old_name}", f"@{new_name}")
            if old_name.startswith(("飞书用户", "用户")):
                content = content.replace(old_name, new_name)
        return content

    @staticmethod
    def _transcript_lines(
        messages: Sequence[StoredMessage],
        tz: Any,
        visible_name_aliases: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        lines: List[str] = []
        for message in messages:
            sent_at = datetime.fromtimestamp(message.create_time_ms / 1000, tz=tz)
            content = GroupSummaryService._canonicalize_visible_names(
                " ".join(message.content.split()),
                visible_name_aliases or {},
            )
            lines.append(f"[{sent_at:%H:%M}] {message.sender_name}：{content}")
        return lines

    @staticmethod
    def _participant_lines(messages: Sequence[StoredMessage], tz: Any) -> List[str]:
        activity: Dict[str, Dict[str, Any]] = {}
        for message in messages:
            item = activity.setdefault(
                message.sender_open_id,
                {
                    "name": message.sender_name,
                    "count": 0,
                    "first": message.create_time_ms,
                    "last": message.create_time_ms,
                },
            )
            item["count"] += 1
            item["first"] = min(item["first"], message.create_time_ms)
            item["last"] = max(item["last"], message.create_time_ms)

        lines: List[str] = []
        for item in sorted(activity.values(), key=lambda value: value["first"]):
            first = datetime.fromtimestamp(item["first"] / 1000, tz=tz)
            last = datetime.fromtimestamp(item["last"] / 1000, tz=tz)
            time_range = f"{first:%H:%M}" if first == last else f"{first:%H:%M}–{last:%H:%M}"
            lines.append(f"- {item['name']}：{item['count']} 条，{time_range}")
        return lines

    def build_summary(self, report_date: str, chat_id: str) -> Optional[SummaryResult]:
        if self.settings.configured_course_phases and not self.settings.course_is_active(
            report_date
        ):
            logger.info("当天不在已配置的课程阶段内，不生成日报：%s", report_date)
            return None
        messages = self._named_messages(report_date, chat_id)
        if not messages:
            return None
        participants = {message.sender_open_id for message in messages}
        homework_messages = self._assignment_window_messages(
            report_date,
            chat_id,
            self.settings.missing_list_hour,
            self.settings.missing_list_minute,
        )
        facts = self._completion_facts(report_date, messages, homework_messages=homework_messages)
        self._persist_attendance(report_date, homework_messages or messages, facts)
        try:
            summary = self.summarizer.summarize(
                report_date,
                self._transcript_lines(
                    messages,
                    self.settings.tz,
                    self.settings.visible_name_aliases,
                ),
                report_context=self._report_context(facts),
            )
        except Exception:
            logger.exception("MiniMax 总结失败，改用可核验原文兜底：%s %s", report_date, chat_id)
            summary = self._fallback_analysis(messages, facts)
        generated_at = datetime.now(tz=self.settings.tz)
        text = self._render_report(report_date, messages, facts, summary, generated_at)
        return SummaryResult(
            chat_id=chat_id,
            report_date=report_date,
            text=text[:120_000],
            message_count=len(messages),
            participant_count=len(participants),
            generated_at=generated_at,
        )

    def send_summary(
        self, report_date: str, chat_id: str, *, force: bool = False, dry_run: bool = False
    ) -> Optional[SummaryResult]:
        if not self.settings.send_enabled and not dry_run:
            logger.info("发送已关闭，仅生成本地预览：%s %s", report_date, chat_id)
            dry_run = True
        if not force and self.store.summary_sent(report_date, chat_id):
            logger.info("群聊总结已发送，跳过：%s %s", report_date, chat_id)
            return None
        result = self.build_summary(report_date, chat_id)
        if result is None or dry_run:
            return result
        result.message_id = self.api.send_post(
            chat_id, result.text, f"group-summary-{report_date}-{chat_id}"[:50]
        )
        self.store.mark_summary_sent(report_date, chat_id, result.message_id or "")
        return result

    def build_daily_brief(self, report_date: str, chat_id: str) -> SummaryResult:
        """生成 23:00 群内简报：更新打卡表，但不调用模型生成长日报。"""
        day = datetime.strptime(report_date, "%Y-%m-%d").date()
        assignment_date = self.settings.assignment_report_date(report_date)
        messages = self._named_messages(report_date, chat_id)
        due_hour, due_minute = self.settings.assignment_due_clock(assignment_date)
        homework_messages = self._assignment_window_messages(
            assignment_date,
            chat_id,
            due_hour,
            due_minute,
        )
        late_messages = self._post_deadline_messages(assignment_date, chat_id)
        facts = self._completion_facts(
            assignment_date,
            messages,
            homework_messages=homework_messages,
        )
        self._persist_attendance(
            assignment_date,
            homework_messages or late_messages or messages,
            facts,
        )
        completed = list(facts["homework_members"])
        late = list(facts["late_members"])
        pending = list(facts.get("pending_members", ()))
        normal = [name for name in completed if name not in set(late)]
        total = len(facts["roster"])
        missing_count = total - len(completed) - len(pending)
        completion_detail = (
            f"正常 {len(normal)}，补卡 {len(late)}，待核验 {len(pending)}，未交 {missing_count}"
            if pending
            else f"正常 {len(normal)}，补卡 {len(late)}，未交 {missing_count}"
        )
        lines = [
            self.settings.report_title,
            "",
            f"日期：{day.year} 年 {day.month} 月 {day.day} 日（截至 23:00）",
            (
                f"作业周期："
                f"{(self.settings.course_phase(assignment_date).name + '・') if self.settings.course_phase(assignment_date) else ''}"
                f"{self._assignment_period_label(assignment_date)}"
            ),
            "",
            "📊 今日打卡",
            "",
            f"应统计：{total} 人",
            f"作业完成：{len(completed)}/{total}（{completion_detail}）",
            f"当日复盘：{len(facts['review_members'])}/{total}",
            f"群内消息：{len(messages)} 条（含图片 {facts['image_count']} 张）",
        ]
        if self.settings.report_link:
            lines.extend(["", "📎 今日打卡表", "", f"[点击查看]({self.settings.report_link})"])
        return SummaryResult(
            chat_id=chat_id,
            report_date=report_date,
            text="\n".join(lines),
            message_count=len(messages),
            participant_count=len({message.sender_open_id for message in messages}),
            generated_at=datetime.now(tz=self.settings.tz),
        )

    def send_daily_brief(self, report_date: str, chat_id: str) -> Optional[SummaryResult]:
        if not self.settings.send_enabled:
            return None
        if self.store.summary_sent(report_date, chat_id):
            logger.info("每日简报已发送，跳过：%s %s", report_date, chat_id)
            return None
        result = self.build_daily_brief(report_date, chat_id)
        result.message_id = self.api.send_post(
            chat_id,
            result.text,
            f"daily-brief-{report_date}-{chat_id}"[:50],
        )
        self.store.mark_summary_sent(report_date, chat_id, result.message_id or "")
        return result

    def send_due_summaries(self, report_date: Optional[str] = None) -> List[SummaryResult]:
        if not self.settings.send_enabled:
            logger.info("发送已关闭，跳过定时总结")
            return []
        day = report_date or self._scheduled_report_date()
        if self.settings.assignment_is_paused(day):
            logger.info("当天是课程缓冲日，跳过全员日报：%s", day)
            return []
        if self.settings.configured_course_phases and not self.settings.course_is_active(day):
            logger.info("当天不在已配置的课程阶段内，跳过定时简报：%s", day)
            return []
        chat_ids = sorted(set(self.settings.chat_ids) | set(self.store.list_chats()))
        if not chat_ids:
            logger.warning("还没有已知群聊；把机器人加入群并发一条消息后会自动记住")
            return []
        results: List[SummaryResult] = []
        for chat_id in chat_ids:
            try:
                result = self.send_daily_brief(day, chat_id)
                if result:
                    results.append(result)
            except Exception:
                logger.exception("发送群聊总结失败：%s %s", day, chat_id)
        return results

    def send_reminder(self, report_date: str, chat_id: str) -> str:
        report_date = self.settings.assignment_report_date(report_date)
        if not self.settings.send_enabled or not self.settings.reminder_enabled:
            return ""
        if self.store.reminder_sent(report_date, chat_id):
            logger.info("打卡提醒已发送，跳过：%s %s", report_date, chat_id)
            return ""
        messages = self._named_messages(report_date, chat_id)
        homework_messages = self._assignment_window_messages(
            report_date,
            chat_id,
            self.settings.reminder_hour,
            self.settings.reminder_minute,
        )
        if not messages and not homework_messages:
            logger.info("当天没有可统计消息，不发送提醒：%s %s", report_date, chat_id)
            return ""
        facts = self._completion_facts(report_date, messages, homework_messages=homework_messages)
        if not facts["assignment_detected"]:
            logger.info("当天未识别到作业，不发送催交：%s %s", report_date, chat_id)
            return ""
        self._persist_attendance(report_date, homework_messages or messages, facts)
        homework_completed = set(facts["homework_members"])
        pending = list(facts.get("pending_members", ()))
        pending_set = set(pending)
        homework_missing = [
            name
            for name in facts["roster"]
            if name not in homework_completed and name not in pending_set
        ]
        union_missing = list(homework_missing) + pending
        open_id_by_name = {name: open_id for open_id, name in self.settings.member_aliases.items()}
        open_id_by_name.update(
            {message.sender_name: message.sender_open_id for message in messages}
        )
        mentions = [
            (open_id_by_name[name], name) for name in union_missing if open_id_by_name.get(name)
        ]
        if not mentions:
            return ""
        message_id = self.api.send_attendance_reminder(
            chat_id,
            mentions,
            homework_missing,
            [],
            f"attendance-reminder-{report_date}-{chat_id}"[:50],
            pending_members=pending,
        )
        self.store.mark_reminder_sent(report_date, chat_id, message_id)
        return message_id

    def send_due_reminders(self, report_date: Optional[str] = None) -> List[str]:
        if not self.settings.send_enabled or not self.settings.reminder_enabled:
            return []
        day = report_date or datetime.now(tz=self.settings.tz).date().isoformat()
        assignment_date = self.settings.assignment_due_report_date(day)
        if assignment_date is None:
            logger.info("今天不是作业截止日，跳过催交：%s", day)
            return []
        message_ids: List[str] = []
        for chat_id in sorted(set(self.settings.chat_ids) | set(self.store.list_chats())):
            try:
                if message_id := self.send_reminder(assignment_date, chat_id):
                    message_ids.append(message_id)
            except Exception:
                logger.exception("发送打卡提醒失败：%s %s", day, chat_id)
        return message_ids

    def send_missing_list(self, report_date: str, chat_id: str) -> str:
        report_date = self.settings.assignment_report_date(report_date)
        if not self.settings.send_enabled or not self.settings.missing_list_enabled:
            return ""
        if self.store.missing_list_sent(report_date, chat_id):
            logger.info("未交作业名单已发送，跳过：%s %s", report_date, chat_id)
            return ""
        messages = self._named_messages(report_date, chat_id)
        homework_messages = self._assignment_window_messages(
            report_date,
            chat_id,
            self.settings.missing_list_hour,
            self.settings.missing_list_minute,
        )
        facts = self._completion_facts(report_date, messages, homework_messages=homework_messages)
        if not facts["assignment_detected"]:
            logger.info("当天未识别到作业，不发送未交名单：%s %s", report_date, chat_id)
            return ""
        self._persist_attendance(report_date, homework_messages or messages, facts)
        completed = set(facts["homework_members"])
        pending = list(facts.get("pending_members", ()))
        pending_set = set(pending)
        missing = [
            name for name in facts["roster"] if name not in completed and name not in pending_set
        ]
        lines = [
            (
                f"{self.settings.missing_list_hour:02d}:"
                f"{self.settings.missing_list_minute:02d} 未交作业名单"
            ),
            "",
            f"{facts['assignment_label']}已完成 {len(completed)}/{len(facts['roster'])}",
        ]
        if pending:
            lines.extend(
                [
                    f"待核验（{len(pending)}人）：",
                    self._names(pending),
                ]
            )
        lines.extend(
            [
                f"未完成（{len(missing)}人）：",
                self._names(missing),
            ]
        )
        text = "\n".join(lines)
        message_id = self.api.send_post(
            chat_id,
            text,
            f"missing-homework-list-{report_date}-{chat_id}"[:50],
        )
        self.store.mark_missing_list_sent(report_date, chat_id, message_id)
        return message_id

    def send_due_missing_lists(self, report_date: Optional[str] = None) -> List[str]:
        if not self.settings.send_enabled or not self.settings.missing_list_enabled:
            return []
        day = report_date or datetime.now(tz=self.settings.tz).date().isoformat()
        assignment_date = self.settings.assignment_due_report_date(day)
        if assignment_date is None:
            logger.info("今天不是作业截止日，跳过未交名单：%s", day)
            return []
        message_ids: List[str] = []
        for chat_id in sorted(set(self.settings.chat_ids) | set(self.store.list_chats())):
            try:
                if message_id := self.send_missing_list(assignment_date, chat_id):
                    message_ids.append(message_id)
            except Exception:
                logger.exception("发送未交作业名单失败：%s %s", day, chat_id)
        return message_ids

    def send_final_status(self, report_date: str, chat_id: str) -> str:
        report_date = self.settings.assignment_report_date(report_date)
        if not self.settings.send_enabled or not self.settings.final_status_enabled:
            return ""
        if self.store.final_status_sent(report_date, chat_id):
            logger.info("最终打卡汇总已发送，跳过：%s %s", report_date, chat_id)
            return ""
        messages = self._named_messages(report_date, chat_id)
        homework_messages = self._assignment_window_messages(
            report_date,
            chat_id,
            *self.settings.assignment_due_clock(report_date),
        )
        facts = self._completion_facts(report_date, messages, homework_messages=homework_messages)
        if not facts["assignment_detected"]:
            logger.info("当期未识别到作业，不发送最终汇总：%s %s", report_date, chat_id)
            return ""
        self._persist_attendance(report_date, homework_messages or messages, facts)
        completed = list(facts["homework_members"])
        completed_set = set(completed)
        pending = list(facts.get("pending_members", ()))
        pending_set = set(pending)
        missing = [
            name
            for name in facts["roster"]
            if name not in completed_set and name not in pending_set
        ]
        lines = [
            f"{self._assignment_period_label(report_date)}・打卡汇总",
            "",
            f"{facts['assignment_label']}已完成 {len(completed)}/{len(facts['roster'])}",
            f"已完成（{len(completed)}人）：",
            self._names(completed),
            "",
        ]
        if pending:
            lines.extend(
                [
                    f"待核验（{len(pending)}人）：",
                    self._names(pending),
                    "",
                ]
            )
        lines.extend(
            [
                f"未完成（{len(missing)}人）：",
                self._names(missing),
            ]
        )
        text = "\n".join(lines)
        message_id = self.api.send_post(
            chat_id,
            text,
            f"final-attendance-{report_date}-{chat_id}"[:50],
        )
        self.store.mark_final_status_sent(report_date, chat_id, message_id)
        return message_id

    def send_due_final_statuses(self, report_date: Optional[str] = None) -> List[str]:
        if not self.settings.send_enabled or not self.settings.final_status_enabled:
            return []
        day = report_date or datetime.now(tz=self.settings.tz).date().isoformat()
        assignment_date = self.settings.assignment_due_report_date(day)
        if assignment_date is None:
            logger.info("今天不是作业截止日，跳过最终汇总：%s", day)
            return []
        message_ids: List[str] = []
        for chat_id in sorted(set(self.settings.chat_ids) | set(self.store.list_chats())):
            try:
                if message_id := self.send_final_status(assignment_date, chat_id):
                    message_ids.append(message_id)
            except Exception:
                logger.exception("发送最终打卡汇总失败：%s %s", assignment_date, chat_id)
        return message_ids

    def _makeup_facts(self, report_date: str, chat_id: str) -> Optional[Dict[str, Any]]:
        report_date = self.settings.assignment_report_date(report_date)
        messages = self._named_messages(report_date, chat_id)
        homework_messages = self._assignment_window_messages(
            report_date,
            chat_id,
            *self.settings.assignment_due_clock(report_date),
        )
        if not messages and not homework_messages:
            return None
        facts = self._completion_facts(report_date, messages, homework_messages=homework_messages)
        if not facts["assignment_detected"]:
            return None
        self._persist_attendance(report_date, homework_messages or messages, facts)
        return facts

    def send_makeup_reminder(self, report_date: str, chat_id: str) -> str:
        report_date = self.settings.assignment_report_date(report_date)
        if not self.settings.send_enabled or not self.settings.makeup_reminder_enabled:
            return ""
        if self.store.makeup_reminder_sent(report_date, chat_id):
            logger.info("补交提醒已发送，跳过：%s %s", report_date, chat_id)
            return ""
        facts = self._makeup_facts(report_date, chat_id)
        if facts is None:
            logger.info("当期未识别到作业，不发送补交提醒：%s %s", report_date, chat_id)
            return ""
        completed = set(facts["homework_members"])
        pending = list(facts.get("pending_members", ()))
        pending_set = set(pending)
        missing = [
            name for name in facts["roster"] if name not in completed and name not in pending_set
        ]
        if not missing and not pending:
            return ""
        open_id_by_name = {name: open_id for open_id, name in self.settings.member_aliases.items()}
        mentions = [
            (open_id_by_name[name], name) for name in missing + pending if open_id_by_name.get(name)
        ]
        if not mentions:
            return ""
        message_id = self.api.send_makeup_reminder(
            chat_id,
            mentions,
            missing,
            f"makeup-reminder-{report_date}-{chat_id}"[:50],
            pending_members=pending,
        )
        self.store.mark_makeup_reminder_sent(report_date, chat_id, message_id)
        return message_id

    def send_due_makeup_reminders(self, report_date: Optional[str] = None) -> List[str]:
        if not self.settings.send_enabled or not self.settings.makeup_reminder_enabled:
            return []
        day = report_date or datetime.now(tz=self.settings.tz).date().isoformat()
        if not self.settings.is_makeup_day(day):
            logger.info("今天不是补交日，跳过补交提醒：%s", day)
            return []
        assignment_date = self.settings.makeup_report_date(day)
        message_ids: List[str] = []
        for chat_id in sorted(set(self.settings.chat_ids) | set(self.store.list_chats())):
            try:
                if message_id := self.send_makeup_reminder(assignment_date, chat_id):
                    message_ids.append(message_id)
            except Exception:
                logger.exception("发送补交提醒失败：%s %s", assignment_date, chat_id)
        return message_ids

    def send_makeup_summary(self, report_date: str, chat_id: str) -> str:
        report_date = self.settings.assignment_report_date(report_date)
        if not self.settings.send_enabled or not self.settings.makeup_summary_enabled:
            return ""
        if self.store.makeup_summary_sent(report_date, chat_id):
            logger.info("补交汇总已发送，跳过：%s %s", report_date, chat_id)
            return ""
        facts = self._makeup_facts(report_date, chat_id)
        if facts is None:
            logger.info("当期未识别到作业，不发送补交汇总：%s %s", report_date, chat_id)
            return ""
        late = list(facts["late_members"])
        late_set = set(late)
        completed = list(facts["homework_members"])
        completed_set = set(completed)
        normal = [name for name in completed if name not in late_set]
        pending = list(facts.get("pending_members", ()))
        pending_set = set(pending)
        missing = [
            name
            for name in facts["roster"]
            if name not in completed_set and name not in pending_set
        ]
        total = len(facts["roster"])
        lines = [
            f"{self._assignment_period_label(report_date)}・补交汇总",
            "",
            f"正常提交：{len(normal)}/{total}",
            f"已补交：{len(late)}/{total}",
        ]
        if pending:
            lines.append(f"待核验：{len(pending)}/{total}")
        lines.extend(
            [
                f"最终完成：{len(completed)}/{total}",
                f"仍未交：{len(missing)}/{total}",
                "",
                f"已补交（{len(late)}人）：",
                self._names(late),
                "",
            ]
        )
        if pending:
            lines.extend(
                [
                    f"待核验（{len(pending)}人）：",
                    self._names(pending),
                    "",
                ]
            )
        lines.extend(
            [
                f"仍未交（{len(missing)}人）：",
                self._names(missing),
                "",
                "说明：补交阶段已结束，此时仍未交者记为旷卡。",
            ]
        )
        text = "\n".join(lines)
        message_id = self.api.send_post(
            chat_id,
            text,
            f"makeup-summary-{report_date}-{chat_id}"[:50],
        )
        self.store.mark_makeup_summary_sent(report_date, chat_id, message_id)
        return message_id

    def send_due_makeup_summaries(self, report_date: Optional[str] = None) -> List[str]:
        if not self.settings.send_enabled or not self.settings.makeup_summary_enabled:
            return []
        day = report_date or datetime.now(tz=self.settings.tz).date().isoformat()
        if not self.settings.is_makeup_day(day):
            logger.info("今天不是补交日，跳过补交汇总：%s", day)
            return []
        assignment_date = self.settings.makeup_report_date(day)
        message_ids: List[str] = []
        for chat_id in sorted(set(self.settings.chat_ids) | set(self.store.list_chats())):
            try:
                if message_id := self.send_makeup_summary(assignment_date, chat_id):
                    message_ids.append(message_id)
            except Exception:
                logger.exception("发送补交汇总失败：%s %s", assignment_date, chat_id)
        return message_ids

    def _scheduled_report_date(self, now: Optional[datetime] = None) -> str:
        moment = now or datetime.now(tz=self.settings.tz)
        day = moment.date()
        if self.settings.summary_hour == 0 and self.settings.summary_minute == 0:
            day -= timedelta(days=1)
        return day.isoformat()
