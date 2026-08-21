from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

from .models import ParsedContent


PLACEHOLDERS = {
    "image": "[图片]",
    "file": "[文件]",
    "audio": "[语音]",
    "media": "[视频]",
    "sticker": "[表情]",
    "share_chat": "[分享群聊]",
    "share_user": "[分享联系人]",
    "interactive": "[互动卡片]",
}


def _as_json(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content


def _walk_post(node: Any, parts: List[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_post(item, parts)
        return
    if not isinstance(node, dict):
        return

    tag = node.get("tag")
    if tag in {"text", "md", "a"} and node.get("text"):
        parts.append(str(node["text"]))
    elif tag == "at":
        name = node.get("user_name") or node.get("name")
        if name:
            parts.append(f"@{name}")
    elif tag == "img":
        parts.append("[图片]")

    for key, value in node.items():
        if key not in {"tag", "text", "user_name", "name", "image_key"}:
            _walk_post(value, parts)


def resolve_mentions(content: str, mentions: Sequence[Any]) -> str:
    """把事件正文中的 @_user_N 占位符替换为飞书给出的成员昵称。"""
    replacements: Dict[str, str] = {}
    for mention in mentions:
        key = str(getattr(mention, "key", "") or "")
        name = str(getattr(mention, "name", "") or "")
        if key and name:
            replacements[key] = f"@{name}"
    if not replacements:
        return content

    def replace(node: Any) -> Any:
        if isinstance(node, str):
            for key, value in replacements.items():
                node = node.replace(key, value)
            return node
        if isinstance(node, list):
            return [replace(item) for item in node]
        if isinstance(node, dict):
            return {key: replace(value) for key, value in node.items()}
        return node

    resolved = replace(_as_json(content))
    if isinstance(resolved, str):
        return resolved
    return json.dumps(resolved, ensure_ascii=False)


def decode_content(message_type: str, content: str) -> ParsedContent:
    """把飞书原始消息转换成适合群聊总结的可读文本。"""
    payload = _as_json(content)
    if message_type == "text":
        if isinstance(payload, dict):
            return ParsedContent(text=str(payload.get("text", "")).strip())
        return ParsedContent(text=str(payload).strip())

    if message_type == "post":
        parts: List[str] = []
        if isinstance(payload, dict):
            _walk_post(payload, parts)
        return ParsedContent(text=" ".join(part.strip() for part in parts if part.strip()))

    if message_type == "file":
        file_name = ""
        if isinstance(payload, dict):
            file_name = str(payload.get("file_name") or payload.get("name") or "").strip()
        suffix = f" {file_name}" if file_name else ""
        return ParsedContent(text=f"[文件]{suffix}")

    if message_type in PLACEHOLDERS:
        return ParsedContent(text=PLACEHOLDERS[message_type])

    if message_type == "merge_forward":
        return ParsedContent(text="")

    if isinstance(payload, dict):
        text = payload.get("text") or payload.get("content") or ""
        return ParsedContent(text=str(text).strip())
    return ParsedContent(text=str(payload).strip() if payload else "")


def extract_merged_children(items: Sequence[Dict[str, Any]]) -> ParsedContent:
    parts: List[str] = []
    for item in items:
        message_type = str(item.get("msg_type") or item.get("message_type") or "")
        if message_type == "merge_forward":
            continue
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        parsed = decode_content(message_type, str(body.get("content") or item.get("content") or ""))
        if not parsed.text:
            continue
        sender = item.get("sender") or {}
        name = sender.get("name") if isinstance(sender, dict) else ""
        parts.append(f"{name}：{parsed.text}" if name else parsed.text)
    return ParsedContent(text="；".join(parts))
