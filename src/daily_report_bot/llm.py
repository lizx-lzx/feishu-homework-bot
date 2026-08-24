from __future__ import annotations

import json
import re
from typing import Iterable, List, Optional

import httpx


SYSTEM_PROMPT = """你是进阶营作业群日报整理员。只根据提供的固定事实和群聊原文整理，
不补造事实，不改变代码已经计算出的名单、人数和作业完成状态。

你的输出只包含以下三个区块，不要重复标题、日期、今日总览、完成情况、未完成人员、链接或页脚：

📝 每日复盘（N 人）
先说明只统计报告日期对应的有效复盘。按固定事实中的复盘完成名单逐人编号整理；保留关键细节，
同一人有多条复盘时标注“（N 条）”并分条概括。没有有效复盘时写“无”。
复盘正文去掉开头的 #0817、#复盘、#0817复盘打卡等标签，不把标签抄进摘要。

💬 群内反馈
只写群友对他人作品或行动的明确反馈，格式为“反馈者 → 对象：内容”；没有就写“无”。
纯“好的/收到/get”、表情回复、催交、打卡规则、表格或问卷提醒都不算群内反馈。

🔍 方法与待解决
方法沉淀：只提炼原文中已经实际采用或明确总结的方法，并在括号中注明来源成员。
群管理流程、催交、飞书表格填写和打卡提交规则不算方法沉淀。
待解决问题：只写成员自己明确遇到的问题，并在括号中注明来源成员。
相应栏目没有内容就写“无”。

成员昵称必须原样使用；昵称“，”就只写一个中文逗号，不能添加“成员名”等解释。
不要输出“建议组长跟进”，不要虚构负责人，不识别图片内容。
输出纯文本，不要使用 Markdown 的 # 标题、**加粗**、- 列表或代码标记。"""


LEADER_COMMAND_PROMPT = """你是课程作业机器人的受限指令解析器。
你只判断这句话是否明确要求把若干群成员的某次作业状态改为正常提交、补卡或未提交。
不是这类代改指令，或目标人、状态不明确时，intent 必须为 unsupported。
目标成员只能从系统给出的名单原样选择，不得创造、改写或猜测名字。
“补了、补作业、补交、补提交、算补卡”的状态都是 late；“交了、完成了、算正常提交”是 completed；“没交、未完成”是 missing。
作业序号没有明说时返回 null，不要自己猜第几次。
只输出一个 JSON 对象，不要 Markdown、解释或其他文字：
{"intent":"leader_override|unsupported","targets":["成员名"],"status":"completed|late|missing|null","assignment_number":1,"confidence":0.0}
"""


QUERY_COMMAND_PROMPT = """你是课程作业机器人的受限查询解析器。
你只能识别作业/复盘统计查询和某个成员的全部打卡记录查询，不回答问题本身。
修改状态、闲聊、课程知识和目标不清晰的话，intent 必须为 unsupported。
目标成员只能从系统名单原样选择；没提到成员时 target 为 null。
“掉队、还差、没跟上、谁还没弄”都是 missing；“谁做了、谁完成了”是 completed；要整体分组或情况是 summary。
作业序号没有明说时返回 null。
只输出一个 JSON 对象，不要 Markdown、解释或其他文字：
{"intent":"attendance_query|member_history|unsupported","topic":"homework|review|null","mode":"summary|missing|completed|null","assignment_number":1,"target":"成员名|null","confidence":0.0}
"""


HOMEWORK_FEEDBACK_PROMPT = """你是课程作业助教。只根据成员主动提供的作业文字、作业说明和复盘做简短反馈。
不打分，不虚构作品画面、链接内容或成员没写的事实，不把通用鸡汤当反馈。
如果文字里信息不足，就明确说明只能根据已提供文字判断。
只输出下面三行，每行不超过80个中文字：
亮点：……
可继续打磨：……
下一步：……
"""


SOCIAL_CHAT_PROMPT = """你是课程群里一个有分寸的 AI 助教。被人直接 @ 时，你可以像正常助手一样回答普通问题；没有人 @ 时，只在确实有帮助价值时克制参与，不要为了存在感硬插话。

你只做决策，不执行统计、补卡、改表或群管理。必须遵守：
1. 有人明确 @ 你时，普通对话、机器人自身功能、群规则、作业/项目/学习以及其他低风险问题都应 reply，不能仅因为超出作业统计就选 silent。遇到敏感隐私、医疗法律金融或危险请求时，也用 reply 简短说明边界或安全做法，不要用 silent 代替回答。
2. 没有 @ 你时，成员明确说“怎么办/怎么做/卡住/报错/打不开/有没有办法”等求助信号，且内容属于课程、AI 工具或项目实操时，原则上选 reply。信息不足时可以只问一个最关键的追问；明显在问其他人时才 silent。
3. 没有 @ 你时，纯打卡、纯复盘、催交、人员状态、“好的/收到/哈哈”、别人之间的对话优先 silent。直接 @ 你时，简短闲聊可以 reply 或 react。
4. 出现“终于跑通/搞定/解决/成功了”这类小进展，且不是带作业标签的例行打卡时，优先选 react，不要完全沉默。
5. 不假装是真人，不编造亲身经历；不声称看过未提供内容的图片、网页或文件，不声称能实时联网、查天气或搜索资料。
6. reply 只写 1—3 句自然中文，最多 160 字，不用 Markdown 标题，不教训人，不重复群友刚说的话。
7. 消息明确在 @ 其他人、并不是向你求助时，选 silent。

只输出一个 JSON 对象，不要 Markdown 或解释：
{"action":"silent|react|reply","reply":"","emoji":"LOVE|FISTBUMP|LAUGH|FINGERHEART|THINKING|OnIt|WOW|Get|HIGHFIVE|PARTY|null","confidence":0.0}
"""


_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_EXPECTED_REVIEWS = re.compile(r"^有效复盘名单JSON：(?P<value>.+)$", re.MULTILINE)
_REQUIRED_SECTIONS = ("📝 每日复盘", "💬 群内反馈", "🔍 方法与待解决")
_REVIEW_HEADING = re.compile(r"📝\s*每日复盘（[^\n）]*人）")
_RAW_FEISHU_NAME = re.compile(r"(?:飞书用户|用户)[A-Za-z0-9]+")
_REPORT_TAG = re.compile(r"#\s*(?:\d{4}|\d{1,2}月\d{1,2}日|复盘)")
_SOCIAL_REACTIONS = {
    "LOVE",
    "FISTBUMP",
    "LAUGH",
    "FINGERHEART",
    "THINKING",
    "OnIt",
    "WOW",
    "Get",
    "HIGHFIVE",
    "PARTY",
}


def _clean_model_output(content: str) -> str:
    """Remove reasoning blocks that some compatible APIs put in message.content."""
    cleaned = _THINK_BLOCK.sub("", content)
    return cleaned.strip()


def _expected_review_names(report_context: str) -> List[str]:
    match = _EXPECTED_REVIEWS.search(report_context)
    if not match:
        return []
    try:
        value = json.loads(match.group("value"))
    except json.JSONDecodeError:
        return []
    return [str(name) for name in value] if isinstance(value, list) else []


def _summary_issues(content: str, report_context: str) -> List[str]:
    issues = [f"缺少区块：{section}" for section in _REQUIRED_SECTIONS if section not in content]
    if "建议组长跟进" in content:
        issues.append("不应输出“建议组长跟进”")
    if match := _RAW_FEISHU_NAME.search(content):
        issues.append(f"使用了未规范的飞书名称：{match.group(0)}")
    if match := _REPORT_TAG.search(content):
        issues.append(f"复盘摘要中保留了打卡标签：{match.group(0)}")
    for old_name in ("温宝", "清风明月", "JQ"):
        if old_name in content:
            issues.append(f"使用了旧昵称：{old_name}")
    expected = _expected_review_names(report_context)
    for name in expected:
        if name not in content:
            issues.append(f"漏掉有效复盘成员：{name}")
    if not expected and "每日复盘（0 人）" not in content:
        issues.append("无有效复盘时人数必须为 0")
    return issues


def _normalize_summary(content: str, report_context: str) -> str:
    """Make the model's review count agree with deterministic evidence."""
    count = len(_expected_review_names(report_context))
    return _REVIEW_HEADING.sub(f"📝 每日复盘（{count} 人）", content, count=1)


class Summarizer:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        max_chars_per_request: int = 50_000,
        timeout: float = 90.0,
        client: Optional[httpx.Client] = None,
    ):
        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.max_chars_per_request = max_chars_per_request
        self.client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def _complete(
        self,
        prompt: str,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        max_output_tokens: int = 6000,
        temperature: float = 0.2,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
        }
        if self.model == "MiniMax-M3":
            # M3 defaults to adaptive thinking. A short legacy max_tokens value
            # can be consumed entirely by <think>, leaving no report body.
            payload["thinking"] = {"type": "disabled"}
            payload["reasoning_split"] = True
            payload["max_completion_tokens"] = max_output_tokens
        else:
            payload["max_tokens"] = min(max_output_tokens, 2000)
        # DeepSeek V4 默认开启思考模式。日报是结构化摘要，关闭它可减少延迟和费用。
        if self.model.startswith("deepseek-v4-"):
            payload["thinking"] = {"type": "disabled"}

        response = self.client.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("模型没有返回总结内容")
        content = choices[0].get("message", {}).get("content")
        if not content:
            raise RuntimeError("模型返回了空总结")
        cleaned = _clean_model_output(str(content))
        if not cleaned:
            raise RuntimeError("模型只返回了思考过程，没有可用总结")
        return cleaned

    def interpret_leader_override(
        self,
        command: str,
        roster: Iterable[str],
    ) -> Optional[dict]:
        """把自然语言组长代改指令收敛成可校验的小型 JSON。

        模型只有解析权；目标名单、可用状态和置信度在这里再做一次
        确定性校验，真正写表仍由 service 层完成。
        """
        allowed_names = tuple(dict.fromkeys(str(name) for name in roster))
        raw = self._complete(
            "群成员名单JSON："
            + json.dumps(allowed_names, ensure_ascii=False)
            + "\n待解析指令："
            + command,
            system_prompt=LEADER_COMMAND_PROMPT,
            max_output_tokens=500,
            temperature=0.0,
        )
        candidate = raw.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict) or parsed.get("intent") != "leader_override":
            return None
        confidence = parsed.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return None
        if float(confidence) < 0.9:
            return None
        status = parsed.get("status")
        if status not in {"completed", "late", "missing"}:
            return None
        targets = parsed.get("targets")
        if not isinstance(targets, list) or not targets:
            return None
        normalized_targets = tuple(dict.fromkeys(str(name) for name in targets))
        if any(name not in allowed_names for name in normalized_targets):
            return None
        assignment_number = parsed.get("assignment_number")
        if assignment_number is not None and (
            isinstance(assignment_number, bool)
            or not isinstance(assignment_number, int)
            or assignment_number < 1
        ):
            return None
        return {
            "targets": normalized_targets,
            "status": status,
            "assignment_number": assignment_number,
            "confidence": float(confidence),
        }

    def interpret_query(self, command: str, roster: Iterable[str]) -> Optional[dict]:
        """把课程统计自然语言收敛成只读查询 JSON。"""
        allowed_names = tuple(dict.fromkeys(str(name) for name in roster))
        raw = self._complete(
            "群成员名单JSON："
            + json.dumps(allowed_names, ensure_ascii=False)
            + "\n待解析问题："
            + command,
            system_prompt=QUERY_COMMAND_PROMPT,
            max_output_tokens=500,
            temperature=0.0,
        )
        candidate = raw.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        intent = parsed.get("intent")
        if intent not in {"attendance_query", "member_history"}:
            return None
        confidence = parsed.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return None
        if float(confidence) < 0.9:
            return None
        assignment_number = parsed.get("assignment_number")
        if assignment_number is not None and (
            isinstance(assignment_number, bool)
            or not isinstance(assignment_number, int)
            or assignment_number < 1
        ):
            return None
        target = parsed.get("target")
        if target is not None and target not in allowed_names:
            return None
        if intent == "member_history" and target is None:
            return None
        topic = parsed.get("topic")
        mode = parsed.get("mode")
        if intent == "attendance_query":
            if topic not in {"homework", "review"}:
                return None
            if mode not in {"summary", "missing", "completed"}:
                return None
        return {
            "intent": intent,
            "topic": topic,
            "mode": mode,
            "assignment_number": assignment_number,
            "target": target,
            "confidence": float(confidence),
        }

    def feedback_homework(self, member_name: str, homework_text: str) -> str:
        """仅在成员显式 #求反馈 时生成三行受限点评。"""
        result = self._complete(
            f"成员：{member_name}\n作业文字：\n{homework_text}",
            system_prompt=HOMEWORK_FEEDBACK_PROMPT,
            max_output_tokens=800,
            temperature=0.2,
        )
        lines = [line.strip() for line in result.splitlines() if line.strip()]
        expected = ("亮点：", "可继续打磨：", "下一步：")
        if len(lines) != 3 or any(
            not line.startswith(prefix) for line, prefix in zip(lines, expected)
        ):
            raise RuntimeError("MiniMax 作业反馈格式校验失败")
        return "\n".join(lines)

    def decide_social_response(
        self,
        member_name: str,
        message_text: str,
        context_lines: Iterable[str],
        *,
        direct: bool,
    ) -> Optional[dict]:
        """决定群助教应当沉默、表情回应还是简短接话。"""
        context = "\n".join(list(context_lines)[-12:])
        raw = self._complete(
            f"是否直接 @ 助教：{'yes' if direct else 'no'}\n"
            f"当前发言人：{member_name}\n"
            f"当前消息：{message_text}\n"
            f"最近群聊（只作上下文）：\n{context}",
            system_prompt=SOCIAL_CHAT_PROMPT,
            max_output_tokens=600,
            temperature=0.35,
        )
        candidate = raw.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        action = parsed.get("action")
        if action not in {"silent", "react", "reply"}:
            return None
        confidence = parsed.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return None
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            return None
        reply = str(parsed.get("reply") or "").strip()
        emoji = parsed.get("emoji")
        if action == "reply":
            if not reply or len(reply) > 200:
                return None
            emoji = None
        elif action == "react":
            if emoji not in _SOCIAL_REACTIONS:
                return None
            reply = ""
        else:
            reply = ""
            emoji = None
        return {
            "action": action,
            "reply": reply,
            "emoji": emoji,
            "confidence": confidence,
        }

    def _chunks(self, lines: Iterable[str]) -> List[str]:
        chunks: List[str] = []
        current: List[str] = []
        current_size = 0
        for line in lines:
            size = len(line) + 1
            if current and current_size + size > self.max_chars_per_request:
                chunks.append("\n".join(current))
                current = []
                current_size = 0
            current.append(line[: self.max_chars_per_request])
            current_size += min(size, self.max_chars_per_request)
        if current:
            chunks.append("\n".join(current))
        return chunks

    def summarize(
        self,
        report_date: str,
        transcript_lines: Iterable[str],
        *,
        report_context: str = "",
    ) -> str:
        chunks = self._chunks(transcript_lines)
        if not chunks:
            return ""
        context = f"\n\n固定事实：\n{report_context}" if report_context else ""
        partials = [
            self._complete(f"日期：{report_date}{context}\n\n以下是群聊原文：\n\n{chunk}")
            for chunk in chunks
        ]
        if len(partials) == 1:
            result = partials[0]
        else:
            combined = "\n\n--- 分段总结 ---\n".join(partials)
            result = self._complete(
                f"日期：{report_date}{context}\n请把以下分段总结合并成最终三个区块，"
                f"去重并保留事实：\n\n{combined}"
            )
        if self.model != "MiniMax-M3":
            return result
        result = _normalize_summary(result, report_context)
        issues = _summary_issues(result, report_context)
        if not issues:
            return result
        repaired = self._complete(
            f"日期：{report_date}{context}\n\n"
            "上一版未通过系统校验，请只修正下列问题，不得改动固定事实：\n"
            + "\n".join(f"- {issue}" for issue in issues)
            + f"\n\n上一版：\n{result}"
        )
        repaired = _normalize_summary(repaired, report_context)
        repaired_issues = _summary_issues(repaired, report_context)
        if repaired_issues:
            raise RuntimeError("模型总结校验失败：" + "；".join(repaired_issues))
        return repaired

    def probe(self) -> str:
        return self._complete("这是连通性测试。请只回复：模型连通正常")
