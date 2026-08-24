from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import lark_oapi as lark
from apscheduler.schedulers.background import BackgroundScheduler

from .api import FeishuApi
from .config import ConfigurationError, load_settings
from .llm import Summarizer
from .models import IncomingMessage
from .parser import resolve_mentions
from .router import GroupServiceRouter
from .service import GroupSummaryService


logger = logging.getLogger(__name__)


def _incoming(event: Any) -> IncomingMessage:
    sender = event.event.sender
    message = event.event.message
    sender_id = sender.sender_id
    return IncomingMessage(
        message_id=str(message.message_id),
        chat_id=str(message.chat_id),
        chat_type=str(message.chat_type or ""),
        sender_open_id=str(sender_id.open_id),
        sender_type=str(sender.sender_type or ""),
        message_type=str(message.message_type),
        content=resolve_mentions(
            str(message.content or ""), getattr(message, "mentions", None) or []
        ),
        create_time_ms=int(message.create_time),
        parent_id=str(message.parent_id) if getattr(message, "parent_id", None) else None,
        root_id=str(message.root_id) if getattr(message, "root_id", None) else None,
        thread_id=str(message.thread_id) if getattr(message, "thread_id", None) else None,
    )


def _bot_added_chat_id(event: Any) -> str:
    event_data = getattr(event, "event", None)
    return str(getattr(event_data, "chat_id", "") or "")


def _runtime(
    env_file: str,
) -> tuple[Any, FeishuApi, Summarizer, GroupServiceRouter]:
    settings = load_settings(env_file)
    settings.validate(require_secrets=True)
    api = FeishuApi(settings.app_id, settings.app_secret)
    summarizer = Summarizer(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model,
        max_chars_per_request=settings.max_chars_per_request,
    )
    router = GroupServiceRouter(settings, api, summarizer)
    return settings, api, summarizer, router


def _register_summary_job(scheduler: Any, settings: Any, service: GroupSummaryService) -> bool:
    if not settings.send_enabled or not getattr(settings, "summary_schedule_enabled", True):
        return False
    scheduler.add_job(
        service.send_due_summaries,
        "cron",
        hour=settings.summary_hour,
        minute=settings.summary_minute,
        id="group-daily-summary",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    return True


def _register_reminder_job(scheduler: Any, settings: Any, service: GroupSummaryService) -> bool:
    if not settings.send_enabled or not settings.reminder_enabled:
        return False
    scheduler.add_job(
        service.send_due_reminders,
        "cron",
        hour=settings.reminder_hour,
        minute=settings.reminder_minute,
        id="group-attendance-reminder",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=1800,
    )
    return True


def _register_missing_list_job(scheduler: Any, settings: Any, service: GroupSummaryService) -> bool:
    if not settings.send_enabled or not settings.missing_list_enabled:
        return False
    scheduler.add_job(
        service.send_due_missing_lists,
        "cron",
        hour=settings.missing_list_hour,
        minute=settings.missing_list_minute,
        id="group-missing-homework-list",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=1800,
    )
    return True


def _register_final_status_job(scheduler: Any, settings: Any, service: GroupSummaryService) -> bool:
    if not settings.send_enabled or not settings.final_status_enabled:
        return False
    scheduler.add_job(
        service.send_due_final_statuses,
        "cron",
        hour=settings.final_status_hour,
        minute=settings.final_status_minute,
        id="group-final-attendance-status",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=1800,
    )
    return True


def _register_makeup_reminder_job(
    scheduler: Any, settings: Any, service: GroupSummaryService
) -> bool:
    if not settings.send_enabled or not settings.makeup_reminder_enabled:
        return False
    scheduler.add_job(
        service.send_due_makeup_reminders,
        "cron",
        hour=settings.makeup_reminder_hour,
        minute=settings.makeup_reminder_minute,
        id="group-makeup-reminder",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=1800,
    )
    return True


def _register_makeup_summary_job(
    scheduler: Any, settings: Any, service: GroupSummaryService
) -> bool:
    if not settings.send_enabled or not settings.makeup_summary_enabled:
        return False
    scheduler.add_job(
        service.send_due_makeup_summaries,
        "cron",
        hour=settings.makeup_summary_hour,
        minute=settings.makeup_summary_minute,
        id="group-makeup-summary",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=1800,
    )
    return True


def _register_assignment_deadline_jobs(
    scheduler: Any, settings: Any, service: GroupSummaryService
) -> int:
    now = datetime.now(tz=settings.tz)
    registered = 0
    for report_date in sorted(settings.assignment_deadline_overrides):
        deadline = settings.assignment_deadline(report_date)
        run_at = deadline + timedelta(minutes=1)
        if run_at < now - timedelta(hours=24):
            continue
        scheduler.add_job(
            service.sync_assignment_deadline,
            "date",
            run_date=run_at,
            args=(report_date,),
            id=f"assignment-deadline-sync-{report_date}",
            replace_existing=True,
            misfire_grace_time=86_400,
        )
        registered += 1
    return registered


def run_bot(env_file: str) -> int:
    settings, api, summarizer, service = _runtime(env_file)

    def process(incoming: IncomingMessage) -> None:
        try:
            service.handle_message(incoming)
        except Exception:
            logger.exception("处理群消息失败：%s", incoming.message_id)

    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="group-summary")

    def on_message(event: Any) -> None:
        executor.submit(process, _incoming(event))

    def process_bot_added(chat_id: str) -> None:
        if not chat_id:
            logger.warning("收到缺少 chat_id 的机器人入群事件")
            return
        try:
            service.handle_bot_added(chat_id)
        except Exception:
            logger.exception("处理机器人入群事件失败：%s", chat_id)

    def on_bot_added(event: Any) -> None:
        executor.submit(process_bot_added, _bot_added_chat_id(event))

    handler = (
        lark.EventDispatcherHandler.builder("", "", lark.LogLevel.WARNING)
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_im_chat_member_bot_added_v1(on_bot_added)
        .build()
    )

    scheduler = BackgroundScheduler(timezone=settings.timezone)
    _register_reminder_job(scheduler, settings, service)
    _register_missing_list_job(scheduler, settings, service)
    _register_final_status_job(scheduler, settings, service)
    _register_makeup_reminder_job(scheduler, settings, service)
    _register_makeup_summary_job(scheduler, settings, service)
    _register_assignment_deadline_jobs(scheduler, settings, service)
    _register_summary_job(scheduler, settings, service)
    scheduler.start()
    logger.info(
        "群聊总结机器人启动：时区=%s 催交=%02d:%02d 未交名单=%02d:%02d "
        "最终汇总=%02d:%02d 补交提醒=%02d:%02d 补交汇总=%02d:%02d "
        "日报=%s 模型=%s 发送=%s",
        settings.timezone,
        settings.reminder_hour,
        settings.reminder_minute,
        settings.missing_list_hour,
        settings.missing_list_minute,
        settings.final_status_hour,
        settings.final_status_minute,
        settings.makeup_reminder_hour,
        settings.makeup_reminder_minute,
        settings.makeup_summary_hour,
        settings.makeup_summary_minute,
        (
            f"{settings.summary_hour:02d}:{settings.summary_minute:02d}"
            if settings.summary_schedule_enabled
            else "关闭"
        ),
        settings.llm_model,
        "开启" if settings.send_enabled else "关闭（只收集）",
    )

    ws_client = lark.ws.Client(
        settings.app_id,
        settings.app_secret,
        log_level=lark.LogLevel.WARNING,
        event_handler=handler,
    )
    try:
        ws_client.start()
    finally:
        scheduler.shutdown(wait=False)
        executor.shutdown(wait=True)
        api.close()
        summarizer.close()
    return 0


def doctor(env_file: str) -> int:
    settings, api, summarizer, router = _runtime(env_file)
    try:
        bot_name = api.check_bot()
        model_reply = summarizer.probe()
    finally:
        api.close()
        summarizer.close()
    print(f"飞书机器人连通：{bot_name}")
    print(f"总结模型连通：{settings.llm_model}（{model_reply[:30]}）")
    print(f"已识别群聊：{len(router.known_chat_ids())} 个")
    return 0


def summary(env_file: str, day: str, chat_id: str, dry_run: bool, force: bool) -> int:
    settings, api, summarizer, router = _runtime(env_file)
    try:
        datetime.strptime(day, "%Y-%m-%d")
        chats = [chat_id] if chat_id else router.known_chat_ids()
        if not chats:
            raise ConfigurationError("没有可用群 ID，请传 --chat-id，或先让机器人收到一条群消息")
        for target in chats:
            service = router.service_for_chat(target)
            if service is None:
                raise ConfigurationError(f"群没有配置独立数据库：{target}")
            result = service.send_summary(day, target, force=force, dry_run=dry_run)
            if result:
                print(result.text)
                print(
                    f"\n--- 群 {target}；消息 {result.message_count}；"
                    f"参与者 {result.participant_count} ---"
                )
            else:
                print(f"{day} 没有可总结消息，或总结已经发送：{target}")
    finally:
        api.close()
        summarizer.close()
    return 0


def sync_attendance(
    env_file: str, from_day: str, to_day: str, chat_id: str, stored_only: bool
) -> int:
    settings, api, summarizer, router = _runtime(env_file)
    try:
        start = datetime.strptime(from_day, "%Y-%m-%d").date()
        end = datetime.strptime(to_day, "%Y-%m-%d").date()
        if end < start:
            raise ConfigurationError("结束日期不能早于开始日期")
        chats = [chat_id] if chat_id else router.known_chat_ids()
        if not chats and not stored_only:
            raise ConfigurationError("没有可用群 ID")
        day = start
        while day <= end:
            if stored_only:
                count = 0
                for service in router._all_services():
                    count += service.sync_stored_attendance_date(day.isoformat())
                print(f"{day.isoformat()}：同步已存状态 {count} 条")
            else:
                for target in chats:
                    service = router.service_for_chat(target)
                    if service is None:
                        raise ConfigurationError(f"群没有配置独立数据库：{target}")
                    count = service.sync_attendance_date(day.isoformat(), target)
                    print(f"{day.isoformat()} {target}：同步 {count} 条")
            day += timedelta(days=1)
    finally:
        api.close()
        summarizer.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="飞书群消息每日总结机器人")
    parser.add_argument("--env-file", default=".env")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="运行长连接机器人")
    subparsers.add_parser("doctor", help="检查飞书和总结模型连通性")
    summary_parser = subparsers.add_parser("summary", help="手动生成或发送某日群聊总结")
    summary_parser.add_argument("--date", required=True, dest="day")
    summary_parser.add_argument("--chat-id", default="")
    summary_parser.add_argument("--dry-run", action="store_true")
    summary_parser.add_argument("--force", action="store_true")
    sync_parser = subparsers.add_parser("sync", help="同步本地打卡状态到多维表格")
    sync_parser.add_argument("--from-date", required=True, dest="from_day")
    sync_parser.add_argument("--to-date", required=True, dest="to_day")
    sync_parser.add_argument("--chat-id", default="")
    sync_parser.add_argument("--stored-only", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        if args.command == "run":
            return run_bot(args.env_file)
        if args.command == "doctor":
            return doctor(args.env_file)
        if args.command == "summary":
            return summary(args.env_file, args.day, args.chat_id, args.dry_run, args.force)
        if args.command == "sync":
            return sync_attendance(
                args.env_file,
                args.from_day,
                args.to_day,
                args.chat_id,
                args.stored_only,
            )
    except (ConfigurationError, ValueError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
