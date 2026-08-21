from daily_report_bot.report import build_post_content


def test_report_is_rendered_as_native_post_without_markdown_nodes():
    report = (
        "进阶营作业群・每日日报\n\n"
        "📊 今日总览\n\n"
        "仅完成图片：成员甲、成员乙\n\n"
        "📎 打卡表链接\n\n"
        "[点击查看打卡表](https://example.com/base)"
    )

    post = build_post_content(report)

    assert post["zh_cn"]["title"] == "进阶营作业群・每日日报"
    nodes = [node for row in post["zh_cn"]["content"] for node in row]
    assert not any(node.get("tag") == "md" for node in nodes)
    assert any(
        node.get("tag") == "a" and node.get("href") == "https://example.com/base" for node in nodes
    )
    assert "成员甲、成员乙" in "".join(node.get("text", "") for node in nodes)
    assert "成员名" not in "".join(node.get("text", "") for node in nodes)


def test_markdown_fallback_is_converted_to_native_rich_text():
    report = (
        "# **进阶营作业群・每日日报**\n\n"
        "## 📊 今日总览\n"
        "**完成图片作业**：18/22\n"
        "- 成员甲已完成\n"
        "[点击查看打卡表](https://example.com/base)\n"
        "---"
    )

    post = build_post_content(report)

    assert post["zh_cn"]["title"] == "进阶营作业群・每日日报"
    nodes = [node for row in post["zh_cn"]["content"] for node in row]
    visible_text = "".join(node.get("text", "") for node in nodes)
    assert "#" not in visible_text
    assert "**" not in visible_text
    assert "- 成员甲" not in visible_text
    assert "• 成员甲" in visible_text
    assert any(node.get("style") == ["bold"] for node in nodes)
    assert any(node.get("tag") == "a" for node in nodes)
