from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "config" / "base_schema.example.json"

REQUIRED_FIELDS = {
    "记录键",
    "日期",
    "作业序号",
    "作业周期",
    "周期状态",
    "作业名称",
    "组员姓名",
    "飞书OpenID",
    "人工状态",
    "作业状态",
    "复盘状态",
    "迭代状态",
    "迭代发起人",
    "提交时间",
    "复盘时间",
    "迭代时间",
    "作业证据消息ID",
    "复盘证据消息ID",
    "正常提交累计",
    "补卡累计",
    "旷卡累计",
    "最后同步时间",
}
REQUIRED_VIEWS = {
    "当前作业周期",
    "本周期已提交",
    "本周期待提交",
    "补卡成员",
    "本周期已复盘",
    "待迭代",
    "已迭代",
}
ALLOWED_FIELD_TYPES = {"text", "number", "select", "datetime"}
ALLOWED_VIEW_OPERATORS = {
    "==",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "intersects",
    "disjoint",
    "empty",
    "non_empty",
}


def _duplicates(values: Iterable[str]) -> Set[str]:
    seen: Set[str] = set()
    duplicates: Set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _field_names(schema: Dict[str, Any]) -> List[str]:
    return [str(field.get("name", "")) for field in schema.get("fields", [])]


def validate_schema(schema: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if schema.get("schema_version") != 1:
        errors.append("schema_version 必须为 1")

    table = schema.get("table")
    if not isinstance(table, dict):
        errors.append("缺少 table 对象")
        table = {}

    fields = schema.get("fields")
    if not isinstance(fields, list):
        errors.append("缺少 fields 数组")
        fields = []
    names = _field_names(schema)
    duplicate_fields = _duplicates(names)
    if duplicate_fields:
        errors.append(f"字段重复：{sorted(duplicate_fields)}")
    missing_fields = REQUIRED_FIELDS - set(names)
    extra_fields = set(names) - REQUIRED_FIELDS
    if missing_fields:
        errors.append(f"缺少运行时字段：{sorted(missing_fields)}")
    if extra_fields:
        errors.append(f"Schema 包含未审核字段：{sorted(extra_fields)}")
    if table.get("primary_field") != "记录键":
        errors.append("主字段必须是“记录键”")

    fields_by_name = {str(field.get("name", "")): field for field in fields}
    for field_name, field in fields_by_name.items():
        if field.get("type") not in ALLOWED_FIELD_TYPES:
            errors.append(f"字段 {field_name} 类型不支持：{field.get('type')}")
        if field.get("managed_by") not in {"bot", "leader"}:
            errors.append(f"字段 {field_name} 缺少合法 managed_by")
        if field.get("type") == "select":
            options = field.get("options")
            if not isinstance(options, list) or not options:
                errors.append(f"单选字段 {field_name} 必须定义 options")
                continue
            option_names = [str(option.get("name", "")) for option in options]
            if _duplicates(option_names):
                errors.append(f"单选字段 {field_name} 存在重复选项")

    manual = fields_by_name.get("人工状态", {})
    manual_options = {str(option.get("name", "")) for option in manual.get("options", [])}
    expected_manual = {"正常提交", "补卡", "未提交", "不参与统计"}
    if manual.get("managed_by") != "leader" or manual_options != expected_manual:
        errors.append("人工状态必须由组长维护并包含四个标准选项")

    system = fields_by_name.get("作业状态", {})
    system_options = {str(option.get("name", "")) for option in system.get("options", [])}
    expected_system = {"已提交", "补卡", "待核验", "未提交"}
    if system.get("managed_by") != "bot" or system_options != expected_system:
        errors.append("作业状态必须由机器人维护并包含四个标准选项")

    views = schema.get("views")
    if not isinstance(views, list):
        errors.append("缺少 views 数组")
        views = []
    view_names = [str(view.get("name", "")) for view in views]
    duplicate_views = _duplicates(view_names)
    if duplicate_views:
        errors.append(f"视图重复：{sorted(duplicate_views)}")
    if set(view_names) != REQUIRED_VIEWS:
        errors.append(
            f"视图集合不完整：缺少 {sorted(REQUIRED_VIEWS - set(view_names))}，"
            f"多出 {sorted(set(view_names) - REQUIRED_VIEWS)}"
        )

    known_fields = set(names)
    for view in views:
        view_name = str(view.get("name", ""))
        if view.get("type") != "grid":
            errors.append(f"视图 {view_name} 必须是 grid")
        filter_config = view.get("filter")
        if not isinstance(filter_config, dict):
            errors.append(f"视图 {view_name} 缺少 filter")
            continue
        if filter_config.get("logic") not in {"and", "or"}:
            errors.append(f"视图 {view_name} filter.logic 不合法")
        conditions = filter_config.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            errors.append(f"视图 {view_name} 必须有筛选条件")
            conditions = []
        for condition in conditions:
            if not isinstance(condition, list) or len(condition) not in {2, 3}:
                errors.append(f"视图 {view_name} 存在非法筛选 tuple")
                continue
            field_name = str(condition[0])
            operator = str(condition[1])
            if field_name not in known_fields:
                errors.append(f"视图 {view_name} 引用未定义字段 {field_name}")
            if operator not in ALLOWED_VIEW_OPERATORS:
                errors.append(f"视图 {view_name} 使用非法 operator {operator}")

        sort_config = view.get("sort", {}).get("sort_config", [])
        if not isinstance(sort_config, list):
            errors.append(f"视图 {view_name} sort_config 必须是数组")
            sort_config = []
        for sort_item in sort_config:
            if sort_item.get("field") not in known_fields:
                errors.append(f"视图 {view_name} 排序引用未定义字段")
            if not isinstance(sort_item.get("desc"), bool):
                errors.append(f"视图 {view_name} 排序 desc 必须是布尔值")

        visible_fields = view.get("visible_fields")
        if not isinstance(visible_fields, list) or not visible_fields:
            errors.append(f"视图 {view_name} 缺少 visible_fields")
            visible_fields = []
        unknown_visible = set(visible_fields) - known_fields
        if unknown_visible:
            errors.append(f"视图 {view_name} 展示未定义字段：{sorted(unknown_visible)}")

    pending_view = next((view for view in views if view.get("name") == "本周期待提交"), {})
    pending_conditions = pending_view.get("filter", {}).get("conditions", [])
    if ["作业状态", "intersects", ["待核验", "未提交"]] not in pending_conditions:
        errors.append("本周期待提交视图必须同时包含待核验和未提交")

    override = schema.get("manual_override", {})
    if override.get("field") != "人工状态":
        errors.append("manual_override.field 必须是“人工状态”")
    if override.get("excluded_value") != "不参与统计":
        errors.append("manual_override.excluded_value 不合法")
    return errors


def main() -> int:
    schema = json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
    errors = validate_schema(schema)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        f"Base Schema 校验通过：{len(schema['fields'])} 个字段，"
        f"{len(schema['views'])} 个视图。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
