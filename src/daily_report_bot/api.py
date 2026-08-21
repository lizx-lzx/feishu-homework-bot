from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import httpx

from .report import build_post_content


class FeishuApiError(RuntimeError):
    def __init__(self, operation: str, code: Any, message: str):
        super().__init__(f"{operation} 失败（{code}）：{message}")
        self.operation = operation
        self.code = code
        self.message = message


class FeishuApi:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str = "https://open.feishu.cn/open-apis",
        timeout: float = 30.0,
        client: Optional[httpx.Client] = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)
        self._access_token = ""
        self._token_expire_at = 0.0
        self._base_field_ids: Dict[Tuple[str, str], Dict[str, str]] = {}

    def close(self) -> None:
        self.client.close()

    def _token(self, force: bool = False) -> str:
        if not force and self._access_token and time.time() < self._token_expire_at - 120:
            return self._access_token
        response = self.client.post(
            f"{self.base_url}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise FeishuApiError(
                "获取 tenant_access_token", payload.get("code"), payload.get("msg", "")
            )
        token = payload.get("tenant_access_token")
        if not token:
            raise FeishuApiError("获取 tenant_access_token", "missing_token", "响应中没有 token")
        self._access_token = str(token)
        self._token_expire_at = time.time() + int(payload.get("expire", 7200))
        return self._access_token

    def _json_request(
        self,
        method: str,
        path: str,
        operation: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        retry_codes = {1061045, 99991400}
        for attempt in range(retries):
            response = self.client.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {self._token()}"},
            )
            if response.status_code == 401 and attempt == 0:
                self._token(force=True)
                continue
            response.raise_for_status()
            payload = response.json()
            code = payload.get("code", 0)
            if code == 0:
                return payload
            if code in retry_codes and attempt + 1 < retries:
                time.sleep(0.5 * (2**attempt))
                continue
            raise FeishuApiError(operation, code, str(payload.get("msg", "")))
        raise FeishuApiError(operation, "retry_exhausted", "重试次数已用完")

    def check_bot(self) -> str:
        payload = self._json_request("GET", "/bot/v3/info", "获取机器人信息")
        bot = payload.get("bot") or payload.get("data", {}).get("bot") or {}
        return str(bot.get("app_name") or bot.get("bot_name") or self.app_id)

    def reply_text(self, message_id: str, text: str, uuid: str) -> str:
        payload = self._json_request(
            "POST",
            f"/im/v1/messages/{quote(message_id, safe='')}/reply",
            "回复消息",
            body={
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "msg_type": "text",
                "uuid": uuid[:50],
            },
        )
        return str(payload.get("data", {}).get("message_id", ""))

    def reply_post(self, message_id: str, text: str, uuid: str) -> str:
        payload = self._json_request(
            "POST",
            f"/im/v1/messages/{quote(message_id, safe='')}/reply",
            "回复富文本消息",
            body={
                "content": json.dumps(build_post_content(text), ensure_ascii=False),
                "msg_type": "post",
                "uuid": uuid[:50],
            },
        )
        return str(payload.get("data", {}).get("message_id", ""))

    def add_reaction(self, message_id: str, emoji_type: str) -> str:
        payload = self._json_request(
            "POST",
            f"/im/v1/messages/{quote(message_id, safe='')}/reactions",
            "添加消息表情回复",
            body={"reaction_type": {"emoji_type": emoji_type}},
        )
        return str(payload.get("data", {}).get("reaction_id", ""))

    def send_text(self, chat_id: str, text: str, uuid: str) -> str:
        payload = self._json_request(
            "POST",
            "/im/v1/messages",
            "发送群消息",
            params={"receive_id_type": "chat_id"},
            body={
                "receive_id": chat_id,
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "msg_type": "text",
                "uuid": uuid[:50],
            },
        )
        return str(payload.get("data", {}).get("message_id", ""))

    def send_post(self, chat_id: str, text: str, uuid: str) -> str:
        payload = self._json_request(
            "POST",
            "/im/v1/messages",
            "发送群富文本消息",
            params={"receive_id_type": "chat_id"},
            body={
                "receive_id": chat_id,
                "content": json.dumps(build_post_content(text), ensure_ascii=False),
                "msg_type": "post",
                "uuid": uuid[:50],
            },
        )
        return str(payload.get("data", {}).get("message_id", ""))

    def send_attendance_reminder(
        self,
        chat_id: str,
        members: Sequence[Tuple[str, str]],
        homework_missing: Sequence[str],
        review_missing: Sequence[str],
        uuid: str,
    ) -> str:
        mention_nodes: List[Dict[str, Any]] = []
        for index, (open_id, name) in enumerate(members):
            if index:
                mention_nodes.append({"tag": "text", "text": "、"})
            mention_nodes.append({"tag": "at", "user_id": open_id, "user_name": name})
        content = [
            mention_nodes or [{"tag": "text", "text": "无"}],
            [
                {
                    "tag": "text",
                    "text": f"作业未交（{len(homework_missing)}人）：{'、'.join(homework_missing) or '无'}",
                }
            ],
            [{"tag": "text", "text": "请在今天 20:00 作业截止前完成打卡。"}],
        ]
        payload = self._json_request(
            "POST",
            "/im/v1/messages",
            "发送打卡提醒",
            params={"receive_id_type": "chat_id"},
            body={
                "receive_id": chat_id,
                "content": json.dumps(
                    {"zh_cn": {"title": "作业催交提醒", "content": content}},
                    ensure_ascii=False,
                ),
                "msg_type": "post",
                "uuid": uuid[:50],
            },
        )
        return str(payload.get("data", {}).get("message_id", ""))

    def send_makeup_reminder(
        self,
        chat_id: str,
        members: Sequence[Tuple[str, str]],
        homework_missing: Sequence[str],
        uuid: str,
    ) -> str:
        mention_nodes: List[Dict[str, Any]] = []
        for index, (open_id, name) in enumerate(members):
            if index:
                mention_nodes.append({"tag": "text", "text": "、"})
            mention_nodes.append({"tag": "at", "user_id": open_id, "user_name": name})
        content = [
            mention_nodes or [{"tag": "text", "text": "无"}],
            [
                {
                    "tag": "text",
                    "text": f"仍未补交（{len(homework_missing)}人）：{'、'.join(homework_missing) or '无'}",
                }
            ],
            [{"tag": "text", "text": "请在今天 20:00 补交截止前完成打卡。"}],
        ]
        payload = self._json_request(
            "POST",
            "/im/v1/messages",
            "发送补交提醒",
            params={"receive_id_type": "chat_id"},
            body={
                "receive_id": chat_id,
                "content": json.dumps(
                    {"zh_cn": {"title": "作业补交提醒", "content": content}},
                    ensure_ascii=False,
                ),
                "msg_type": "post",
                "uuid": uuid[:50],
            },
        )
        return str(payload.get("data", {}).get("message_id", ""))

    def list_base_records(self, base_token: str, table_id: str) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        offset = 0
        while True:
            payload = self._json_request(
                "GET",
                f"/base/v3/bases/{quote(base_token, safe='')}/tables/{quote(table_id, safe='')}/records",
                "读取多维表格记录",
                params={"offset": offset, "limit": 200},
            )
            data = payload.get("data", {})
            fields = list(data.get("fields") or [])
            rows = list(data.get("data") or [])
            record_ids = list(data.get("record_id_list") or [])
            for index, row in enumerate(rows):
                records.append(
                    {
                        "record_id": str(record_ids[index]) if index < len(record_ids) else "",
                        "fields": dict(zip(fields, row)),
                    }
                )
            if not data.get("has_more") or not rows:
                break
            offset += len(rows)
        return records

    def _base_field_id_map(self, base_token: str, table_id: str) -> Dict[str, str]:
        """Return field-name to field-id mapping used by Base v3 PATCH.

        Base v3 accepts field names when creating records, but PATCH can return
        code=0 while silently ignoring name-keyed fields. Resolving names to
        field IDs keeps later attendance/status updates observable and reliable.
        """
        cache_key = (base_token, table_id)
        cached = self._base_field_ids.get(cache_key)
        if cached is not None:
            return cached
        payload = self._json_request(
            "GET",
            (
                f"/base/v3/bases/{quote(base_token, safe='')}/tables/"
                f"{quote(table_id, safe='')}/fields"
            ),
            "读取多维表格字段",
        )
        fields = payload.get("data", {}).get("fields") or []
        mapping = {
            str(field.get("name")): str(field.get("id"))
            for field in fields
            if field.get("name") and field.get("id")
        }
        self._base_field_ids[cache_key] = mapping
        return mapping

    def create_base_record(self, base_token: str, table_id: str, fields: Dict[str, Any]) -> str:
        payload = self._json_request(
            "POST",
            f"/base/v3/bases/{quote(base_token, safe='')}/tables/{quote(table_id, safe='')}/records",
            "创建多维表格记录",
            body=fields,
        )
        data = payload.get("data", {})
        record = data.get("record") or {}
        record_ids = record.get("record_id_list") or data.get("record_id_list") or []
        return str(
            record.get("record_id")
            or record.get("id")
            or data.get("record_id")
            or data.get("id")
            or (record_ids[0] if record_ids else "")
            or ""
        )

    def update_base_record(
        self, base_token: str, table_id: str, record_id: str, fields: Dict[str, Any]
    ) -> None:
        field_ids = self._base_field_id_map(base_token, table_id)
        patch = {field_ids.get(key, key): value for key, value in fields.items()}
        self._json_request(
            "PATCH",
            (
                f"/base/v3/bases/{quote(base_token, safe='')}/tables/"
                f"{quote(table_id, safe='')}/records/{quote(record_id, safe='')}"
            ),
            "更新多维表格记录",
            body=patch,
        )

    def delete_base_record(self, base_token: str, table_id: str, record_id: str) -> None:
        self._json_request(
            "DELETE",
            (
                f"/base/v3/bases/{quote(base_token, safe='')}/tables/"
                f"{quote(table_id, safe='')}/records/{quote(record_id, safe='')}"
            ),
            "删除多维表格记录",
        )

    def get_message_items(self, message_id: str) -> List[Dict[str, Any]]:
        payload = self._json_request(
            "GET",
            f"/im/v1/messages/{quote(message_id, safe='')}",
            "获取消息内容",
        )
        return list(payload.get("data", {}).get("items") or [])

    def get_member_name(self, chat_id: str, open_id: str) -> str:
        page_token = ""
        while True:
            params: Dict[str, Any] = {"member_id_type": "open_id", "page_size": 100}
            if page_token:
                params["page_token"] = page_token
            payload = self._json_request(
                "GET",
                f"/im/v1/chats/{quote(chat_id, safe='')}/members",
                "获取群成员",
                params=params,
            )
            data = payload.get("data", {})
            for item in data.get("items") or []:
                if item.get("member_id") == open_id:
                    return str(item.get("name") or "")
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                break
        return ""
