from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Sequence

from daily_report_bot.models import StoredMessage
from daily_report_bot.store import LocalStore


_IMAGE_TOKEN = re.compile(r"\[Image:\s*[^\]]+\]")
_MARKDOWN_IMAGE = re.compile(r"!\[Image\]\([^\)]+\)")


def _parse_cli_time(value: str) -> int:
    value = value.strip()
    if not value:
        return 0
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _parse_iso_time(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _normalize_content(message_type: str, value: Any) -> str:
    text = str(value or "").strip()
    text = _IMAGE_TOKEN.sub("[图片]", text)
    text = _MARKDOWN_IMAGE.sub("[图片]", text)
    if message_type == "image":
        return "[图片]"
    if message_type == "file" and not text:
        return "[文件]"
    if message_type in {"media", "video"} and not text:
        return "[视频]"
    return text or f"[{message_type}]"


def _flatten(messages: Iterable[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    seen: set[str] = set()
    for root in messages:
        candidates = [root, *(root.get("thread_replies") or [])]
        for item in candidates:
            message_id = str(item.get("message_id") or "")
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            if item.get("deleted"):
                continue
            sender = item.get("sender") or {}
            if sender.get("sender_type") != "user" or not sender.get("id"):
                continue
            if item is not root and not item.get("root_id"):
                item = dict(item)
                item["root_id"] = str(root.get("message_id") or "")
            yield item


def _load_aliases(path: Path, chat_id: str) -> Dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    profile = payload.get(chat_id) or {}
    aliases = profile.get("member_aliases") or {}
    return {str(key): str(value) for key, value in aliases.items()}


def _run_json(command: Sequence[str], env: Dict[str, str]) -> Dict[str, Any]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lark-cli 返回的不是 JSON：{completed.stdout[:500]}") from exc
    if not payload.get("ok"):
        raise RuntimeError(json.dumps(payload.get("error") or payload, ensure_ascii=False))
    return payload


def backfill(
    *,
    chat_id: str,
    start: str,
    end: str,
    db_path: Path,
    profiles_path: Path,
    cli_command: str,
) -> Dict[str, int]:
    aliases = _load_aliases(profiles_path, chat_id)
    store = LocalStore(db_path)
    base_command = shlex.split(cli_command)
    env = dict(os.environ)
    env["TZ"] = "UTC"
    start_ms = _parse_iso_time(start)
    end_ms = _parse_iso_time(end)
    page_token = ""
    page_count = 0
    fetched = 0
    inserted = 0
    seen_tokens: set[str] = set()
    while True:
        command = [
            *base_command,
            "im",
            "+chat-messages-list",
            "--as",
            "bot",
            "--chat-id",
            chat_id,
            "--start",
            start,
            "--end",
            end,
            "--order",
            "asc",
            "--page-size",
            "50",
            "--no-reactions",
            "--format",
            "json",
        ]
        if page_token:
            command.extend(["--page-token", page_token])
        payload = _run_json(command, env)
        data = payload.get("data") or {}
        page_count += 1
        for item in _flatten(data.get("messages") or []):
            created_ms = _parse_cli_time(str(item.get("create_time") or ""))
            if not start_ms <= created_ms < end_ms:
                continue
            sender = item.get("sender") or {}
            open_id = str(sender.get("id") or "")
            sender_name = aliases.get(open_id) or str(sender.get("name") or "")
            fetched += 1
            if store.add_message(
                StoredMessage(
                    message_id=str(item["message_id"]),
                    chat_id=chat_id,
                    sender_open_id=open_id,
                    sender_name=sender_name or f"成员-{open_id[-6:]}",
                    message_type=str(item.get("msg_type") or "text"),
                    content=_normalize_content(
                        str(item.get("msg_type") or "text"), item.get("content")
                    ),
                    create_time_ms=created_ms,
                    parent_id=str(item.get("parent_id") or ""),
                    root_id=str(item.get("root_id") or ""),
                    thread_id=str(item.get("thread_id") or ""),
                )
            ):
                inserted += 1
        next_token = str(data.get("page_token") or "")
        if not data.get("has_more") or not next_token:
            break
        if next_token in seen_tokens:
            raise RuntimeError("lark-cli 分页 token 重复，已停止以避免死循环")
        seen_tokens.add(next_token)
        page_token = next_token
    renamed = 0
    with store._lock, store._connect() as connection:
        for open_id, name in aliases.items():
            cursor = connection.execute(
                "UPDATE group_messages SET sender_name = ? "
                "WHERE sender_open_id = ? AND sender_name <> ?",
                (name, open_id, name),
            )
            renamed += cursor.rowcount
    return {
        "pages": page_count,
        "fetched": fetched,
        "inserted": inserted,
        "renamed": renamed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="把飞书群历史消息回填到独立 SQLite")
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--start", required=True, help="ISO 8601 起始时间，包含")
    parser.add_argument("--end", required=True, help="ISO 8601 结束时间，不包含")
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--profiles-path", required=True, type=Path)
    parser.add_argument(
        "--cli-command",
        default="npx -y @larksuite/cli@1.0.65",
        help="用于读取历史消息的 lark-cli 命令",
    )
    args = parser.parse_args()
    result = backfill(
        chat_id=args.chat_id,
        start=args.start,
        end=args.end,
        db_path=args.db_path,
        profiles_path=args.profiles_path,
        cli_command=args.cli_command,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
