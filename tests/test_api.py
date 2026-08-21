import json

import httpx

from daily_report_bot.api import FeishuApi


def test_add_reaction_uses_message_reaction_api():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        if request.method == "POST" and request.url.path.endswith(
            "/im/v1/messages/om_test/reactions"
        ):
            assert json.loads(request.content) == {"reaction_type": {"emoji_type": "FINGERHEART"}}
            return httpx.Response(
                200,
                json={"code": 0, "data": {"reaction_id": "reaction_1"}},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    api = FeishuApi(
        "app",
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert api.add_reaction("om_test", "FINGERHEART") == "reaction_1"
    assert requests[-1].url.path == "/open-apis/im/v1/messages/om_test/reactions"


def test_base_update_resolves_field_names_to_ids_and_caches_schema():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        if request.method == "GET" and request.url.path.endswith("/fields"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "fields": [
                            {"id": "fld_name", "name": "组员姓名", "type": "text"},
                            {"id": "fld_status", "name": "作业状态", "type": "select"},
                        ]
                    },
                },
            )
        if request.method == "PATCH" and "/records/" in request.url.path:
            assert json.loads(request.content) in (
                {"fld_name": "成员甲", "fld_status": "已提交"},
                {"fld_status": "补卡"},
            )
            return httpx.Response(200, json={"code": 0, "data": {"update": {}}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    api = FeishuApi(
        "app",
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    api.update_base_record(
        "base",
        "table",
        "record-1",
        {"组员姓名": "成员甲", "作业状态": "已提交"},
    )
    api.update_base_record("base", "table", "record-1", {"作业状态": "补卡"})

    assert sum(request.method == "GET" for request in requests) == 1
