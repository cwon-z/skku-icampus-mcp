"""Runtime settings: the typed registry, validation, and who wins (UI over environment over default)."""

import pytest

from icampus.config import Settings
from icampus.runtime import ENV_ONLY, SPECS, Runtime, Spec, coerce
from icampus.store import Store


def runtime(tmp_path, **env) -> Runtime:
    settings = Settings(data_dir=tmp_path, _env_file=None, **env)
    return Runtime(settings, Store(settings.db_path))


def test_every_setting_has_an_environment_default_and_help():
    fields = Settings.model_fields
    for key, spec in SPECS.items():
        assert key in fields, f"{key} needs a Settings field (ICAMPUS_{key.upper()})"
        assert spec.help and spec.group
        coerce(spec, fields[key].default)  # the default passes its own validation
    secret_keys = {"password", "api_tokens", "admin_labels", "username", "max_logins_per_day", "host", "port"}
    assert not secret_keys & SPECS.keys()  # what decides who gets in is never editable at runtime
    assert secret_keys <= {key for key, _, _ in ENV_ONLY}


def test_precedence_ui_over_env_over_default(tmp_path):
    rt = runtime(tmp_path, video_cache_gb=12)
    assert (rt.video_cache_gb, rt.source("video_cache_gb")) == (12, "env")
    assert (rt.video_max_gb, rt.source("video_max_gb")) == (4, "default")
    assert rt.set("video_cache_gb", "20") == 20.0
    assert (rt.video_cache_gb, rt.source("video_cache_gb")) == (20.0, "ui")
    again = Runtime(rt.env, rt._store)  # a restart reads the stored value back
    assert (again.video_cache_gb, again.source("video_cache_gb")) == (20.0, "ui")
    assert again.reset("video_cache_gb") == 12 and again.source("video_cache_gb") == "env"
    assert rt.data_dir == tmp_path and rt.db_path == tmp_path / "icampus.db"  # everything else reads through
    described = {d["key"]: d for d in again.describe()}
    assert described["video_cache_gb"]["env"] == "ICAMPUS_VIDEO_CACHE_GB"
    assert described["video_cache_gb"]["value"] == 12 and described["video_cache_gb"]["default"] == 30


def test_listeners_hear_changes(tmp_path):
    rt = runtime(tmp_path)
    heard = []
    rt.on_change(heard.append)
    rt.set("stale_hours", 6)
    rt.reset("stale_hours")
    assert heard == ["stale_hours", "stale_hours"]


@pytest.mark.parametrize("key, raw, message", [
    ("video_cache_gb", "lots", "Use a number"),
    ("video_cache_gb", -1, "from 0.1 to 4000"),
    ("video_cache_gb", "nan", "Use a number"),
    ("attachment_max_mb", "2.5", "whole number"),
    ("attachment_max_mb", True, "whole number"),
    ("attachment_max_mb", 5000, "from 1 to 200"),
    ("sync_times", "7:30,25:00", "24-hour times"),
    ("sync_times", "noon", "24-hour times"),
])
def test_bad_values_are_refused_with_a_fix(tmp_path, key, raw, message):
    rt = runtime(tmp_path)
    before = getattr(rt, key)
    with pytest.raises(ValueError, match=message):
        rt.set(key, raw)
    assert getattr(rt, key) == before and rt.source(key) == "default"


def test_unknown_and_env_only_settings_cannot_be_set(tmp_path):
    rt = runtime(tmp_path)
    for key in ("api_tokens", "password", "nonsense"):
        with pytest.raises(KeyError):
            rt.set(key, "x")


def test_value_shapes():
    assert coerce(SPECS["sync_times"], " 7:30 , 22:30 ") == "07:30,22:30"
    assert coerce(SPECS["sync_times"], "") == ""  # no schedule
    assert coerce(SPECS["attachment_max_mb"], "30") == 30
    flag = Spec("flag", "bool", "g", "h")
    assert coerce(flag, "on") is True and coerce(flag, "false") is False and coerce(flag, "") is False
    with pytest.raises(ValueError):
        coerce(flag, "maybe")
    names = Spec("names", "list", "g", "h")
    assert coerce(names, "a, b") == ["a", "b"] and coerce(names, '["a","b"]') == ["a", "b"] and coerce(names, "") == []
    rules = Spec("rules", "rules", "g", "h")
    assert coerce(rules, '[{"course": "CSE1001", "provider": "p1", "note": 1}]') == [
        {"course": "CSE1001", "provider": "p1"}]
    for bad in ('[{"provider": "p1"}]', '[{"course": "x"}]', "{not json", '"text"'):
        with pytest.raises(ValueError):
            coerce(rules, bad)
    pairs = Spec("pairs", "map", "g", "h")
    assert coerce(pairs, '{"CSE1001": "ko"}') == {"CSE1001": "ko"} and coerce(pairs, "") == {}
    with pytest.raises(ValueError):
        coerce(pairs, '{"CSE1001": 3}')
    choice = Spec("choice", "enum", "g", "h", choices=("word", "segment"))
    assert coerce(choice, "word") == "word"
    with pytest.raises(ValueError, match="word, segment"):
        coerce(choice, "letter")


def test_a_stored_value_that_no_longer_validates_falls_back(tmp_path):
    rt = runtime(tmp_path, attachment_max_mb=25)
    rt._store.execute("INSERT INTO settings (key, value, updated_at) VALUES ('attachment_max_mb', '99999', 'x')")
    rt._store.execute("INSERT INTO settings (key, value, updated_at) VALUES ('from_the_future', '1', 'x')")
    again = Runtime(rt.env, rt._store)
    assert (again.attachment_max_mb, again.source("attachment_max_mb")) == (25, "env")


def test_env_only_settings_show_no_secrets(tmp_path):
    token = "s3cr3t" * 5
    rt = runtime(tmp_path, password="hunter2-hunter2", api_tokens=f"mcp:{token},admin:{token[::-1]}",
                 kuma_push_url="http://kuma.example/api/push/PUSHTOKEN")
    shown = {d["key"]: d["value"] for d in rt.describe_env_only()}
    assert shown["password"] == "set" and shown["api_tokens"] == "admin, mcp" and shown["kuma_push_url"] == "set"
    text = str(rt.describe_env_only()) + str(rt.describe())
    assert token not in text and "hunter2" not in text and "PUSHTOKEN" not in text
