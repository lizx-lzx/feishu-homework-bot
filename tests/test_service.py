import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from daily_report_bot.config import Settings
from daily_report_bot.models import AttendanceRecord, IncomingMessage, StoredMessage
from daily_report_bot.router import GroupServiceRouter
from daily_report_bot.service import GroupSummaryService
from daily_report_bot.store import LocalStore


class FakeApi:
    def __init__(self):
        self.replies = []
        self.sent = []
        self.message_items = {}
        self.reminders = []
        self.makeup_reminders = []
        self.base_records = []
        self.base_updates = []
        self.reactions = []

    def get_member_name(self, chat_id, open_id):
        return {"ou_1": "小李", "ou_2": "小王"}.get(open_id, "")

    def check_bot(self):
        return "知识库助手"

    def get_message_items(self, message_id):
        return self.message_items.get(message_id, [])

    def reply_post(self, message_id, text, uuid):
        self.replies.append((message_id, text, uuid))
        return "om_reply"

    def reply_text(self, message_id, text, uuid):
        self.replies.append((message_id, text, uuid))
        return "om_reply"

    def send_post(self, chat_id, text, uuid):
        self.sent.append((chat_id, text, uuid))
        return "om_summary"

    def add_reaction(self, message_id, emoji_type):
        self.reactions.append((message_id, emoji_type))
        return f"reaction_{len(self.reactions)}"

    def send_attendance_reminder(self, chat_id, members, homework_missing, review_missing, uuid):
        self.reminders.append(
            (chat_id, list(members), list(homework_missing), list(review_missing), uuid)
        )
        return "om_reminder"

    def send_makeup_reminder(self, chat_id, members, homework_missing, uuid):
        self.makeup_reminders.append((chat_id, list(members), list(homework_missing), uuid))
        return "om_makeup_reminder"

    def list_base_records(self, base_token, table_id):
        return list(self.base_records)

    def create_base_record(self, base_token, table_id, fields):
        record_id = f"rec_{len(self.base_records) + 1}"
        self.base_records.append({"record_id": record_id, "fields": dict(fields)})
        return record_id

    def update_base_record(self, base_token, table_id, record_id, fields):
        self.base_updates.append((record_id, dict(fields)))
        for record in self.base_records:
            if record["record_id"] == record_id:
                record["fields"].update(fields)
                break


class MemberLookupFailingApi(FakeApi):
    def get_member_name(self, chat_id, open_id):
        raise RuntimeError("member lookup unavailable")


class FakeSummarizer:
    def __init__(self):
        self.calls = []

    def summarize(self, report_date, transcript_lines, *, report_context=""):
        lines = list(transcript_lines)
        self.calls.append((report_date, lines, report_context))
        return (
            "📝 每日复盘（0 人）\n\n无\n\n"
            "💬 群内反馈\n\n无\n\n"
            "🔍 方法与待解决\n\n方法沉淀：\n无\n\n待解决问题：\n无"
        )


class FakeSemanticSummarizer(FakeSummarizer):
    def interpret_leader_override(self, command, roster):
        assert command == "第三次那份，卫安和米粒都算补了吧"
        assert "卫安" in roster and "米粒" in roster
        return {
            "targets": ("卫安", "米粒"),
            "status": "late",
            "assignment_number": 3,
            "confidence": 0.98,
        }


class FakeSemanticQuerySummarizer(FakeSummarizer):
    def interpret_query(self, command, roster):
        assert command == "第一次还有哪些人掉队了"
        assert tuple(roster) == ("小李", "小王")
        return {
            "intent": "attendance_query",
            "topic": "homework",
            "mode": "missing",
            "assignment_number": 1,
            "target": None,
            "confidence": 0.96,
        }


class FakeFeedbackSummarizer(FakeSummarizer):
    def __init__(self):
        super().__init__()
        self.feedback_calls = []

    def feedback_homework(self, member_name, homework_text):
        self.feedback_calls.append((member_name, homework_text))
        return (
            "亮点：写清了交付成果和遇到的卡点。\n"
            "可继续打磨：可补充关键取舍的原因。\n"
            "下一步：用另一台设备验证公网链接。"
        )


class FakeSocialSummarizer(FakeSummarizer):
    def __init__(self, *decisions):
        super().__init__()
        self.decisions = list(decisions)
        self.social_calls = []

    def decide_social_response(
        self,
        member_name,
        message_text,
        context_lines,
        *,
        direct,
    ):
        self.social_calls.append((member_name, message_text, list(context_lines), direct))
        return self.decisions.pop(0)


def test_transcript_normalizes_known_feishu_display_names():
    message = StoredMessage(
        message_id="om_alias",
        chat_id="oc_group",
        sender_open_id="ou_sender",
        sender_name="成员甲",
        message_type="text",
        content="@飞书用户1234AB 游戏打不开，@旧昵称 也看一下",
        create_time_ms=1_786_947_600_000,
    )

    lines = GroupSummaryService._transcript_lines(
        [message],
        ZoneInfo("Asia/Shanghai"),
        {"飞书用户1234AB": "成员乙", "旧昵称": "新昵称"},
    )

    assert lines == ["[14:20] 成员甲：@成员乙 游戏打不开，@新昵称 也看一下"]


def make_settings(tmp_path: Path, *, send_enabled: bool = True) -> Settings:
    return Settings(
        app_id="cli_test",
        app_secret="secret",
        llm_base_url="https://example.com/v1",
        llm_api_key="key",
        llm_model="test-model",
        chat_ids=(),
        timezone="Asia/Shanghai",
        summary_hour=23,
        summary_minute=0,
        summary_commands=("打开日报", "#总结", "#今日总结"),
        send_enabled=send_enabled,
        max_messages=2000,
        max_chars_per_request=50_000,
        db_path=tmp_path / "state.sqlite3",
        log_level="INFO",
        homework_reaction_enabled=False,
        member_aliases={"ou_1": "小李", "ou_2": "小王"},
        excluded_member_ids=("ou_excluded",),
        report_title="进阶营作业群・每日日报",
        report_members=("小李", "小王"),
        report_link="https://example.com/base",
        review_tag="#复盘",
    )


def incoming(
    message_id: str,
    text: str,
    *,
    sender_open_id: str = "ou_1",
    chat_id: str = "oc_group",
    message_type: str = "text",
    thread_id: str = "",
    parent_id: str = "",
    root_id: str = "",
    created_at: Optional[datetime] = None,
) -> IncomingMessage:
    created = created_at or datetime(2026, 8, 11, 18, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    content = json.dumps({"text": text}, ensure_ascii=False)
    if message_type == "file":
        content = json.dumps(
            {"file_key": f"file_{message_id}", "file_name": text}, ensure_ascii=False
        )
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type="group",
        sender_open_id=sender_open_id,
        sender_type="user",
        message_type=message_type,
        content=content,
        create_time_ms=int(created.timestamp() * 1000),
        parent_id=parent_id or None,
        root_id=root_id or None,
        thread_id=thread_id or None,
    )


def make_service(tmp_path, *, send_enabled: bool = True):
    settings = make_settings(tmp_path, send_enabled=send_enabled)
    api = FakeApi()
    summarizer = FakeSummarizer()
    store = LocalStore(settings.db_path)
    return settings, api, summarizer, store, GroupSummaryService(settings, api, summarizer, store)


def test_collects_group_messages_and_deduplicates(tmp_path):
    _, _, _, store, service = make_service(tmp_path)
    message = incoming("om_1", "完成了联调")
    assert service.handle_message(message) is True
    assert service.handle_message(message) is False
    stored = store.list_messages("oc_group", 0, 9_999_999_999_999, 100)
    assert len(stored) == 1
    assert stored[0].sender_name == "小李"


def test_new_group_welcome_guide_is_native_post_and_sent_once(tmp_path):
    settings, api, _, store, service = make_service(tmp_path)

    first = service.send_welcome_guide("oc_new")
    second = service.send_welcome_guide("oc_new")

    assert first == "om_summary"
    assert second == ""
    assert store.welcome_guide_sent("oc_new") is True
    assert len(api.sent) == 1
    chat_id, text, uuid = api.sent[0]
    assert chat_id == "oc_new"
    assert uuid == "welcome-guide-oc_new"
    assert text.startswith("知识库助手・群内使用指南")
    assert "前三次作业" in text
    assert "图片内容" in text
    assert f"[点击查看打卡表]({settings.report_link})" in text


def test_concurrent_join_and_first_message_still_send_one_welcome_guide(tmp_path):
    _, api, _, _, service = make_service(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.send_welcome_guide("oc_new"), range(2)))

    assert sorted(results) == ["", "om_summary"]
    assert len(api.sent) == 1


def test_welcome_guide_does_not_backfill_a_group_with_history(tmp_path):
    _, api, _, store, service = make_service(tmp_path)
    service.handle_message(incoming("om_old", "历史消息", chat_id="oc_existing"))

    assert service.send_welcome_guide("oc_existing") == ""
    assert api.sent == []
    assert store.welcome_guide_sent("oc_existing") is True


def test_router_uses_first_group_message_as_welcome_fallback(tmp_path):
    chat_id = "oc_new"
    settings = replace(
        make_settings(tmp_path),
        group_databases={chat_id: str(tmp_path / "group_new.sqlite3")},
    )
    api = FakeApi()
    router = GroupServiceRouter(settings, api, FakeSummarizer())

    assert router.handle_message(incoming("om_first", "第一条消息", chat_id=chat_id)) is True
    assert router.handle_message(incoming("om_second", "第二条消息", chat_id=chat_id)) is True

    assert len(api.sent) == 1
    assert api.sent[0][0] == chat_id
    service = router.service_for_chat(chat_id)
    assert service is not None
    assert service.store.welcome_guide_sent(chat_id) is True


def test_homework_reaction_uses_cycle_stage_pools_and_is_idempotent(tmp_path, monkeypatch):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        homework_reaction_enabled=True,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        member_aliases={**original.member_aliases, "ou_1": "小李", "ou_2": "小王"},
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    monkeypatch.setattr("daily_report_bot.service.choice", lambda values: values[-1])

    day_one = incoming(
        "om_day_one_reaction",
        "",
        message_type="image",
        created_at=datetime(2026, 8, 17, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    day_two = incoming(
        "om_day_two_reaction",
        "",
        sender_open_id="ou_2",
        message_type="image",
        created_at=datetime(2026, 8, 18, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    makeup = incoming(
        "om_makeup_reaction",
        "#8月21日 第2次作业已补交\n技术作业\n作业说明：已完成网页部署",
        created_at=datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert service.handle_message(day_one) is True
    assert service.handle_message(day_one) is False
    assert service.handle_message(day_two) is True
    assert service.handle_message(makeup) is True

    assert api.reactions == [
        ("om_day_one_reaction", "FINGERHEART"),
        ("om_day_two_reaction", "PARTY"),
        ("om_makeup_reaction", "Get"),
    ]
    assert store.homework_reaction_sent("om_day_one_reaction") is True


def test_non_homework_message_gets_no_reaction(tmp_path):
    settings = replace(make_settings(tmp_path), homework_reaction_enabled=True)
    api = FakeApi()
    service = GroupSummaryService(
        settings,
        api,
        FakeSummarizer(),
        LocalStore(settings.db_path),
    )

    assert service.handle_message(incoming("om_chat", "今天课程很有意思")) is True
    assert api.reactions == []


def test_existing_message_database_is_migrated_for_topic_fields(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE group_messages (
                message_id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                sender_open_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                message_type TEXT NOT NULL,
                content TEXT NOT NULL,
                create_time_ms INTEGER NOT NULL,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    LocalStore(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(group_messages)")}
    assert {"parent_id", "root_id", "thread_id"} <= columns


def test_member_lookup_failure_does_not_drop_message(tmp_path):
    settings = make_settings(tmp_path)
    api = MemberLookupFailingApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)

    assert (
        service.handle_message(incoming("om_1", "昵称接口暂时不可用", sender_open_id="ou_unknown"))
        is True
    )
    stored = store.list_messages("oc_group", 0, 9_999_999_999_999, 100)
    assert len(stored) == 1
    assert stored[0].sender_name == "成员-nknown"


def test_private_messages_are_ignored(tmp_path):
    _, _, _, store, service = make_service(tmp_path)
    message = incoming("om_1", "这是一条私聊")
    message = IncomingMessage(**{**message.__dict__, "chat_type": "p2p"})
    assert service.handle_message(message) is False
    assert store.list_chats() == []


def test_summary_command_replies_with_today_summary(tmp_path):
    _, api, summarizer, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_1", "完成了联调"))
    service.handle_message(incoming("om_2", "继续测试", sender_open_id="ou_2"))
    assert service.handle_message(incoming("om_3", "打开日报")) is True
    assert len(api.replies) == 1
    assert "群内消息：2 条（含图片 0 张）" in api.replies[0][1]
    assert "进阶营作业群・每日日报" in api.replies[0][1]
    assert "[18:30] 小李：完成了联调" in summarizer.calls[0][1]


def test_bot_mention_can_request_historical_report_after_midnight(tmp_path):
    _, api, summarizer, _, service = make_service(tmp_path)
    report_time = datetime(2026, 8, 16, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    question_time = datetime(2026, 8, 17, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    service.handle_message(
        replace(
            incoming("om_report", "#0816前置作业已完成"),
            create_time_ms=int(report_time.timestamp() * 1000),
        )
    )

    assert (
        service.handle_message(
            replace(
                incoming("om_question", "@知识库助手 我要8-16的日报"),
                create_time_ms=int(question_time.timestamp() * 1000),
            )
        )
        is True
    )

    assert len(api.replies) == 1
    assert "日期：2026 年 8 月 16 日" in api.replies[0][1]
    assert summarizer.calls[0][0] == "2026-08-16"


def test_bot_mention_replies_with_missing_homework_for_requested_date(tmp_path):
    _, api, _, store, service = make_service(tmp_path)
    service.handle_message(incoming("om_done", "#0811前置作业已完成"))

    assert (
        service.handle_message(incoming("om_question", "@知识库助手 现在还有谁没交0811的作业"))
        is True
    )

    assert len(api.replies) == 1
    assert api.replies[0][0] == "om_question"
    assert api.replies[0][1] == (
        "8月11日前置作业已提交 1/2（正常提交 1，已补交 0）。\n"
        "已补交（0人）：无\n"
        "仍未交（1人）：小王"
    )
    attendance = store.list_daily_attendance("2026-08-11")
    assert len(attendance) == 2


def test_bot_mention_replies_with_review_status(tmp_path):
    _, api, _, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_review", "#复盘 0811 今天完成了练习"))

    service.handle_message(incoming("om_question", "@知识库助手 0811谁还没复盘"))

    assert api.replies[-1][1] == "8月11日复盘已完成 1/2。\n未完成（1人）：小王"


def test_bot_mention_can_list_completed_members(tmp_path):
    _, api, _, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_done", "#0811前置作业已完成"))

    service.handle_message(incoming("om_question", "@知识库助手 0811谁完成了作业"))

    assert api.replies[-1][1] == (
        "8月11日前置作业已提交 1/2。\n正常提交（1人）：小李\n已补交（0人）：无"
    )


def test_bot_mention_returns_group_table_link(tmp_path):
    _, api, _, _, service = make_service(tmp_path)

    assert service.handle_message(incoming("om_table", "@知识库助手 打开打卡表")) is True

    assert api.replies == [
        (
            "om_table",
            "本群打卡表：https://example.com/base",
            "table-link-om_table",
        )
    ]


def test_bot_menu_and_numeric_my_stats_shortcut(tmp_path):
    _, api, _, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_done", "#0811前置作业已完成"))

    assert service.handle_message(incoming("om_menu", "@知识库助手 菜单")) is True
    assert "🎮 作业助教菜单" in api.replies[-1][1]
    assert "3  我的战绩" in api.replies[-1][1]

    assert service.handle_message(incoming("om_my_stats", "@知识库助手 3")) is True
    reply = api.replies[-1][1]
    assert reply.startswith("🎮 小李・我的战绩")
    assert "累计：正常 1 次｜补卡 0 次｜未交 0 次" in reply
    assert "最近一次：✅ 正常提交｜❌ 未复盘" in reply


def test_bot_uses_constrained_semantic_query_for_natural_wording(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSemanticQuerySummarizer(), store)
    service.handle_message(
        incoming(
            "om_assignment_one",
            "#8月17日 第1次作业已完成\n作业说明：已完成部署",
            created_at=datetime(2026, 8, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert (
        service.handle_message(
            incoming(
                "om_semantic_query",
                "@知识库助手 第一次还有哪些人掉队了",
                created_at=datetime(2026, 8, 18, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        )
        is True
    )

    reply = api.replies[-1][1]
    assert "第1次作业已提交 1/2" in reply
    assert "仍未交（1人）：小王" in reply


def test_homework_feedback_is_opt_in_and_idempotent(tmp_path):
    settings = make_settings(tmp_path)
    api = FakeApi()
    summarizer = FakeFeedbackSummarizer()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)
    message = incoming(
        "om_feedback",
        "@知识库助手 #0811前置作业已完成\n"
        "作业说明：我完成了公网部署，也记录了别人无法访问时的排查过程。\n"
        "#求反馈",
    )

    assert service.handle_message(message) is True
    assert service.handle_message(message) is False

    assert len(summarizer.feedback_calls) == 1
    assert summarizer.feedback_calls[0][0] == "小李"
    assert "#求反馈" not in summarizer.feedback_calls[0][1]
    assert len(api.replies) == 1
    assert api.replies[0][1].startswith("亮点：")
    assert api.replies[0][2] == "homework-feedback-om_feedback"


def test_short_homework_feedback_request_does_not_call_model(tmp_path):
    settings = make_settings(tmp_path)
    api = FakeApi()
    summarizer = FakeFeedbackSummarizer()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)

    service.handle_message(incoming("om_short_feedback", "#0811作业已完成 #求反馈"))

    assert summarizer.feedback_calls == []
    assert "补充作业说明、收获或卡点" in api.replies[-1][1]


def test_weekly_growth_card_is_generated_only_on_demand(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_growth_done",
            "#8月17日 第1次作业已完成\n作业说明：已完成页面",
            created_at=datetime(2026, 8, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    assert api.replies == []

    service.handle_message(
        incoming(
            "om_growth_query",
            "@知识库助手 5",
            created_at=datetime(2026, 8, 18, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    reply = api.replies[-1][1]
    assert reply.startswith("🌟 本周成长卡")
    assert "作业完成：1/2" in reply
    assert "🎯 全部准时：小李" in reply


def test_multi_cycle_query_patterns_exclude_the_current_unfinished_cycle(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
    )
    service = GroupSummaryService(
        settings,
        FakeApi(),
        FakeSummarizer(),
        LocalStore(settings.db_path),
    )
    reference_day = datetime(2026, 8, 24).date()

    assert service._multi_cycle_assignment_numbers("前三次作业", reference_day) == [
        1,
        2,
        3,
    ]
    assert service._multi_cycle_assignment_numbers("这三次是否属实", reference_day) == [
        1,
        2,
        3,
    ]
    assert service._multi_cycle_assignment_numbers("第1到3次作业", reference_day) == [
        1,
        2,
        3,
    ]
    assert service._multi_cycle_assignment_numbers("技术周整体情况", reference_day) == [
        1,
        2,
        3,
    ]
    assert service._multi_cycle_assignment_numbers("第三次作业", reference_day) is None


def test_multi_cycle_query_reads_base_overrides_without_modifying_work_data(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
        report_members=("小李", "小王"),
        member_aliases={"ou_1": "小李", "ou_2": "小王"},
    )
    api = FakeApi()
    api.base_records = [
        {
            "record_id": "rec_manual_late",
            "fields": {
                "记录键": "2026-08-21|ou_1",
                "组员姓名": "小李",
                "作业状态": "未提交",
                "人工状态": "补卡",
            },
        }
    ]
    store = LocalStore(settings.db_path)

    def seed_cycle(report_date, first_status, second_status):
        store.replace_daily_attendance(
            [
                AttendanceRecord(
                    report_date=report_date,
                    member_key="ou_1",
                    sender_open_id="ou_1",
                    sender_name="小李",
                    assignment_label="作业",
                    homework_status=first_status,
                    review_status="missing",
                    homework_source="tag",
                ),
                AttendanceRecord(
                    report_date=report_date,
                    member_key="ou_2",
                    sender_open_id="ou_2",
                    sender_name="小王",
                    assignment_label="作业",
                    homework_status=second_status,
                    review_status="missing",
                    homework_source="tag",
                ),
            ]
        )

    seed_cycle("2026-08-17", "completed", "completed")
    seed_cycle("2026-08-19", "completed", "late")
    seed_cycle("2026-08-21", "missing", "missing")
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)

    service.handle_message(
        incoming(
            "om_multi_cycle_review",
            "@知识库助手 你能复查一下这三次作业的情况吗，是否属实",
            created_at=datetime(2026, 8, 24, 14, 3, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    reply = api.replies[-1][1]
    assert reply.startswith("📊 第1—3次作业复查（只读）")
    assert "第1次（8月17日—8月18日）：正常 2｜补卡 0｜最终 2/2｜未交 0" in reply
    assert "第2次（8月19日—8月20日）：正常 1｜补卡 1｜最终 2/2｜未交 0" in reply
    assert "第3次（8月21日—8月22日）：正常 0｜补卡 1｜最终 1/2｜未交 1" in reply
    assert "正常：小李、小王" in reply
    assert "累计作业人次：正常 3｜补卡 2｜最终完成 5/6｜未交 1" in reply
    assert "所选周期全部完成（1人）：小李" in reply
    assert "本次查询没有修改任何表格数据" in reply
    assert api.base_updates == []
    assert len(api.base_records) == 1
    stored = {
        record.sender_name: record.homework_status
        for record in store.list_daily_attendance("2026-08-21")
    }
    assert stored == {"小李": "missing", "小王": "missing"}
    assert api.replies[-1][2] == "multi-cycle-stats-om_multi_cycle_review"


def test_direct_mention_can_start_natural_course_chat(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(original, social_chat_enabled=True)
    api = FakeApi()
    summarizer = FakeSocialSummarizer(
        {
            "action": "reply",
            "reply": "先确认你发的是部署后的公网链接，不是本地预览地址。",
            "emoji": None,
            "confidence": 0.97,
        }
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)

    assert (
        service.handle_message(
            incoming(
                "om_social_direct",
                "@知识库助手 我这个 Cloudflare 页面部署后别人打不开，怎么排查？",
            )
        )
        is True
    )

    assert summarizer.social_calls[0][3] is True
    assert api.replies[-1][1].startswith("先确认你发的是部署后")
    assert api.replies[-1][2] == "social-chat-om_social_direct"
    assert store.social_chat_action_sent("om_social_direct") is True


def test_direct_mention_can_answer_ordinary_question_outside_course_scope(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(original, social_chat_enabled=True)
    api = FakeApi()
    summarizer = FakeSocialSummarizer(
        {
            "action": "reply",
            "reply": "想轻松一点可以看《银河系漫游指南》，它的幽默感很强。",
            "emoji": None,
            "confidence": 0.60,
        }
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)

    service.handle_message(incoming("om_social_book", "@知识库助手 推荐一本适合周末看的科幻小说？"))

    assert summarizer.social_calls[0][3] is True
    assert api.replies[-1][2] == "social-chat-om_social_book"
    assert "银河系漫游指南" in api.replies[-1][1]


def test_direct_mention_uses_natural_fallback_when_social_model_stays_silent(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(original, social_chat_enabled=True)
    api = FakeApi()
    summarizer = FakeSocialSummarizer(
        {"action": "silent", "reply": "", "emoji": None, "confidence": 0.99}
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)

    service.handle_message(incoming("om_social_silent", "@知识库助手 你觉得这个怎么样？"))

    assert "作业统计、群规则、项目卡点或普通问题" in api.replies[-1][1]
    assert "只支持" not in api.replies[-1][1]


def test_bot_explains_its_reminder_schedule_without_calling_social_model(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        social_chat_enabled=True,
        reminder_enabled=True,
        reminder_hour=12,
        missing_list_enabled=True,
        missing_list_hour=17,
        final_status_enabled=True,
        final_status_hour=20,
        makeup_reminder_enabled=True,
        makeup_reminder_hour=17,
        makeup_summary_enabled=True,
        makeup_summary_hour=20,
    )
    api = FakeApi()
    summarizer = FakeSocialSummarizer()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)

    service.handle_message(incoming("om_reminder_what", "@知识库助手 你这个催交提醒是啥"))

    reply = api.replies[-1][1]
    assert "截止日：12:00 @ 尚未提交的成员" in reply
    assert "17:00 发未交名单" in reply
    assert "20:00 发完成/未完成汇总" in reply
    assert "补交日：17:00 @ 仍未补交的成员" in reply
    assert summarizer.social_calls == []


def test_proactive_social_chat_only_considers_useful_signals(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        social_chat_enabled=True,
        social_chat_proactive_enabled=True,
    )
    api = FakeApi()
    summarizer = FakeSocialSummarizer(
        {
            "action": "reply",
            "reply": "可以先用手机流量打开公网链接，把本地缓存和内网环境排除掉。",
            "emoji": None,
            "confidence": 0.80,
        }
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)
    daytime = datetime(2026, 8, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    service.handle_message(incoming("om_social_plain", "晚上吃什么好", created_at=daytime))
    assert summarizer.social_calls == []

    service.handle_message(
        incoming(
            "om_social_help",
            "我的页面自己能打开，别人打不开，有没有办法排查？",
            created_at=daytime,
        )
    )

    assert len(summarizer.social_calls) == 1
    assert summarizer.social_calls[0][3] is False
    assert api.replies[-1][2] == "social-chat-om_social_help"


def test_proactive_social_chat_can_join_course_work_discussion(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        social_chat_enabled=True,
        social_chat_proactive_enabled=True,
    )
    api = FakeApi()
    summarizer = FakeSocialSummarizer(
        {
            "action": "reply",
            "reply": "如果你是觉得首屏太空，可以先判断是信息层级不足，还是纯粹的间距问题。你现在更别扭的是标题区还是主视觉？",
            "emoji": None,
            "confidence": 0.70,
        }
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)

    service.handle_message(
        incoming(
            "om_social_work_discussion",
            "这个页面的留白有点多，但我还在想怎么调整",
            created_at=datetime(2026, 8, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert len(summarizer.social_calls) == 1
    assert summarizer.social_calls[0][3] is False
    assert api.replies[-1][2] == "social-chat-om_social_work_discussion"
    assert "信息层级" in api.replies[-1][1]


def test_proactive_social_chat_uses_reaction_for_small_win(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        social_chat_enabled=True,
        social_chat_proactive_enabled=True,
    )
    api = FakeApi()
    summarizer = FakeSocialSummarizer(
        {
            "action": "react",
            "reply": "",
            "emoji": "PARTY",
            "confidence": 0.94,
        }
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)

    service.handle_message(
        incoming(
            "om_social_win",
            "终于把这个页面跑通了！",
            created_at=datetime(2026, 8, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert api.reactions == [("om_social_win", "PARTY")]
    assert api.replies == []
    assert store.social_chat_action_sent("om_social_win") is True


def test_proactive_social_chat_respects_cooldown_and_skips_homework(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        social_chat_enabled=True,
        social_chat_proactive_enabled=True,
        social_chat_cooldown_minutes=10,
    )
    api = FakeApi()
    summarizer = FakeSocialSummarizer(
        {
            "action": "reply",
            "reply": "先看第一个报错行，把它前后两行一起发出来。",
            "emoji": None,
            "confidence": 0.96,
        }
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)
    first_at = datetime(2026, 8, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    service.handle_message(
        incoming("om_social_first", "这里一直报错，怎么办？", created_at=first_at)
    )
    service.handle_message(
        incoming(
            "om_social_cooldown",
            "另一个页面也卡住了，怎么办？",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 11, 10, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    service.handle_message(
        incoming(
            "om_social_homework",
            "#0811作业已完成 终于搞定了",
            created_at=datetime(2026, 8, 11, 10, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert len(summarizer.social_calls) == 1
    assert len(api.replies) == 1


def test_proactive_social_chat_stays_quiet_at_night(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        social_chat_enabled=True,
        social_chat_proactive_enabled=True,
    )
    api = FakeApi()
    summarizer = FakeSocialSummarizer(
        {
            "action": "reply",
            "reply": "这条不应在静默时段发出。",
            "emoji": None,
            "confidence": 0.99,
        }
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)

    service.handle_message(
        incoming(
            "om_social_night",
            "我这里一直报错，怎么办？",
            created_at=datetime(2026, 8, 11, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert summarizer.social_calls == []
    assert api.replies == []


def test_replying_to_bot_continues_conversation_with_previous_reply_context(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(original, social_chat_enabled=True)
    api = FakeApi()
    summarizer = FakeSocialSummarizer(
        {
            "action": "reply",
            "reply": "先确认部署链接和本地预览链接不是同一个。",
            "emoji": None,
            "confidence": 0.97,
        },
        {
            "action": "reply",
            "reply": "然后用手机流量访问一次，这能快速排除局域网和缓存影响。",
            "emoji": None,
            "confidence": 0.96,
        },
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)

    service.handle_message(incoming("om_social_turn_one", "@知识库助手 部署后别人打不开，怎么办？"))
    service.handle_message(
        incoming(
            "om_social_turn_two",
            "那下一步我先查什么？",
            parent_id="om_reply",
            created_at=datetime(2026, 8, 11, 18, 31, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert len(api.replies) == 2
    assert summarizer.social_calls[1][3] is True
    assert any("助教：先确认部署链接" in line for line in summarizer.social_calls[1][2])


def test_group_leader_can_mark_another_member_completed(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
        report_members=("组长", "Arina", "普通成员"),
        member_aliases={"ou_leader": "组长", "ou_arina": "Arina", "ou_member": "普通成员"},
        leader_member_ids=("ou_leader",),
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_third_assignment",
            "#8月21日 第3次作业已完成\n作业说明：已完成部署",
            sender_open_id="ou_member",
            created_at=datetime(2026, 8, 22, 13, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert (
        service.handle_message(
            incoming(
                "om_leader_override",
                "@知识库助手 Arina已完成",
                sender_open_id="ou_leader",
                created_at=datetime(2026, 8, 22, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        )
        is True
    )

    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-21")
    }
    assert attendance["Arina"].homework_status == "completed"
    arina_record = next(
        record for record in api.base_records if record["fields"]["组员姓名"] == "Arina"
    )
    assert arina_record["fields"]["人工状态"] == "正常提交"
    assert api.replies[-1][1] == (
        "已由组长组长把Arina的第3次作业（8月21日—8月22日）标记为正常提交。\n"
        "数据库和多维表格已同步。"
    )
    assert store.list_attendance_overrides("2026-08-21") == [
        {
            "message_id": "om_leader_override",
            "report_date": "2026-08-21",
            "member_key": "ou_arina",
            "member_name": "Arina",
            "status": "completed",
            "actor_open_id": "ou_leader",
            "actor_name": "组长",
            "event_time_ms": int(
                datetime(2026, 8, 22, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
            ),
        }
    ]


def test_group_leader_can_mark_multiple_members_late_including_comma_name(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
        report_members=("组长", "卫安", "米粒", "，"),
        member_aliases={
            "ou_leader": "组长",
            "ou_weian": "卫安",
            "ou_mili": "米粒",
            "ou_comma": "，",
        },
        leader_member_ids=("ou_leader",),
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_third_cycle_seed",
            "第3次作业发布",
            sender_open_id="ou_leader",
            created_at=datetime(2026, 8, 21, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.handle_message(
        incoming(
            "om_multi_override",
            "@知识库助手 卫安、米粒、，已补交",
            sender_open_id="ou_leader",
            created_at=datetime(2026, 8, 23, 20, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-21")
    }
    assert attendance["卫安"].homework_status == "late"
    assert attendance["米粒"].homework_status == "late"
    assert attendance["，"].homework_status == "late"
    assert service._leader_override_request("卫安、米粒、，已补交") == (
        ("卫安", "米粒", "，"),
        "late",
    )
    manual_updates = [fields for _, fields in api.base_updates if set(fields) == {"人工状态"}]
    assert len(manual_updates) == 3
    assert {fields["人工状态"] for fields in manual_updates} == {"补卡"}
    assert "把卫安、米粒、，的第3次作业" in api.replies[-1][1]
    assert store.list_homework_verifications("2026-08-21") == []
    audit = store.list_attendance_overrides("2026-08-21")
    assert len(audit) == 1
    assert audit[0]["member_name"] == "卫安、米粒、，"
    assert audit[0]["status"] == "late"


def test_group_leader_natural_language_override_uses_constrained_minimax(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
        report_members=("组长", "卫安", "米粒"),
        member_aliases={
            "ou_leader": "组长",
            "ou_weian": "卫安",
            "ou_mili": "米粒",
        },
        leader_member_ids=("ou_leader",),
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSemanticSummarizer(), store)
    service.handle_message(
        incoming(
            "om_semantic_cycle_seed",
            "第3次作业发布",
            sender_open_id="ou_leader",
            created_at=datetime(2026, 8, 21, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.handle_message(
        incoming(
            "om_semantic_override",
            "@知识库助手 第三次那份，卫安和米粒都算补了吧",
            sender_open_id="ou_leader",
            created_at=datetime(2026, 8, 23, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-21")
    }
    assert attendance["卫安"].homework_status == "late"
    assert attendance["米粒"].homework_status == "late"
    assert "把卫安、米粒的第3次作业" in api.replies[-1][1]


def test_group_leader_can_mark_historical_assignment_late(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
        report_members=("组长", "Arina"),
        member_aliases={"ou_leader": "组长", "ou_arina": "Arina"},
        leader_member_ids=("ou_leader",),
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_second_cycle_seed",
            "第2次作业发布",
            sender_open_id="ou_leader",
            created_at=datetime(2026, 8, 19, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.handle_message(
        incoming(
            "om_historical_late",
            "@知识库助手 Arina第2次作业已补交",
            sender_open_id="ou_leader",
            created_at=datetime(2026, 8, 22, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-19")
    }
    assert attendance["Arina"].homework_status == "late"
    assert "第2次作业（8月19日—8月20日）标记为补卡" in api.replies[-1][1]


def test_non_leader_cannot_override_another_member(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
        report_members=("组长", "Arina", "普通成员"),
        member_aliases={"ou_leader": "组长", "ou_arina": "Arina", "ou_member": "普通成员"},
        leader_member_ids=("ou_leader",),
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)

    service.handle_message(
        incoming(
            "om_denied_override",
            "@知识库助手 Arina已完成",
            sender_open_id="ou_member",
        )
    )

    assert api.replies[-1][1] == "只有本群已配置的组长可以代替其他成员修改作业状态。"
    assert api.base_updates == []
    assert store.list_attendance_overrides("2026-08-11") == []


def test_status_question_is_not_parsed_as_leader_override(tmp_path):
    _, _, _, _, service = make_service(tmp_path)

    assert service._leader_override_request("Arina完成了吗？") is None


def test_makeup_submission_spelling_is_recognized_as_late(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        member_aliases={**original.member_aliases, "ou_2": "米粒"},
        report_members=("小李", "米粒"),
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)

    service.handle_message(
        incoming(
            "om_makeup_submission_spelling",
            "#0822 第3次作业补提交\n技术作业\n成果链接：https://example.com/work",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 23, 19, 52, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-21")
    }
    assert attendance["米粒"].homework_status == "late"
    assert attendance["米粒"].homework_message_ids == ("om_makeup_submission_spelling",)


def test_bot_mention_resolves_chinese_assignment_number_instead_of_current_cycle(
    tmp_path,
):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_second_assignment",
            "#0819第2次作业已完成",
            created_at=datetime(2026, 8, 19, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.handle_message(
        incoming(
            "om_second_assignment_question",
            "@知识库助手 重新发第二次作业的情况",
            created_at=datetime(2026, 8, 21, 20, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    reply = api.replies[-1][1]
    assert reply.startswith("第2次作业（8月19日—8月20日）")
    assert "第3次作业" not in reply
    assert service._query_report_date("第2次作业", datetime(2026, 8, 21).date()) == ("2026-08-19")


def test_homework_query_counts_submission_between_five_and_deadline_as_normal(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        missing_list_hour=17,
        member_aliases={**original.member_aliases, "ou_2": "成员乙"},
        report_members=("小李", "成员乙"),
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_ironworker",
            "#8月18日 第1次作业已补卡\nhttps://example.com/work",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 18, 19, 34, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.handle_message(
        incoming(
            "om_question",
            "@知识库助手 现在还有谁没交作业",
            created_at=datetime(2026, 8, 18, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert api.replies[-1][1] == (
        "第1次作业（8月17日—8月18日）第1次作业已提交 1/2"
        "（正常提交 1，已补交 0）。\n"
        "已补交（0人）：无\n"
        "仍未交（1人）：小李"
    )
    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-17")
    }
    assert attendance["成员乙"].homework_status == "completed"


def test_homework_query_counts_post_deadline_submission_as_late(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        missing_list_hour=17,
        member_aliases={**original.member_aliases, "ou_2": "成员乙"},
        report_members=("小李", "成员乙"),
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_normal",
            "#0817第1次作业已完成",
            created_at=datetime(2026, 8, 17, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    service.handle_message(
        incoming(
            "om_ironworker_late",
            "#0817第1次作业已补交\n作业说明：已完成网页部署",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 18, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.handle_message(
        incoming(
            "om_question",
            "@知识库助手 现在还有谁没交0817的作业",
            created_at=datetime(2026, 8, 18, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert "已提交 2/2（正常提交 1，已补交 1）" in api.replies[-1][1]
    assert "已补交（1人）：成员乙" in api.replies[-1][1]
    assert "仍未交（0人）：无" in api.replies[-1][1]
    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-17")
    }
    assert attendance["成员乙"].homework_status == "late"


def test_bot_mention_can_query_one_members_full_history(tmp_path):
    member_key = "ou_member_a"
    original = make_settings(tmp_path)
    settings = replace(
        original,
        report_members=("成员甲", "小王"),
        member_aliases={**original.member_aliases, member_key: "成员甲"},
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    store.replace_daily_attendance(
        [
            AttendanceRecord(
                report_date="2026-08-09",
                member_key=member_key,
                sender_open_id=member_key,
                sender_name="成员甲",
                assignment_label="作业",
                homework_status="missing",
                review_status="missing",
                homework_source="image",
            ),
            *[
                AttendanceRecord(
                    report_date="2026-08-09",
                    member_key=name,
                    sender_open_id="",
                    sender_name=name,
                    assignment_label="作业",
                    homework_status="missing",
                    review_status="missing",
                    homework_source="image",
                )
                for name in settings.report_members
                if name != "成员甲"
            ],
        ]
    )
    store.replace_daily_attendance(
        [
            AttendanceRecord(
                report_date="2026-08-10",
                member_key=member_key,
                sender_open_id=member_key,
                sender_name="成员甲",
                assignment_label="前置作业",
                homework_status="late",
                review_status="completed",
                homework_source="tag",
            ),
            *[
                AttendanceRecord(
                    report_date="2026-08-10",
                    member_key=name,
                    sender_open_id="",
                    sender_name=name,
                    assignment_label="前置作业",
                    homework_status="missing",
                    review_status="missing",
                    homework_source="tag",
                )
                for name in settings.report_members
                if name != "成员甲"
            ],
        ]
    )

    assert (
        service.handle_message(incoming("om_history", "@知识库助手 查询成员甲全部打卡记录")) is True
    )

    reply = api.replies[-1][1]
    assert reply.startswith("成员甲・全部打卡记录")
    assert "正常 0 次｜补卡 1 次｜未打卡 1 次｜复盘 1/2" in reply
    assert "8月9日（作业）：❌ 未打卡｜❌ 未复盘" in reply
    assert "8月10日（前置作业）：🟡 补卡｜✅ 已复盘" in reply


def test_bot_mention_of_unrelated_question_explains_scope(tmp_path):
    _, api, _, _, service = make_service(tmp_path)

    service.handle_message(incoming("om_question", "@知识库助手 今天天气怎么样"))

    assert api.replies[-1][1] == "我目前只支持查询作业、复盘、未交名单和日报。"


def test_mentioning_another_member_does_not_trigger_bot_reply(tmp_path):
    _, api, _, _, service = make_service(tmp_path)

    service.handle_message(incoming("om_question", "@小王 现在还有谁没交作业"))

    assert api.replies == []


def test_summary_uses_preferred_completion_layout(tmp_path):
    _, _, _, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_1", "开始"))
    later = incoming("om_2", "完成")
    later_at = datetime(2026, 8, 11, 19, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    later = IncomingMessage(
        **{**later.__dict__, "create_time_ms": int(later_at.timestamp() * 1000)}
    )
    service.handle_message(later)

    result = service.build_summary("2026-08-11", "oc_group")
    assert result is not None
    assert "📊 今日总览" in result.text
    assert "✅ 完成情况" in result.text
    assert "⚠️ 未完成人员" in result.text
    assert "完成图片作业：0/2" in result.text
    assert "完成复盘作业：0/2" in result.text
    assert "两项均未完成（2 人）：\n小李、小王" in result.text


def test_scheduled_summary_is_idempotent(tmp_path):
    _, api, _, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_1", "完成了联调"))
    first = service.send_summary("2026-08-11", "oc_group")
    second = service.send_summary("2026-08-11", "oc_group")
    assert first is not None
    assert second is None
    assert len(api.sent) == 1


def test_scheduled_job_sends_short_brief_with_table_link_without_using_llm(tmp_path):
    settings, api, summarizer, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_1", "", message_type="image"))

    results = service.send_due_summaries("2026-08-11")

    assert len(results) == 1
    assert len(api.sent) == 1
    assert summarizer.calls == []
    _, text, uuid = api.sent[0]
    assert text.startswith(settings.report_title)
    assert "📊 今日打卡" in text
    assert "作业完成：1/2（正常 1，补卡 0，未交 1）" in text
    assert "[点击查看](https://example.com/base)" in text
    assert uuid.startswith("daily-brief-2026-08-11")


def test_evening_schedule_reports_the_current_day(tmp_path):
    _, _, _, _, service = make_service(tmp_path)
    evening = datetime(2026, 8, 11, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert service._scheduled_report_date(evening) == "2026-08-11"


def test_capture_only_mode_never_replies_or_sends(tmp_path):
    _, api, _, store, service = make_service(tmp_path, send_enabled=False)
    assert service.handle_message(incoming("om_1", "普通消息")) is True
    assert service.handle_message(incoming("om_2", "打开日报")) is False
    assert service.send_due_summaries("2026-08-11") == []
    assert api.replies == []
    assert api.sent == []
    assert len(store.list_messages("oc_group", 0, 9_999_999_999_999, 100)) == 1


def test_messages_from_different_groups_are_isolated(tmp_path):
    _, _, summarizer, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_1", "A群内容", chat_id="oc_a"))
    service.handle_message(incoming("om_2", "B群内容", chat_id="oc_b"))
    service.build_summary("2026-08-11", "oc_a")
    transcript = "\n".join(summarizer.calls[-1][1])
    assert "A群内容" in transcript
    assert "B群内容" not in transcript


def test_excluded_member_message_not_stored(tmp_path):
    _, _, _, store, service = make_service(tmp_path)
    excluded_id = "ou_excluded"
    assert (
        service.handle_message(incoming("om_excluded", "被排除的消息", sender_open_id=excluded_id))
        is False
    )
    stored = store.list_messages("oc_group", 0, 9_999_999_999_999, 100)
    assert len(stored) == 0


def test_alias_used_when_storing_message(tmp_path):
    _, _, _, store, service = make_service(tmp_path)
    alias_id = "ou_1"
    assert service.handle_message(incoming("om_alias", "别名消息", sender_open_id=alias_id)) is True
    stored = store.list_messages("oc_group", 0, 9_999_999_999_999, 100)
    assert len(stored) == 1
    assert stored[0].sender_name == "小李"


def test_build_summary_filters_excluded_and_applies_alias_to_historical(tmp_path):
    settings = make_settings(tmp_path)
    api = FakeApi()
    summarizer = FakeSummarizer()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, summarizer, store)

    # Insert a message from an excluded member directly into store (simulating historical data)
    excluded_id = "ou_excluded"
    excluded_msg = StoredMessage(
        message_id="om_hist_excluded",
        chat_id="oc_group",
        sender_open_id=excluded_id,
        sender_name="旧昵称",
        message_type="text",
        content="历史排除消息",
        create_time_ms=int(
            datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
        ),
    )
    store.add_message(excluded_msg)

    # Insert a message from an aliased member with old name
    alias_id = "ou_1"
    alias_msg = StoredMessage(
        message_id="om_hist_alias",
        chat_id="oc_group",
        sender_open_id=alias_id,
        sender_name="旧昵称",
        message_type="text",
        content="历史别名消息",
        create_time_ms=int(
            datetime(2026, 8, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
        ),
    )
    store.add_message(alias_msg)

    result = service.build_summary("2026-08-11", "oc_group")
    assert result is not None
    assert result.message_count == 1
    assert result.participant_count == 1
    assert len(summarizer.calls) == 1
    transcript_lines = summarizer.calls[0][1]
    transcript = "\n".join(transcript_lines)
    assert "历史排除消息" not in transcript
    assert "历史别名消息" in transcript
    assert "小李" in transcript
    assert "旧昵称" not in transcript


def test_image_and_current_day_review_completion_are_computed_in_code(tmp_path):
    _, _, summarizer, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_image", "", message_type="image"))
    service.handle_message(incoming("om_review", "#复盘 0811 今天完成了练习"))
    service.handle_message(
        incoming("om_old_review", "#复盘 0810 昨天的内容", sender_open_id="ou_2")
    )

    result = service.build_summary("2026-08-11", "oc_group")

    assert result is not None
    assert "完成图片作业：1/2" in result.text
    assert "完成复盘作业：1/2" in result.text
    assert "两项均完成（1 人）：\n小李" in result.text
    assert "仅完成复盘（0 人）：\n无" in result.text
    assert "缺复盘（1 人）：\n小王" in result.text
    assert "当日有效复盘人员及条数：小李（1 条）" in summarizer.calls[-1][2]


def test_topic_files_are_stored_and_counted_as_homework(tmp_path):
    _, _, _, store, service = make_service(tmp_path)
    service.handle_message(
        incoming("om_root", "云上花圃-离线版.html", message_type="file", thread_id="omt_1")
    )
    service.handle_message(
        incoming(
            "om_reply",
            "会赢吗.html",
            sender_open_id="ou_2",
            message_type="file",
            thread_id="omt_1",
        )
    )

    result = service.build_summary("2026-08-11", "oc_group")

    assert result is not None
    assert "完成话题作业：2/2" in result.text
    stored = store.list_messages("oc_group", 0, 9_999_999_999_999, 100)
    assert [message.content for message in stored] == [
        "[文件] 云上花圃-离线版.html",
        "[文件] 会赢吗.html",
    ]
    assert {message.thread_id for message in stored} == {"omt_1"}
    attendance = {r.sender_name: r for r in store.list_daily_attendance("2026-08-11")}
    assert attendance["小李"].homework_source == "thread"
    assert attendance["小李"].homework_message_ids == ("om_root",)
    assert attendance["小王"].homework_message_ids == ("om_reply",)


def test_topic_links_and_completed_replies_count_for_the_root_assignment(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_deadline_overrides={"2026-08-11": "2026-08-12 14:00"},
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, FakeApi(), FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_topic",
            "技术周第1次作业完成打卡",
            sender_open_id="ou_2",
            thread_id="omt_1",
            created_at=datetime(2026, 8, 11, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    service.handle_message(
        incoming(
            "om_link",
            "https://example.com/work",
            thread_id="omt_1",
            parent_id="om_topic",
            root_id="om_topic",
            created_at=datetime(2026, 8, 11, 18, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    service.handle_message(
        incoming(
            "om_late",
            "#8月12日 第1次作业已补卡 https://example.com/late",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 12, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.sync_attendance_date("2026-08-11", "oc_group")

    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-11")
    }
    assert attendance["小李"].assignment_label == "第1次作业"
    assert attendance["小李"].homework_status == "completed"
    assert attendance["小王"].homework_status == "late"
    assert attendance["小王"].homework_message_ids == ("om_late",)


def test_topic_replies_do_not_become_the_next_days_assignment(tmp_path):
    _, _, _, _, service = make_service(tmp_path)
    service.handle_message(
        incoming(
            "om_topic",
            "第1次作业完成打卡",
            sender_open_id="ou_2",
            thread_id="omt_1",
            created_at=datetime(2026, 8, 11, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    service.handle_message(
        incoming(
            "om_late_link",
            "https://example.com/late",
            thread_id="omt_1",
            parent_id="om_topic",
            root_id="om_topic",
            created_at=datetime(2026, 8, 11, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    messages = service._assignment_window_messages("2026-08-12", "oc_group", 20, 0)

    assert messages == []


def test_file_outside_a_topic_is_not_automatically_homework(tmp_path):
    _, _, _, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_file", "群资料.html", message_type="file"))

    result = service.build_summary("2026-08-11", "oc_group")

    assert result is not None
    assert "完成图片作业：0/2" in result.text


def test_dated_completion_tag_switches_report_to_named_assignment(tmp_path):
    _, _, _, store, service = make_service(tmp_path)
    service.handle_message(
        incoming(
            "om_reminder",
            "@_all 完成前置作业的成员按以下格式打卡 #0811前置作业已完成",
            sender_open_id="ou_2",
        )
    )
    service.handle_message(incoming("om_done", "#0811前置作业已完成"))

    result = service.build_summary("2026-08-11", "oc_group")

    assert result is not None
    assert "完成前置作业：1/2" in result.text
    assert "仅完成前置作业（1 人）：\n小李" in result.text
    assert "缺前置作业（1 人）：\n小王" in result.text

    attendance = store.list_daily_attendance("2026-08-11")
    assert len(attendance) == 2
    by_name = {record.sender_name: record for record in attendance}
    assert by_name["小李"].homework_status == "completed"
    assert by_name["小李"].homework_source == "tag"
    assert by_name["小李"].homework_message_ids == ("om_done",)
    assert by_name["小王"].homework_status == "missing"


def test_link_before_completion_tag_is_a_submission_not_an_instruction(tmp_path):
    _, _, _, store, service = make_service(tmp_path)
    service.handle_message(
        incoming(
            "om_done",
            "https://example.com/work #0811第一次作业已完成",
        )
    )

    service.sync_attendance_date("2026-08-11", "oc_group")

    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-11")
    }
    assert attendance["小李"].homework_status == "completed"
    assert attendance["小李"].homework_message_ids == ("om_done",)


def test_review_date_before_keyword_is_assigned_to_the_right_day(tmp_path):
    _, _, _, _, service = make_service(tmp_path)
    service.handle_message(incoming("om_today", "#0811复盘 今天的复盘"))
    service.handle_message(incoming("om_old", "#0810 复盘打卡 昨天的复盘", sender_open_id="ou_2"))

    result = service.build_summary("2026-08-11", "oc_group")

    assert result is not None
    assert "完成复盘作业：1/2" in result.text
    assert "仅完成复盘（1 人）：\n小李" in result.text


def test_explicit_late_image_does_not_count_as_current_day_homework(tmp_path):
    settings, api, summarizer, store, service = make_service(tmp_path)
    created = int(datetime(2026, 8, 11, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    store.add_message(
        StoredMessage(
            message_id="om_late",
            chat_id="oc_group",
            sender_open_id="ou_1",
            sender_name="小李",
            message_type="post",
            content="补打卡 [图片]",
            create_time_ms=created,
        )
    )
    store.add_message(
        StoredMessage(
            message_id="om_normal",
            chat_id="oc_group",
            sender_open_id="ou_2",
            sender_name="小王",
            message_type="image",
            content="[图片]",
            create_time_ms=created - 60_000,
        )
    )

    result = service.build_summary("2026-08-11", "oc_group")

    assert result is not None
    assert "完成图片作业：1/2" in result.text
    assert "仅完成图片（1 人）：\n小王" in result.text


def test_cross_day_first_completion_is_recorded_as_late(tmp_path):
    settings, api, summarizer, store, service = make_service(tmp_path)
    store.add_message(
        StoredMessage(
            message_id="om_start",
            chat_id="oc_group",
            sender_open_id="ou_2",
            sender_name="小王",
            message_type="text",
            content="开始今天的任务",
            create_time_ms=int(
                datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
            ),
        )
    )
    store.add_message(
        StoredMessage(
            message_id="om_late_done",
            chat_id="oc_group",
            sender_open_id="ou_1",
            sender_name="小李",
            message_type="text",
            content="#0811前置作业已完成\n作业说明：已完成网页部署",
            create_time_ms=int(
                datetime(2026, 8, 12, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
            ),
        )
    )

    result = service.build_summary("2026-08-11", "oc_group")

    assert result is not None
    attendance = {r.sender_name: r for r in store.list_daily_attendance("2026-08-11")}
    assert attendance["小李"].homework_status == "late"
    assert attendance["小李"].homework_message_ids == ("om_late_done",)


def test_completion_after_default_20_deadline_is_late(tmp_path):
    settings, api, summarizer, store, service = make_service(tmp_path)
    service.handle_message(
        incoming(
            "om_start",
            "开始今天的任务",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    service.handle_message(
        incoming(
            "om_after_20",
            "#0811前置作业已完成\n作业说明：已完成网页部署",
            created_at=datetime(2026, 8, 11, 20, 6, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.build_summary("2026-08-11", "oc_group")

    attendance = {r.sender_name: r for r in store.list_daily_attendance("2026-08-11")}
    assert attendance["小李"].homework_status == "late"


def test_one_time_deadline_override_keeps_before_14_normal_and_after_14_late(
    tmp_path,
):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_deadline_overrides={"2026-08-11": "2026-08-12 14:00"},
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_before_14",
            "#0811前置作业已完成https://example.com/work",
            created_at=datetime(2026, 8, 12, 13, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    service.handle_message(
        incoming(
            "om_after_14",
            "#0811前置作业已完成\n作业说明：已完成网页部署",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 12, 14, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.sync_attendance_date("2026-08-11", "oc_group")

    attendance = {r.sender_name: r for r in store.list_daily_attendance("2026-08-11")}
    assert attendance["小李"].homework_status == "completed"
    assert attendance["小王"].homework_status == "late"


def test_new_dated_submission_immediately_refreshes_attendance(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_deadline_overrides={"2026-08-11": "2026-08-12 14:00"},
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, FakeApi(), FakeSummarizer(), store)

    service.handle_message(
        incoming(
            "om_after_14",
            "#0811前置作业已完成\n作业说明：已完成网页部署",
            created_at=datetime(2026, 8, 12, 14, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    attendance = {r.sender_name: r for r in store.list_daily_attendance("2026-08-11")}
    assert attendance["小李"].homework_status == "late"


def test_base_sync_is_idempotent_and_writes_cumulative_fields(tmp_path):
    settings = replace(
        make_settings(tmp_path),
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(incoming("om_image", "", message_type="image"))

    service.build_summary("2026-08-11", "oc_group")
    service.build_summary("2026-08-11", "oc_group")

    assert len(api.base_records) == 2
    by_name = {record["fields"]["组员姓名"]: record["fields"] for record in api.base_records}
    assert by_name["小李"]["作业状态"] == "已提交"
    assert by_name["小李"]["正常提交累计"] == 1
    assert by_name["小王"]["作业状态"] == "未提交"
    assert by_name["小王"]["旷卡累计"] == 1
    assert api.base_updates == []


def test_base_sync_writes_assignment_cycle_fields(tmp_path):
    settings = replace(
        make_settings(tmp_path),
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    store.replace_daily_attendance(
        [
            AttendanceRecord(
                report_date="2026-08-21",
                member_key="ou_1",
                sender_open_id="ou_1",
                sender_name="小李",
                assignment_label="第3次作业",
                homework_status="completed",
                review_status="missing",
                homework_source="tag",
            )
        ]
    )

    service.sync_stored_attendance_date("2026-08-21")

    fields = api.base_records[0]["fields"]
    assert fields["作业序号"] == 3
    assert fields["作业周期"] == "第3次作业（8月21日—8月22日）"
    assert fields["周期状态"] in {"当前周期", "历史周期"}


def test_cycle_status_refresh_demotes_rows_from_previous_assignment(tmp_path):
    settings = replace(
        make_settings(tmp_path),
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
    )
    api = FakeApi()
    api.base_records = [
        {
            "record_id": "rec_old",
            "fields": {
                "记录键": "2026-08-19|ou_1",
                "周期状态": "当前周期",
            },
        },
        {
            "record_id": "rec_current",
            "fields": {
                "记录键": "2026-08-21|ou_1",
                "周期状态": "当前周期",
            },
        },
    ]
    service = GroupSummaryService(settings, api, FakeSummarizer(), LocalStore(settings.db_path))

    assert service._refresh_base_cycle_statuses("2026-08-21") == 1
    assert api.base_updates == [("rec_old", {"周期状态": "历史周期"})]


def test_iteration_day_status_can_be_recorded_and_queried(tmp_path):
    _, api, _, _, service = make_service(tmp_path)

    assert (
        service.handle_message(incoming("om_iter_pending", "@知识库助手 @小王 #迭代 DAY5")) is True
    )
    service.handle_message(incoming("om_iter_query", "@知识库助手 #迭代 DAY5 状态"))
    assert "待迭代（1人）：小王" in api.replies[-1][1]

    assert (
        service.handle_message(
            incoming(
                "om_iter_done",
                "@知识库助手 #迭代 DAY5 已完成",
                sender_open_id="ou_2",
            )
        )
        is True
    )
    service.handle_message(incoming("om_iter_query_2", "@知识库助手 #迭代 DAY5 状态"))
    assert "待迭代（0人）：无" in api.replies[-1][1]
    assert "已迭代（1人）：小王" in api.replies[-1][1]


def test_reminder_mentions_missing_union_once_and_is_idempotent(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        reminder_enabled=True,
        member_aliases={**original.member_aliases, "ou_1": "小李", "ou_2": "小王"},
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_image",
            "",
            message_type="image",
            created_at=datetime(2026, 8, 11, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    first = service.send_reminder("2026-08-11", "oc_group")
    second = service.send_reminder("2026-08-11", "oc_group")

    assert first == "om_reminder"
    assert second == ""
    assert len(api.reminders) == 1
    _, mentions, homework_missing, review_missing, _ = api.reminders[0]
    assert mentions == [("ou_2", "小王")]
    assert homework_missing == ["小王"]
    assert review_missing == []


def test_two_day_assignment_cycle_uses_second_day_deadline(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
    )

    assert settings.assignment_cycle("2026-08-17") == (
        datetime(2026, 8, 17).date(),
        datetime(2026, 8, 18).date(),
        1,
    )
    assert settings.assignment_report_date("2026-08-18") == "2026-08-17"
    assert settings.assignment_cycle("2026-08-20")[2] == 2
    assert settings.is_assignment_due_day("2026-08-18") is True
    assert settings.is_assignment_due_day("2026-08-19") is False
    assert settings.is_makeup_day("2026-08-18") is False
    assert settings.is_makeup_day("2026-08-19") is True
    assert settings.makeup_report_date("2026-08-19") == "2026-08-17"
    assert settings.assignment_deadline("2026-08-17").isoformat() == ("2026-08-18T20:00:00+08:00")


def test_two_day_cycle_counts_both_dates_as_one_assignment(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        member_aliases={**original.member_aliases, "ou_1": "小李", "ou_2": "小王"},
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, FakeApi(), FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_day_1",
            "#0817第一次作业已完成",
            created_at=datetime(2026, 8, 17, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    service.handle_message(
        incoming(
            "om_day_2",
            "#8月18日 第一次作业已完成",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 18, 19, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.sync_attendance_date("2026-08-18", "oc_group")

    attendance = store.list_daily_attendance("2026-08-17")
    assert {record.homework_status for record in attendance} == {"completed"}
    assert store.list_daily_attendance("2026-08-18") == []


def test_two_day_cycle_accepts_assignment_label_without_ci(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        report_members=("Arina", "铁匠"),
        member_aliases={"ou_1": "Arina", "ou_2": "铁匠"},
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, FakeApi(), FakeSummarizer(), store)

    service.handle_message(
        incoming(
            "om_arina",
            "#8月22日 第3作业已完成\n技术作业\n成果链接：https://example.com/arina",
            created_at=datetime(2026, 8, 22, 14, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    service.handle_message(
        incoming(
            "om_blacksmith",
            "#8月22日 第3作业已完成\n技术作业\n成果链接：https://example.com/blacksmith",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 22, 13, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.sync_attendance_date("2026-08-21", "oc_group")

    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-21")
    }
    assert attendance["Arina"].assignment_label == "第3次作业"
    assert attendance["Arina"].homework_status == "completed"
    assert attendance["铁匠"].homework_status == "completed"


def test_cycle_notifications_only_run_on_second_day_and_final_is_idempotent(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        final_status_enabled=True,
        member_aliases={**original.member_aliases, "ou_1": "小李", "ou_2": "小王"},
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_done",
            "#0817第一次作业已完成",
            created_at=datetime(2026, 8, 17, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert service.send_due_final_statuses("2026-08-17") == []
    assert service.send_due_final_statuses("2026-08-18") == ["om_summary"]
    assert service.send_due_final_statuses("2026-08-18") == []
    assert "第1次作业（8月17日—8月18日）・打卡汇总" in api.sent[0][1]
    assert "未完成（1人）：\n小王" in api.sent[0][1]


def test_makeup_notifications_run_on_third_day_and_split_final_status(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-09",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        makeup_reminder_enabled=True,
        makeup_summary_enabled=True,
        report_members=("小李", "小王", "小周"),
        member_aliases={
            **original.member_aliases,
            "ou_1": "小李",
            "ou_2": "小王",
            "ou_3": "小周",
        },
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_normal",
            "#0809第一次作业已完成",
            created_at=datetime(2026, 8, 9, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    service.handle_message(
        incoming(
            "om_late",
            "#0810第一次作业已完成\n作业说明：已完成网页部署",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 10, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert service.send_due_makeup_reminders("2026-08-10") == []
    assert service.send_due_makeup_reminders("2026-08-11") == ["om_makeup_reminder"]
    assert service.send_due_makeup_reminders("2026-08-11") == []
    _, mentions, missing, _ = api.makeup_reminders[0]
    assert mentions == [("ou_3", "小周")]
    assert missing == ["小周"]

    assert service.send_due_makeup_summaries("2026-08-10") == []
    assert service.send_due_makeup_summaries("2026-08-11") == ["om_summary"]
    assert service.send_due_makeup_summaries("2026-08-11") == []
    _, text, _ = api.sent[-1]
    assert "正常提交：1/3" in text
    assert "已补交：1/3" in text
    assert "最终完成：2/3" in text
    assert "仍未交（1人）：\n小周" in text
    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-09")
    }
    assert attendance["小李"].homework_status == "completed"
    assert attendance["小王"].homework_status == "late"
    assert attendance["小周"].homework_status == "missing"


def test_makeup_day_tags_use_assignment_number_and_accept_real_group_formats(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        report_members=("正常成员", "成员乙", "成员丙", "成员丁"),
        member_aliases={
            **original.member_aliases,
            "ou_1": "正常成员",
            "ou_2": "成员乙",
            "ou_3": "成员丙",
            "ou_4": "成员丁",
        },
    )
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, FakeApi(), FakeSummarizer(), store)
    submissions = (
        (
            "om_normal",
            "#0819第2次作业已完成",
            "ou_1",
            datetime(2026, 8, 19, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
        (
            "om_pigeon",
            "#0821日 第2次作业已补卡\n作业说明：已完成补卡作业",
            "ou_2",
            datetime(2026, 8, 21, 11, 11, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
        (
            "om_tang",
            "#0821日 第2次作业补交打卡\n作业说明：已完成补卡作业",
            "ou_3",
            datetime(2026, 8, 21, 18, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
        (
            "om_zeng",
            "#0821 第2次作业已完成补卡\n作业说明：已完成补卡作业",
            "ou_4",
            datetime(2026, 8, 21, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    )
    for message_id, text, sender_open_id, created_at in submissions:
        service.handle_message(
            incoming(
                message_id,
                text,
                sender_open_id=sender_open_id,
                created_at=created_at,
            )
        )

    for text in (
        "#0821日 第2次作业已补卡",
        "#0821日 第2次作业补交打卡",
        "#0821 第2次作业已完成补卡",
    ):
        assert service._submission_report_dates(text, datetime(2026, 8, 21).date()) == [
            "2026-08-19"
        ]

    service.sync_attendance_date("2026-08-19", "oc_group")

    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-19")
    }
    assert attendance["正常成员"].homework_status == "completed"
    assert attendance["成员乙"].homework_status == "late"
    assert attendance["成员丙"].homework_status == "late"
    assert attendance["成员丁"].homework_status == "late"
    assert attendance["成员乙"].homework_message_ids == ("om_pigeon",)
    assert attendance["成员丙"].homework_message_ids == ("om_tang",)
    assert attendance["成员丁"].homework_message_ids == ("om_zeng",)


def test_self_makeup_claim_without_evidence_does_not_change_status(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)

    service.handle_message(
        incoming(
            "om_claim_only",
            "@知识库助手 补交第2次作业",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert store.list_homework_verifications("2026-08-19") == []
    assert "当前状态没有修改" in api.replies[-1][1]
    assert "作业链接、图片、文件或完整作业正文" in api.replies[-1][1]


def test_makeup_declaration_without_self_pronoun_updates_base_as_late(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_late_link",
            "第2次作业链接：https://example.com/work",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 21, 11, 55, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.handle_message(
        incoming(
            "om_late_claim",
            "@知识库助手 #8月21日 第2次作业已补交",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    verification = store.list_homework_verifications("2026-08-19")
    assert len(verification) == 1
    assert verification[0]["sender_open_id"] == "ou_2"
    assert verification[0]["status"] == "late"
    assert verification[0]["evidence_message_ids"] == ("om_late_link",)
    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-19")
    }
    assert attendance["小王"].homework_status == "late"
    assert attendance["小王"].homework_message_ids == ("om_late_link",)
    assert "判定结果：补卡" in api.replies[-1][1]
    assert "8月21日 11:55" in api.replies[-1][1]
    base_record = next(
        record for record in api.base_records if record["fields"]["组员姓名"] == "小王"
    )
    base_row = next(
        fields
        for record_id, fields in reversed(api.base_updates)
        if record_id == base_record["record_id"]
    )
    assert base_row["作业状态"] == "补卡"
    assert base_row["作业证据消息ID"] == "om_late_link"


def test_makeup_status_question_does_not_trigger_verification(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)

    service.handle_message(
        incoming(
            "om_makeup_question",
            "@知识库助手 第二次作业补交情况",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert store.list_homework_verifications("2026-08-19") == []
    assert api.replies[-1][2] == "stats-question-om_makeup_question"
    assert "仍未交" in api.replies[-1][1]


def test_self_makeup_wording_keeps_pre_deadline_evidence_normal(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_normal_body",
            "第2次作业\n课程：七夕网站\n作业说明：已完成网页部署",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 20, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.handle_message(
        incoming(
            "om_normal_claim",
            "@知识库助手 我补交第2次作业了",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-19")
    }
    assert attendance["小王"].homework_status == "completed"
    assert "判定结果：正常提交" in api.replies[-1][1]
    assert "按实际提交时间判定" in api.replies[-1][1]


def test_self_makeup_does_not_reuse_new_assignment_evidence_in_overlap_window(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
    )
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_third_work",
            "第3次作业链接：https://example.com/third",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 21, 11, 55, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.handle_message(
        incoming(
            "om_second_claim",
            "@知识库助手 我补交第2次作业了",
            sender_open_id="ou_2",
            created_at=datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    assert store.list_homework_verifications("2026-08-19") == []
    assert "当前状态没有修改" in api.replies[-1][1]


def test_self_makeup_verification_is_isolated_between_two_groups(tmp_path):
    chat_27 = "oc_27"
    chat_24 = "oc_24"
    settings = replace(
        make_settings(tmp_path),
        assignment_cycle_start_date="2026-08-17",
        assignment_cycle_days=2,
        assignment_publish_hour=10,
        assignment_due_hour=20,
        group_databases={
            chat_27: str(tmp_path / "group_27.sqlite3"),
            chat_24: str(tmp_path / "group_24.sqlite3"),
        },
    )
    router = GroupServiceRouter(settings, FakeApi(), FakeSummarizer())
    router.handle_message(
        incoming(
            "om_24_link",
            "第2次作业链接：https://example.com/group24",
            sender_open_id="ou_2",
            chat_id=chat_24,
            created_at=datetime(2026, 8, 21, 11, 55, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )
    router.handle_message(
        incoming(
            "om_24_claim",
            "@知识库助手 我补交第2次作业了",
            sender_open_id="ou_2",
            chat_id=chat_24,
            created_at=datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service_24 = router.service_for_chat(chat_24)
    service_27 = router.service_for_chat(chat_27)
    assert service_24 is not None
    assert service_27 is not None
    assert len(service_24.store.list_homework_verifications("2026-08-19")) == 1
    assert service_27.store.list_homework_verifications("2026-08-19") == []


def test_missing_homework_list_is_sent_once_as_post(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(original, missing_list_enabled=True)
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(incoming("om_image", "", message_type="image"))

    first = service.send_missing_list("2026-08-11", "oc_group")
    second = service.send_missing_list("2026-08-11", "oc_group")

    assert first == "om_summary"
    assert second == ""
    assert len(api.sent) == 1
    _, text, _ = api.sent[0]
    assert text.startswith("20:00 未交作业名单")
    assert "图片作业已完成 1/2" in text
    assert "未完成（1人）：\n小王" in text


def test_manual_exclusion_is_hidden_from_counts_names_and_reminders(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
        missing_list_enabled=True,
    )
    api = FakeApi()
    api.base_records = [
        {
            "record_id": "rec_manual",
            "fields": {
                "记录键": "2026-08-11|ou_2",
                "组员姓名": "小王",
                "人工状态": "不参与统计",
                "人工备注": "仅组长可见的原因",
            },
        }
    ]
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(incoming("om_image", "", message_type="image"))

    assert service.send_reminder("2026-08-11", "oc_group") == ""
    assert api.reminders == []
    service.send_missing_list("2026-08-11", "oc_group")

    _, text, _ = api.sent[-1]
    assert "图片作业已完成 1/1" in text
    assert "未完成（0人）：\n无" in text
    assert "小王" not in text
    assert "请假" not in text
    assert "不参与统计" not in text
    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-11")
    }
    assert attendance["小王"].homework_status == "excluded"
    excluded_updates = [
        fields for record_id, fields in api.base_updates if record_id == "rec_manual"
    ]
    assert excluded_updates
    assert "人工状态" not in excluded_updates[-1]
    assert "人工备注" not in excluded_updates[-1]


def test_manual_status_can_mark_missing_member_as_completed(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(
        original,
        base_sync_enabled=True,
        base_token="bas_test",
        base_table_id="tbl_test",
        final_status_enabled=True,
    )
    api = FakeApi()
    api.base_records = [
        {
            "record_id": "rec_manual",
            "fields": {
                "记录键": "2026-08-11|name:小王",
                "组员姓名": "小王",
                "人工状态": "正常提交",
            },
        }
    ]
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(incoming("om_image", "", message_type="image"))

    service.send_final_status("2026-08-11", "oc_group")

    _, text, _ = api.sent[-1]
    assert "图片作业已完成 2/2" in text
    assert "未完成（0人）：\n无" in text
    attendance = {
        record.sender_name: record for record in store.list_daily_attendance("2026-08-11")
    }
    assert attendance["小王"].homework_status == "completed"


def test_missing_list_includes_submission_after_previous_20(tmp_path):
    original = make_settings(tmp_path)
    settings = replace(original, missing_list_enabled=True)
    api = FakeApi()
    store = LocalStore(settings.db_path)
    service = GroupSummaryService(settings, api, FakeSummarizer(), store)
    service.handle_message(
        incoming(
            "om_previous_evening",
            "",
            message_type="image",
            created_at=datetime(2026, 8, 10, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    )

    service.send_missing_list("2026-08-11", "oc_group")

    _, text, _ = api.sent[0]
    assert "图片作业已完成 1/2" in text
    assert "未完成（1人）：\n小王" in text


def test_group_router_writes_each_chat_to_its_own_database(tmp_path):
    chat_27 = "oc_27"
    chat_24 = "oc_24"
    settings = replace(
        make_settings(tmp_path),
        group_databases={
            chat_27: str(tmp_path / "group_27.sqlite3"),
            chat_24: str(tmp_path / "group_24.sqlite3"),
        },
        capture_only_chat_ids=(chat_24,),
    )
    api = FakeApi()
    router = GroupServiceRouter(settings, api, FakeSummarizer())

    assert router.handle_message(incoming("om_27", "27组消息", chat_id=chat_27)) is True
    assert router.handle_message(incoming("om_24", "24组消息", chat_id=chat_24)) is True

    service_27 = router.service_for_chat(chat_27)
    service_24 = router.service_for_chat(chat_24)
    assert service_27 is not None
    assert service_24 is not None
    assert len(service_27.store.list_messages(chat_27, 0, 9_999_999_999_999, 10)) == 1
    assert len(service_24.store.list_messages(chat_24, 0, 9_999_999_999_999, 10)) == 1
    assert service_27.store.list_messages(chat_24, 0, 9_999_999_999_999, 10) == []
    assert service_24.store.list_messages(chat_27, 0, 9_999_999_999_999, 10) == []
    assert service_27.settings.send_enabled is True
    assert service_24.settings.send_enabled is False
    assert service_24.settings.base_sync_enabled is False
    assert service_24.settings.report_members == ()


def test_group_router_applies_independent_profile_without_enabling_sends(tmp_path):
    chat_27 = "oc_27"
    chat_24 = "oc_24"
    settings = replace(
        make_settings(tmp_path),
        group_databases={
            chat_27: str(tmp_path / "group_27.sqlite3"),
            chat_24: str(tmp_path / "group_24.sqlite3"),
        },
        group_profiles={
            chat_24: {
                "report_title": "24组日报",
                "report_members": ["李"],
                "member_aliases": {"ou_li": "李"},
                "leader_member_ids": ["ou_leader"],
                "additional_excluded_member_ids": ["ou_manager"],
                "send_enabled": False,
                "base_sync_enabled": True,
                "base_token": "bas_24",
                "base_table_id": "tbl_24",
                "report_link": "https://example.com/base/24",
                "assignment_deadline_overrides": {},
            }
        },
        capture_only_chat_ids=(),
        assignment_deadline_overrides={"2026-08-17": "2026-08-18 14:00"},
    )
    router = GroupServiceRouter(settings, FakeApi(), FakeSummarizer())

    service_27 = router.service_for_chat(chat_27)
    service_24 = router.service_for_chat(chat_24)
    assert service_27 is not None
    assert service_24 is not None
    assert service_27.settings.member_aliases["ou_1"] == "小李"
    assert service_24.settings.member_aliases == {"ou_li": "李"}
    assert service_24.settings.leader_member_ids == ("ou_leader",)
    assert service_24.settings.report_members == ("李",)
    assert service_24.settings.send_enabled is False
    assert service_24.settings.base_sync_enabled is True
    assert service_24.settings.base_token == "bas_24"
    assert service_24.settings.assignment_deadline_overrides == {}
    assert "ou_manager" in service_24.settings.excluded_member_ids
