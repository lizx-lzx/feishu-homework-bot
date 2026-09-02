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


def test_decode_post_does_not_duplicate_content_v2():
    body = [[{"tag": "text", "text": "第4次作业已完成"}], [{"tag": "img"}]]
    content = json.dumps({"content": body, "content_v2": body}, ensure_ascii=False)

    assert decode_content("post", content).text == "第4次作业已完成 [图片]"


def test_decode_post_keeps_embedded_video_and_file_placeholders():
    content = json.dumps(
        {
            "zh_cn": {
                "content": [
                    [{"tag": "text", "text": "第7次作业已完成"}],
                    [{"tag": "media", "file_key": "file_video"}],
                    [{"tag": "file", "file_key": "file_doc"}],
                ]
            }
        },
        ensure_ascii=False,
    )

    assert decode_content("post", content).text == "第7次作业已完成 [视频] [文件]"


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


def test_extract_merged_children_marks_multiple_senders():
    items = [
        {"msg_type": "merge_forward", "sender": {"id": "ou_outer"}},
        {
            "msg_type": "text",
            "sender": {"id": "ou_outer", "name": "小李"},
            "body": {"content": json.dumps({"text": "我的作业"}, ensure_ascii=False)},
        },
        {
            "msg_type": "text",
            "sender": {"id": "ou_other", "name": "小王"},
            "body": {"content": json.dumps({"text": "收到"}, ensure_ascii=False)},
        },
    ]

    parsed = extract_merged_children(items, outer_sender_id="ou_outer")

    assert parsed.text == "[多人合并转发] 小李：我的作业；小王：收到"
