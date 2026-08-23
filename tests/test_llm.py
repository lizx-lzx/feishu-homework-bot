import json

import httpx

from daily_report_bot.llm import Summarizer


def test_openai_compatible_summary_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "今日重点：完成联调"}}]},
        )

    summarizer = Summarizer(
        "https://example.com/v1",
        "secret",
        "test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert summarizer.summarize("2026-08-11", ["[10:00] 小李：完成联调"]) == "今日重点：完成联调"


def test_deepseek_v4_uses_non_thinking_mode():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode()
        assert '"model":"deepseek-v4-flash"' in payload
        assert '"thinking":{"type":"disabled"}' in payload
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "汇总完成"}}]},
        )

    summarizer = Summarizer(
        "https://api.deepseek.com",
        "secret",
        "deepseek-v4-flash",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert summarizer.summarize("2026-08-12", ["[10:00] 小李：已提交"]) == "汇总完成"


def test_reasoning_block_is_removed_from_compatible_model_output():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<think>Let me analyze the report.</think>\n\n"
                                "📝 每日复盘（0 人）\n无\n\n"
                                "💬 群内反馈\n无\n\n"
                                "🔍 方法与待解决\n方法沉淀：无\n待解决问题：无"
                            )
                        }
                    }
                ]
            },
        )

    summarizer = Summarizer(
        "https://example.com/v1",
        "secret",
        "MiniMax-M3",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert summarizer.summarize("2026-08-16", ["[10:00] 小李：已提交"]) == (
        "📝 每日复盘（0 人）\n无\n\n"
        "💬 群内反馈\n无\n\n"
        "🔍 方法与待解决\n方法沉淀：无\n待解决问题：无"
    )


def test_minimax_m3_disables_thinking_and_uses_current_token_parameter():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["reasoning_split"] is True
        assert payload["max_completion_tokens"] == 6000
        assert "max_tokens" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "📝 每日复盘（0 人）\n无\n\n"
                                "💬 群内反馈\n无\n\n"
                                "🔍 方法与待解决\n方法沉淀：无\n待解决问题：无"
                            )
                        }
                    }
                ]
            },
        )

    summarizer = Summarizer(
        "https://api.minimaxi.com/v1",
        "secret",
        "MiniMax-M3",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = summarizer.summarize("2026-08-17", ["[10:00] 小李：已提交"])
    assert "💬 群内反馈" in result


def test_minimax_interprets_natural_leader_override_as_strict_json():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "受限指令解析器" in payload["messages"][0]["content"]
        assert "卫安" in payload["messages"][1]["content"]
        assert payload["max_completion_tokens"] == 500
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```json\n"
                                '{"intent":"leader_override","targets":["卫安","米粒"],'
                                '"status":"late","assignment_number":3,"confidence":0.98}'
                                "\n```"
                            )
                        }
                    }
                ]
            },
        )

    summarizer = Summarizer(
        "https://api.minimaxi.com/v1",
        "secret",
        "MiniMax-M3",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert summarizer.interpret_leader_override(
        "第三次那份，卫安和米粒都算补了吧",
        ("卫安", "米粒", "，"),
    ) == {
        "targets": ("卫安", "米粒"),
        "status": "late",
        "assignment_number": 3,
        "confidence": 0.98,
    }


def test_minimax_rejects_unlisted_target_from_command_interpretation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"leader_override","targets":["不在群里的人"],'
                                '"status":"completed","assignment_number":3,'
                                '"confidence":0.99}'
                            )
                        }
                    }
                ]
            },
        )

    summarizer = Summarizer(
        "https://api.minimaxi.com/v1",
        "secret",
        "MiniMax-M3",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert summarizer.interpret_leader_override("他已经交了", ("卫安", "米粒")) is None


def test_minimax_m3_normalizes_review_count_placeholder_from_fixed_facts():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "📝 每日复盘（N 人）\n无\n\n"
                                "💬 群内反馈\n无\n\n"
                                "🔍 方法与待解决\n方法沉淀：无\n待解决问题：无"
                            )
                        }
                    }
                ]
            },
        )

    summarizer = Summarizer(
        "https://api.minimaxi.com/v1",
        "secret",
        "MiniMax-M3",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = summarizer.summarize("2026-08-18", ["成员甲：今日无复盘"])

    assert "📝 每日复盘（0 人）" in result
    assert "N 人" not in result


def test_minimax_m3_repairs_missing_review_member_once():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = (
            "📝 每日复盘（1 人）\n无\n\n"
            "💬 群内反馈\n无\n\n"
            "🔍 方法与待解决\n方法沉淀：无\n待解决问题：无"
            if calls == 1
            else (
                "📝 每日复盘（1 人）\n1. 小李\n完成复盘\n\n"
                "💬 群内反馈\n无\n\n"
                "🔍 方法与待解决\n方法沉淀：无\n待解决问题：无"
            )
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    summarizer = Summarizer(
        "https://api.minimaxi.com/v1",
        "secret",
        "MiniMax-M3",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    context = '有效复盘名单JSON：["小李"]'

    result = summarizer.summarize(
        "2026-08-17", ["[10:00] 小李：#复盘 完成复盘"], report_context=context
    )

    assert calls == 2
    assert "1. 小李" in result


def test_minimax_m3_repairs_raw_feishu_display_name():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        target = "飞书用户1234AB" if calls == 1 else "成员乙"
        content = (
            "📝 每日复盘（0 人）\n无\n\n"
            f"💬 群内反馈\n成员甲 → {target}：游戏打不开\n\n"
            "🔍 方法与待解决\n方法沉淀：无\n待解决问题：无"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    summarizer = Summarizer(
        "https://api.minimaxi.com/v1",
        "secret",
        "MiniMax-M3",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = summarizer.summarize("2026-08-17", ["成员甲：@成员乙 游戏打不开"])

    assert calls == 2
    assert "成员甲 → 成员乙" in result


def test_minimax_m3_repairs_checkin_tag_in_review_summary():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        review = "1. 凡：#0817 复盘打卡，完成视频探索" if calls == 1 else "1. 凡：完成视频探索"
        content = (
            f"📝 每日复盘（1 人）\n{review}\n\n"
            "💬 群内反馈\n无\n\n"
            "🔍 方法与待解决\n方法沉淀：无\n待解决问题：无"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    summarizer = Summarizer(
        "https://api.minimaxi.com/v1",
        "secret",
        "MiniMax-M3",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = summarizer.summarize(
        "2026-08-17",
        ["凡：#0817 复盘打卡，完成视频探索"],
        report_context='有效复盘名单JSON：["凡"]',
    )

    assert calls == 2
    assert "#0817" not in result


def test_reasoning_only_response_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "<think>only reasoning</think>"}}]},
        )

    summarizer = Summarizer(
        "https://example.com/v1",
        "secret",
        "MiniMax-M3",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        summarizer.summarize("2026-08-16", ["[10:00] 小李：已提交"])
    except RuntimeError as exc:
        assert "只返回了思考过程" in str(exc)
    else:
        raise AssertionError("reasoning-only responses must be rejected")
