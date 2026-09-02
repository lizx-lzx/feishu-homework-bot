from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config" / "base_schema.example.json"
VALIDATOR_PATH = ROOT / "scripts" / "validate_base_schema.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_base_schema", VALIDATOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_open_source_base_schema_is_valid() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = _load_validator()

    assert validator.validate_schema(schema) == []
    assert len(schema["fields"]) == 22
    assert len(schema["views"]) == 7


def test_manual_status_is_leader_owned_and_excluded_from_status_views() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    fields = {field["name"]: field for field in schema["fields"]}
    manual = fields["人工状态"]

    assert manual["managed_by"] == "leader"
    assert {option["name"] for option in manual["options"]} == {
        "正常提交",
        "补卡",
        "未提交",
        "不参与统计",
    }
    for view in schema["views"]:
        if view["name"] == "当前作业周期":
            continue
        assert ["人工状态", "disjoint", ["不参与统计"]] in view["filter"][
            "conditions"
        ]


def test_system_status_and_pending_view_include_verification_state() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    fields = {field["name"]: field for field in schema["fields"]}
    system = fields["作业状态"]
    pending_view = next(view for view in schema["views"] if view["name"] == "本周期待提交")

    assert {option["name"] for option in system["options"]} == {
        "已提交",
        "补卡",
        "待核验",
        "未提交",
    }
    assert ["作业状态", "intersects", ["待核验", "未提交"]] in pending_view[
        "filter"
    ]["conditions"]


def test_every_view_uses_current_cycle_instead_of_relative_dates() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    for view in schema["views"]:
        assert ["周期状态", "intersects", ["当前周期"]] in view["filter"][
            "conditions"
        ]
        assert "Today" not in json.dumps(view, ensure_ascii=False)
        assert "Yesterday" not in json.dumps(view, ensure_ascii=False)
