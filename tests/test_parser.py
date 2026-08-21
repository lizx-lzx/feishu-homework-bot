import json
from types import SimpleNamespace

from daily_report_bot.parser import decode_content, extract_merged_children, resolve_mentions


def test_decode_text_message():
    parsed = decode_content("text", json.dumps({"text": "今天完成联调"}, ensure_ascii=False))
    assert parsed.text == "今天完成联调"


def test_decode_post_keeps_text_mentions_and_image_placeholder():
    content = json.dumps(
        {
            "zh_cn": {
                "content": [
                    [{"tag": "text", "text": "进展"}, {"tag": "at", "user_name": "小李"}],
                    [{"tag": "img", "image_key": "img_123"}],
                ]
            }
        }
    )
    assert decode_content("post", content).text == "进展 @小李 [图片]"


def test_decode_media_as_readable_placeholder():
    assert decode_content("image", '{"image_key":"img_x"}').text == "[图片]"
    assert decode_content("audio", '{"file_key":"file_x"}').text == "[语音]"


def test_decode_file_keeps_its_name():
    assert decode_content("file", '{"file_key":"file_x","file_name":"会赢吗.html"}').text == (
        "[文件] 会赢吗.html"
    )


def test_resolve_mentions_uses_event_nicknames():
    content = json.dumps({"text": "@_user_1 请把图片交给 @_user_2"}, ensure_ascii=False)
    mentions = [
        SimpleNamespace(key="@_user_1", name="小王"),
        SimpleNamespace(key="@_user_2", name="小李"),
    ]

    resolved = resolve_mentions(content, mentions)
    assert decode_content("text", resolved).text == "@小王 请把图片交给 @小李"


def test_extract_merged_children_keeps_readable_content():
    items = [
        {"msg_type": "merge_forward", "body": {"content": "Merged"}},
        {
            "msg_type": "text",
            "sender": {"name": "小李"},
            "body": {"content": json.dumps({"text": "决定周五上线"}, ensure_ascii=False)},
        },
        {
            "msg_type": "image",
            "sender": {"name": "小王"},
            "body": {"content": json.dumps({"image_key": "img_x"})},
        },
    ]
    assert extract_merged_children(items).text == "小李：决定周五上线；小王：[图片]"
