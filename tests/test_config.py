import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from daily_report_bot.config import (
    DEFAULT_EXCLUDED_MEMBER_IDS,
    DEFAULT_MEMBER_ALIASES,
    ConfigurationError,
    load_settings,
)


def test_summary_commands_are_loaded_from_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('SUMMARY_COMMANDS="打开日报,#总结,#今日总结"\n', encoding="utf-8")
    monkeypatch.delenv("SUMMARY_COMMANDS", raising=False)
    monkeypatch.delenv("SUMMARY_HOUR", raising=False)
    settings = load_settings(str(env_file))
    assert settings.summary_commands == ("打开日报", "#总结", "#今日总结")
    assert settings.summary_hour == 23
    assert settings.send_enabled is True
    assert settings.homework_reaction_enabled is False
    assert settings.reminder_hour == 17
    assert settings.missing_list_enabled is True
    assert settings.missing_list_hour == 20
    assert settings.makeup_reminder_enabled is True
    assert settings.makeup_reminder_hour == 11
    assert settings.makeup_deadline_hour == 12
    assert settings.makeup_summary_enabled is True
    assert settings.makeup_summary_hour == 12


def test_send_can_be_disabled_from_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("SUMMARY_SEND_ENABLED=false\n", encoding="utf-8")
    monkeypatch.delenv("SUMMARY_SEND_ENABLED", raising=False)
    assert load_settings(str(env_file)).send_enabled is False


def test_homework_reaction_can_be_enabled_from_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("HOMEWORK_REACTION_ENABLED=true\n", encoding="utf-8")
    monkeypatch.delenv("HOMEWORK_REACTION_ENABLED", raising=False)
    assert load_settings(str(env_file)).homework_reaction_enabled is True


def test_course_end_date_is_loaded_from_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ASSIGNMENT_CYCLE_START_DATE=2026-08-17\nCOURSE_END_DATE=2026-08-26\n",
        encoding="utf-8",
    )
    for name in ("ASSIGNMENT_CYCLE_START_DATE", "COURSE_END_DATE"):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(str(env_file))

    assert settings.assignment_cycle_start_date == "2026-08-17"
    assert settings.course_end_date == "2026-08-26"


def test_multiple_course_phases_are_loaded_from_dotenv(tmp_path, monkeypatch):
    phases = [
        {
            "name": "技术周",
            "start_date": "2026-08-17",
            "end_date": "2026-08-26",
            "cycle_days": 2,
            "publish_hour": 10,
            "due_hour": 20,
        },
        {
            "name": "视频周",
            "start_date": "2026-08-28",
            "end_date": "",
            "cycle_days": 1,
            "publish_hour": 8,
            "due_hour": 20,
        },
    ]
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("BASE_SYNC_ENABLED", "false")
    monkeypatch.setenv("COURSE_PHASES_JSON", json.dumps(phases, ensure_ascii=False))

    settings = load_settings(str(env_file))
    settings.validate(require_secrets=False)

    assert [phase.name for phase in settings.course_phases] == ["技术周", "视频周"]
    assert settings.course_phases[0].cycle_days == 2
    assert settings.course_phases[1].publish_hour == 8
    assert settings.course_phases[1].end_day is None


@pytest.mark.parametrize(
    "phases, error",
    [
        (
            [
                {"name": "A", "start_date": "2026-08-17", "end_date": ""},
                {"name": "B", "start_date": "2026-08-28", "end_date": ""},
            ],
            "无结束日的课程阶段必须放在最后",
        ),
        (
            [
                {"name": "A", "start_date": "2026-08-17", "end_date": "2026-08-26"},
                {"name": "B", "start_date": "2026-08-26", "end_date": "2026-08-30"},
            ],
            "课程阶段的日期不能重叠",
        ),
    ],
)
def test_invalid_course_phases_are_rejected(tmp_path, monkeypatch, phases, error):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("BASE_SYNC_ENABLED", "false")
    monkeypatch.setenv("COURSE_PHASES_JSON", json.dumps(phases, ensure_ascii=False))

    settings = load_settings(str(env_file))

    with pytest.raises(ConfigurationError, match=error):
        settings.validate(require_secrets=False)


def test_course_end_date_must_not_precede_course_start(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FEISHU_APP_ID=cli_test\n"
        "BASE_SYNC_ENABLED=false\n"
        "ASSIGNMENT_CYCLE_START_DATE=2026-08-17\n"
        "COURSE_END_DATE=2026-08-16\n",
        encoding="utf-8",
    )
    for name in (
        "FEISHU_APP_ID",
        "BASE_SYNC_ENABLED",
        "ASSIGNMENT_CYCLE_START_DATE",
        "COURSE_END_DATE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(str(env_file))

    with pytest.raises(ConfigurationError, match="COURSE_END_DATE 不能早于"):
        settings.validate(require_secrets=False)


def test_minimax_is_the_default_summary_provider(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    for name in (
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "MINIMAX_API_KEY",
        "DEEPSEEK_API_KEY",
        "INFO_RADAR_AI_BASE_URL",
        "INFO_RADAR_AI_API_KEY",
        "INFO_RADAR_AI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(str(env_file))

    assert settings.llm_base_url == "https://api.minimaxi.com/v1"
    assert settings.llm_model == "MiniMax-M3"


def test_default_member_aliases_and_excluded_ids_are_loaded(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    for name in ("MEMBER_ALIASES_JSON", "EXCLUDED_MEMBER_IDS"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings(str(env_file))
    assert settings.member_aliases == DEFAULT_MEMBER_ALIASES
    assert settings.member_aliases == {}
    assert settings.report_members == ()
    assert set(settings.excluded_member_ids) == set(DEFAULT_EXCLUDED_MEMBER_IDS)


def test_env_member_aliases_override_defaults(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.delenv("MEMBER_ALIASES_JSON", raising=False)
    monkeypatch.delenv("EXCLUDED_MEMBER_IDS", raising=False)
    monkeypatch.setenv(
        "MEMBER_ALIASES_JSON",
        json.dumps({"ou_existing": "新昵称", "ou_new": "新人"}),
    )
    settings = load_settings(str(env_file))
    assert settings.member_aliases["ou_existing"] == "新昵称"
    assert settings.member_aliases["ou_new"] == "新人"


def test_env_excluded_member_ids_append_to_defaults(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.delenv("MEMBER_ALIASES_JSON", raising=False)
    monkeypatch.delenv("EXCLUDED_MEMBER_IDS", raising=False)
    monkeypatch.setenv("EXCLUDED_MEMBER_IDS", "ou_extra1,ou_extra2")
    settings = load_settings(str(env_file))
    assert "ou_extra1" in settings.excluded_member_ids
    assert "ou_extra2" in settings.excluded_member_ids
    assert set(settings.excluded_member_ids) == {"ou_extra1", "ou_extra2"}


def test_invalid_member_aliases_json_raises_configuration_error(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.delenv("MEMBER_ALIASES_JSON", raising=False)
    monkeypatch.delenv("EXCLUDED_MEMBER_IDS", raising=False)
    monkeypatch.setenv("MEMBER_ALIASES_JSON", "{invalid json}")
    with pytest.raises(ConfigurationError, match="MEMBER_ALIASES_JSON 不是合法 JSON"):
        load_settings(str(env_file))


def test_assignment_deadline_override_uses_summary_timezone(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'ASSIGNMENT_DEADLINE_OVERRIDES_JSON={"2026-08-17":"2026-08-18 14:00"}\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("ASSIGNMENT_DEADLINE_OVERRIDES_JSON", raising=False)

    settings = load_settings(str(env_file))

    assert settings.assignment_deadline("2026-08-17").isoformat() == ("2026-08-18T14:00:00+08:00")
    assert settings.assignment_deadline("2026-08-19").isoformat() == ("2026-08-19T20:00:00+08:00")


def test_makeup_window_ends_strictly_before_next_day_noon(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    for name in ("MAKEUP_DEADLINE_HOUR", "MAKEUP_DEADLINE_MINUTE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "COURSE_PHASES_JSON",
        json.dumps(
            [
                {
                    "name": "视频周",
                    "start_date": "2026-08-28",
                    "end_date": "",
                    "cycle_days": 1,
                    "publish_hour": 8,
                    "due_hour": 20,
                }
            ],
            ensure_ascii=False,
        ),
    )
    settings = load_settings(str(env_file))
    tz = ZoneInfo("Asia/Shanghai")

    assert settings.makeup_deadline("2026-08-28").isoformat() == (
        "2026-08-29T12:00:00+08:00"
    )
    assert settings.is_makeup_submission(
        "2026-08-28", datetime(2026, 8, 28, 20, 0, 1, tzinfo=tz)
    )
    assert settings.is_makeup_submission(
        "2026-08-28", datetime(2026, 8, 29, 11, 59, 59, tzinfo=tz)
    )
    assert not settings.is_makeup_submission(
        "2026-08-28", datetime(2026, 8, 29, 12, 0, tzinfo=tz)
    )


def test_group_databases_and_capture_only_chats_are_loaded(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'GROUP_DATABASES_JSON={"oc_27":"./data/group_27.sqlite3","oc_24":"./data/group_24.sqlite3"}',
                "CAPTURE_ONLY_CHAT_IDS=oc_24",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("GROUP_DATABASES_JSON", raising=False)
    monkeypatch.delenv("CAPTURE_ONLY_CHAT_IDS", raising=False)

    settings = load_settings(str(env_file))

    assert settings.group_databases == {
        "oc_27": "./data/group_27.sqlite3",
        "oc_24": "./data/group_24.sqlite3",
    }
    assert settings.capture_only_chat_ids == ("oc_24",)


def test_group_profiles_can_be_loaded_from_file(tmp_path, monkeypatch):
    profile_path = tmp_path / "groups.json"
    profile_path.write_text(
        json.dumps(
            {
                "oc_24": {
                    "report_members": ["李"],
                    "member_aliases": {"ou_li": "李"},
                    "leader_member_ids": ["ou_leader"],
                    "send_enabled": False,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'GROUP_DATABASES_JSON={"oc_24":"./data/group_24.sqlite3"}',
                f"GROUP_PROFILES_PATH={profile_path}",
            ]
        ),
        encoding="utf-8",
    )
    for name in ("GROUP_DATABASES_JSON", "GROUP_PROFILES_PATH", "GROUP_PROFILES_JSON"):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(str(env_file))

    assert settings.group_profiles["oc_24"]["report_members"] == ["李"]
    assert settings.group_profiles["oc_24"]["member_aliases"] == {"ou_li": "李"}
    assert settings.group_profiles["oc_24"]["leader_member_ids"] == ["ou_leader"]
