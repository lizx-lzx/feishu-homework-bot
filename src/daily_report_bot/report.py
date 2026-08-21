from __future__ import annotations

import re
from typing import Any, Dict, List


_LINK_LINE = re.compile(r"^\[([^\]]+)]\((https?://[^)]+)\)$")
_INLINE_MARKUP = re.compile(r"\[([^\]]+)]\((https?://[^)]+)\)|\*\*(.+?)\*\*|__(.+?)__|`([^`]+)`")
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+")
_MARKDOWN_BULLET = re.compile(r"^[-*+]\s+")
_MARKDOWN_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")


def build_post_content(text: str) -> Dict[str, Any]:
    """把日报纯文本转换为飞书原生 post，而不是 Markdown 文本。"""
    lines = text.splitlines()
    title = _plain_title(lines[0]) if lines else ""
    body = lines[1:] if lines else []
    content: List[List[Dict[str, Any]]] = []

    for raw_line in body:
        line = raw_line.strip()
        if not line:
            content.append([{"tag": "text", "text": "\u200b"}])
            continue

        link = _LINK_LINE.fullmatch(line)
        if link:
            content.append([{"tag": "a", "text": link.group(1), "href": link.group(2)}])
            continue

        if _MARKDOWN_RULE.fullmatch(line):
            continue

        markdown_heading = bool(_MARKDOWN_HEADING.match(line))
        line = _MARKDOWN_HEADING.sub("", line)
        line = _MARKDOWN_BULLET.sub("• ", line)
        nodes = _inline_nodes(line)
        if markdown_heading or _is_heading(line):
            for node in nodes:
                if node["tag"] == "text":
                    node["style"] = sorted(set(node.get("style", [])) | {"bold"})
        content.append(nodes)

    return {"zh_cn": {"title": title, "content": content}}


def _plain_title(title: str) -> str:
    value = _MARKDOWN_HEADING.sub("", title.strip())
    if (value.startswith("**") and value.endswith("**")) or (
        value.startswith("__") and value.endswith("__")
    ):
        value = value[2:-2]
    return value.strip("`")


def _inline_nodes(line: str) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    position = 0
    for match in _INLINE_MARKUP.finditer(line):
        if match.start() > position:
            nodes.append({"tag": "text", "text": line[position : match.start()]})
        if match.group(1) is not None:
            nodes.append({"tag": "a", "text": match.group(1), "href": match.group(2)})
        else:
            value = match.group(3) or match.group(4) or match.group(5) or ""
            node: Dict[str, Any] = {"tag": "text", "text": value}
            if match.group(3) is not None or match.group(4) is not None:
                node["style"] = ["bold"]
            nodes.append(node)
        position = match.end()
    if position < len(line):
        nodes.append({"tag": "text", "text": line[position:]})
    return nodes or [{"tag": "text", "text": "​"}]


def _is_heading(line: str) -> bool:
    if line.startswith(("📊 ", "✅ ", "⚠️ ", "📝 ", "💬 ", "🔍 ", "📎 ")):
        return True
    return line in {"方法沉淀：", "待解决问题："}
