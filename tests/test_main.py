from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from daily_report_bot.main import (
    _bot_added_chat_id,
    _incoming,
    _register_assignment_deadline_jobs,
    _register_final_status_job,
    _register_makeup_reminder_job,
    _register_makeup_summary_job,
    _register_missing_list_job,
    _register_summary_job,
)


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, function, trigger, **kwargs):
        self.jobs.append((function, trigger, kwargs))


class FakeService:
    def send_due_summaries(self):
        return []

    def send_due_missing_lists(self):
        return []

    def send_due_final_statuses(self):
        return []

    def send_due_makeup_reminders(self):
        return []

    def send_due_makeup_summaries(self):
        return []

    def sync_assignment_deadline(self, report_date):
        return 0


def test_incoming_keeps_topic_relationship_fields():
    message = SimpleNamespace(
        message_id="om_reply",
        chat_id="oc_group",
        chat_type="group",
        message_type="file",
        content='{"file_key":"file_x","file_name":"会赢吗.html"}',
        create_time="1786960190662",
        mentions=[],
        parent_id="om_root",
        root_id="om_root",
        thread_id="omt_topic",
    )
    sender = SimpleNamespace(
        sender_id=SimpleNamespace(open_id="ou_1"),
        sender_type="user",
    )

    result = _incoming(SimpleNamespace(event=SimpleNamespace(message=message, sender=sender)))

    assert result.parent_id == "om_root"
    assert result.root_id == "om_root"
    assert result.thread_id == "omt_topic"


def test_bot_added_chat_id_reads_join_event():
    event = SimpleNamespace(event=SimpleNamespace(chat_id="oc_new_group"))

    assert _bot_added_chat_id(event) == "oc_new_group"
    assert _bot_added_chat_id(SimpleNamespace(event=None)) == ""


def test_capture_only_mode_registers_no_summary_job():
    scheduler = FakeScheduler()
    settings = SimpleNamespace(send_enabled=False, summary_hour=23, summary_minute=0)

    assert _register_summary_job(scheduler, settings, FakeService()) is False
    assert scheduler.jobs == []


def test_send_mode_registers_one_daily_summary_job():
    scheduler = FakeScheduler()
    settings = SimpleNamespace(send_enabled=True, summary_hour=23, summary_minute=0)
    service = FakeService()

    assert _register_summary_job(scheduler, settings, service) is True
    assert len(scheduler.jobs) == 1
    function, trigger, kwargs = scheduler.jobs[0]
    assert function == service.send_due_summaries
    assert trigger == "cron"
    assert kwargs == {
        "hour": 23,
        "minute": 0,
        "id": "group-daily-summary",
        "replace_existing": True,
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 3600,
    }


def test_send_mode_registers_missing_list_at_20():
    scheduler = FakeScheduler()
    settings = SimpleNamespace(
        send_enabled=True,
        missing_list_enabled=True,
        missing_list_hour=20,
        missing_list_minute=0,
    )
    service = FakeService()

    assert _register_missing_list_job(scheduler, settings, service) is True
    assert scheduler.jobs == [
        (
            service.send_due_missing_lists,
            "cron",
            {
                "hour": 20,
                "minute": 0,
                "id": "group-missing-homework-list",
                "replace_existing": True,
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 1800,
            },
        )
    ]


def test_send_mode_registers_final_status_at_20():
    scheduler = FakeScheduler()
    settings = SimpleNamespace(
        send_enabled=True,
        final_status_enabled=True,
        final_status_hour=20,
        final_status_minute=0,
    )
    service = FakeService()

    assert _register_final_status_job(scheduler, settings, service) is True
    function, trigger, kwargs = scheduler.jobs[0]
    assert function == service.send_due_final_statuses
    assert trigger == "cron"
    assert kwargs["hour"] == 20
    assert kwargs["minute"] == 0


def test_send_mode_registers_makeup_jobs_at_17_and_20():
    reminder_scheduler = FakeScheduler()
    summary_scheduler = FakeScheduler()
    settings = SimpleNamespace(
        send_enabled=True,
        makeup_reminder_enabled=True,
        makeup_reminder_hour=17,
        makeup_reminder_minute=0,
        makeup_summary_enabled=True,
        makeup_summary_hour=20,
        makeup_summary_minute=0,
    )
    service = FakeService()

    assert _register_makeup_reminder_job(reminder_scheduler, settings, service) is True
    assert _register_makeup_summary_job(summary_scheduler, settings, service) is True
    assert reminder_scheduler.jobs[0][0] == service.send_due_makeup_reminders
    assert reminder_scheduler.jobs[0][2]["hour"] == 17
    assert reminder_scheduler.jobs[0][2]["id"] == "group-makeup-reminder"
    assert summary_scheduler.jobs[0][0] == service.send_due_makeup_summaries
    assert summary_scheduler.jobs[0][2]["hour"] == 20
    assert summary_scheduler.jobs[0][2]["id"] == "group-makeup-summary"


def test_override_registers_one_time_deadline_sync():
    scheduler = FakeScheduler()
    deadline = datetime.now(tz=ZoneInfo("Asia/Shanghai")) + timedelta(hours=1)
    settings = SimpleNamespace(
        tz=ZoneInfo("Asia/Shanghai"),
        assignment_deadline_overrides={"2026-08-17": deadline.isoformat()},
        assignment_deadline=lambda report_date: deadline,
    )
    service = FakeService()

    assert _register_assignment_deadline_jobs(scheduler, settings, service) == 1
    function, trigger, kwargs = scheduler.jobs[0]
    assert function == service.sync_assignment_deadline
    assert trigger == "date"
    assert kwargs["run_date"] == deadline + timedelta(minutes=1)
    assert kwargs["args"] == ("2026-08-17",)
