from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs" / "飞书作业统计机器人搭建教程.md"
START_MARKER = "## 十七、完整可运行源码"
END_MARKER = "## 十八、从源码启动"
SOURCE_FILES = (
    ("pyproject.toml", "toml"),
    (".env.example", "dotenv"),
    ("config/group_profiles.example.json", "json"),
    ("deploy/feishu-group-summary.service", "ini"),
    ("deploy/feishu-group-summary-llm.conf", "ini"),
    (".github/workflows/tests.yml", "yaml"),
    ("src/daily_report_bot/__init__.py", "python"),
    ("src/daily_report_bot/models.py", "python"),
    ("src/daily_report_bot/config.py", "python"),
    ("src/daily_report_bot/parser.py", "python"),
    ("src/daily_report_bot/api.py", "python"),
    ("src/daily_report_bot/llm.py", "python"),
    ("src/daily_report_bot/report.py", "python"),
    ("src/daily_report_bot/store.py", "python"),
    ("src/daily_report_bot/service.py", "python"),
    ("src/daily_report_bot/router.py", "python"),
    ("src/daily_report_bot/main.py", "python"),
)


def build_source_section() -> str:
    parts = [
        START_MARKER,
        "",
        "下面是机器人当前运行所需的完整源码。每个三级标题就是文件保存路径。",
        "真实 App ID、Secret、API Key、chat_id、open_id、Base token、成员名单和服务器状态均不写入文档。",
        "",
        "这一节由 `scripts/update_tutorial_source.py` 从仓库实际文件生成，避免教程和源码版本脱节。",
        "",
    ]
    for relative_path, language in SOURCE_FILES:
        source = (ROOT / relative_path).read_text(encoding="utf-8").rstrip()
        parts.extend(
            [
                f"### `{relative_path}`",
                "",
                f"```{language}",
                source,
                "```",
                "",
            ]
        )
    return "\n".join(parts).rstrip() + "\n\n"


def main() -> int:
    text = DOCUMENT.read_text(encoding="utf-8")
    start = text.index(START_MARKER)
    end = text.index(END_MARKER)
    DOCUMENT.write_text(
        text[:start] + build_source_section() + text[end:],
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
