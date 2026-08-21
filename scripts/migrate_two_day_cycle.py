from __future__ import annotations

import argparse

from daily_report_bot.main import _runtime


def main() -> int:
    parser = argparse.ArgumentParser(description="把旧的单日打卡记录合并到两日作业周期")
    parser.add_argument("--obsolete-date", required=True)
    parser.add_argument("--cycle-date", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--env-file", default="/dev/null")
    args = parser.parse_args()

    _settings, api, summarizer, router = _runtime(args.env_file)
    try:
        for chat_id in router.known_chat_ids():
            service = router.service_for_chat(chat_id)
            if service is None:
                continue
            prefix = f"{args.obsolete_date}|"
            base_targets = [
                str(record.get("record_id") or "")
                for record in api.list_base_records(
                    service.settings.base_token, service.settings.base_table_id
                )
                if str(record.get("fields", {}).get("记录键") or "").startswith(prefix)
                and record.get("record_id")
            ]
            local_count = len(service.store.list_daily_attendance(args.obsolete_date))
            print(
                f"{chat_id} 待删除 Base={len(base_targets)} 本地={local_count} "
                f"重算周期={args.cycle_date}"
            )
            if not args.apply:
                continue
            for record_id in base_targets:
                api.delete_base_record(
                    service.settings.base_token,
                    service.settings.base_table_id,
                    record_id,
                )
            deleted = service.store.delete_attendance_date(args.obsolete_date)
            synced = service.sync_attendance_date(args.cycle_date, chat_id)
            print(f"{chat_id} 已删除 Base={len(base_targets)} 本地={deleted} 已同步={synced}")
    finally:
        api.close()
        summarizer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
