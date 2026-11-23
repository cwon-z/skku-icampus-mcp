"""The provider registry: configs and their guards, write-only keys, health, budgets with a reset day, and how a
provider is chosen (explicitly, by rule, by default, by fallback)."""

import json
import logging
from datetime import datetime, timedelta

import httpx
import pytest

from helpers import EL, OTHER_SECRET, SECRET, WHISPER, Stt
from icampus.config import KST, Settings
from icampus.runtime import Runtime
from icampus.store import Store
from icampus.stt.registry import DEFAULT_ELEVENLABS, Registry, Unavailable, period_end, period_start

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=KST)


def make(tmp_path, monkeypatch, *configs: dict, **env) -> Registry:
    monkeypatch.setenv("TEST_EL_KEY", SECRET)
    settings = Settings(data_dir=tmp_path, _env_file=None, stt_seed=json.dumps(list(configs or (EL, WHISPER))), **env)
    runtime = Runtime(settings, Store(settings.db_path))
    return Registry(runtime, runtime._store, transport=httpx.MockTransport(Stt()))


def spend(registry: Registry, provider_id: str, hours: float, at: datetime = NOW) -> None:
    registry.record_usage(registry.get(provider_id), hours * 3600, now=at)


# --- configs -----------------------------------------------------------------------------------

def test_first_boot_seeds_from_the_environment_or_the_default(tmp_path, monkeypatch):
    seeded = make(tmp_path / "a", monkeypatch)
    assert [c.id for c in seeded.all()] == ["el", "whisper"]  # by priority
    again = Registry(seeded.settings, seeded.store)
    assert [c.id for c in again.all()] == ["el", "whisper"]  # only while the table is empty

    file = tmp_path / "seed.json"
    file.write_text(json.dumps({"providers": [WHISPER, {"id": "Bad Id", "type": "elevenlabs"}]}))
    settings = Settings(data_dir=tmp_path / "b", stt_seed_file=file, _env_file=None)
    from_file = Registry(settings, Store(settings.db_path))
    assert [c.id for c in from_file.all()] == ["whisper"]  # the broken entry is skipped, not fatal

    settings = Settings(data_dir=tmp_path / "c", _env_file=None)
    default = Registry(settings, Store(settings.db_path))
    [cfg] = default.all()
    assert (cfg.id, cfg.model, cfg.api_key_env, cfg.monthly_hours_cap) == (
        "elevenlabs-scribe", "scribe_v3", "ELEVENLABS_API_KEY", 100)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert default.describe(cfg)["not_ready"].startswith("no API key (set ELEVENLABS_API_KEY")
    assert DEFAULT_ELEVENLABS["cost_per_hour"] == 0.20


@pytest.mark.parametrize("change, field, message", [
    ({"id": "Has Spaces"}, "id", "lowercase"),
    ({"api_key_env": "ICAMPUS_PASSWORD"}, "api_key_env", "never sent to a provider"),
    ({"api_key_env": "ICAMPUS_API_TOKENS"}, "api_key_env", "never sent to a provider"),
    ({"api_key_env": "lower-case"}, "api_key_env", "environment variable"),
    ({"base_url": "ftp://files.example"}, "base_url", "address like"),
    ({"base_url": "https://user:pw@api.example/v1"}, "base_url", "credentials"),
    ({"base_url": "https://lcms.skku.edu/v1"}, "base_url", "never sent to an SKKU host"),
    ({"base_url": "https://api.example/v1?key=1"}, "base_url", "no query"),
    ({"base_url": ""}, "config", "needs a base URL"),
    ({"languages": ["korean"]}, "languages", "ISO 639-1"),
    ({"audio_format": "aiff"}, "audio_format", "one of"),
    ({"capabilities": {"timestamps": "letter"}}, "config", "timestamps must be one of"),
    ({"capabilities": {"speed": 3}}, "config", "unknown capability"),
    ({"params": {"file": "x"}}, "params", "audio itself"),
    ({"concurrency": 0}, "concurrency", "greater than or equal to 1"),
    ({"cost_per_hour": -1}, "cost_per_hour", "greater than or equal to 0"),
    ({"reset_day": 32}, "reset_day", "less than or equal to 31"),
    ({"api_key": SECRET}, "api_key", "Extra inputs"),  # a key pasted into the config is refused, and not echoed
])
def test_bad_configs_are_refused_field_by_field(tmp_path, monkeypatch, change, field, message):
    registry = make(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match=message) as failure:
        registry.save({**WHISPER, "id": "new", **change}, create=True)
    assert field in failure.value.fields and SECRET not in str(failure.value)
    assert registry.get("new") is None


def test_save_update_delete(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="already exists"):
        registry.save(WHISPER, create=True)
    registry.save({**WHISPER, "description": "RTX 3090 on the tailnet", "cost_per_hour": 0,
                   "api_key_env": "ICAMPUS_STT_WHISPER_KEY"})
    assert registry.get("whisper").description == "RTX 3090 on the tailnet"
    registry.set_key("whisper", OTHER_SECRET)
    assert registry.delete("whisper") and registry.get("whisper") is None and not registry.delete("whisper")
    assert "whisper" not in json.loads(registry.keys_path.read_text())  # its key goes with it


# --- keys --------------------------------------------------------------------------------------

def test_keys_are_write_only_and_the_environment_wins(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    registry = make(tmp_path, monkeypatch)
    el, whisper = registry.get("el"), registry.get("whisper")
    assert registry.key(el) == SECRET
    assert registry.key_status(el) == {"set": True, "source": "env", "env": "TEST_EL_KEY", "hint": "…1234",
                                       "needed": True}
    assert registry.key_status(whisper) == {"set": False, "source": None, "env": None, "hint": None, "needed": False}

    registry.set_key("whisper", f"  {OTHER_SECRET}\n")
    assert registry.keys_path.stat().st_mode & 0o777 == 0o600 and registry.key(whisper) == OTHER_SECRET
    assert registry.key_status(whisper)["source"] == "stored" and registry.key_status(whisper)["hint"] == "…5678"
    registry.set_key("el", "stored-but-overridden-key")
    assert registry.key(el) == SECRET  # the environment variable still wins
    monkeypatch.delenv("TEST_EL_KEY")
    assert registry.key(el) == "stored-but-overridden-key" and registry.key_status(el)["source"] == "stored"
    registry.set_key("el", None)
    assert registry.key(el) is None and registry.not_eligible(el)[1] == "key"
    registry.set_key("short", "abc12345")
    assert registry._stored_keys()["short"] == "abc12345"

    shown = json.dumps([registry.describe(c, admin=True) for c in registry.all()], ensure_ascii=False)
    assert SECRET not in shown and OTHER_SECRET not in shown and "stored-but" not in shown
    assert SECRET not in caplog.text and OTHER_SECRET not in caplog.text


# --- health ------------------------------------------------------------------------------------

async def test_the_test_action_records_health_and_a_failing_provider_is_skipped(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    registry = make(tmp_path, monkeypatch, stt_fail_threshold=2, stt_skip_minutes=15)
    server = registry.transport.handler
    assert registry.health_state("el") == "unknown"
    result = await registry.test("el")
    assert result["ok"] and result["timestamps"] == "word" and "eigenvalue" in result["text"]
    assert registry.health_state("el") == "ok" and registry.health("el")["last_test"]["ok"]
    assert registry.usage(registry.get("el"))["hours"] == pytest.approx(5 / 3600)  # the test clip is paid for too
    assert server.calls[0][1]["language_code"] == ["en"]

    server.broken.add("api.elevenlabs.io")
    result = await registry.test("el")
    assert not result["ok"] and "refused the API key" in result["error"] and SECRET not in json.dumps(result)
    assert registry.health_state("el") == "failing" and registry.not_eligible(registry.get("el")) is None
    await registry.test("el")  # the second failure in a row reaches the threshold
    assert registry.health_state("el") == "skipped"
    reason, kind = registry.not_eligible(registry.get("el"))
    assert kind == "health" and "skipped until" in reason and SECRET not in reason
    later = datetime.now(KST) + timedelta(minutes=16)
    assert registry.not_eligible(registry.get("el"), now=later) is None  # skipped for 15 minutes, not for good
    registry.record_ok("el")
    assert registry.health_state("el") == "ok" and registry.health("el")["failures"] == 0
    assert SECRET not in caplog.text and SECRET not in json.dumps(registry.health("el"))
    with pytest.raises(KeyError):
        await registry.test("nobody")


# --- budget ------------------------------------------------------------------------------------

def test_budget_periods_start_on_the_reset_day():
    assert period_start(1, NOW) == datetime(2026, 11, 1, tzinfo=KST)
    assert period_start(20, NOW) == datetime(2026, 11, 20, tzinfo=KST)  # the reset day itself opens the period
    assert period_start(21, NOW) == datetime(2026, 10, 21, tzinfo=KST)
    assert period_end(21, NOW) == datetime(2026, 11, 21, tzinfo=KST)
    assert period_start(15, datetime(2027, 1, 3, tzinfo=KST)) == datetime(2026, 12, 15, tzinfo=KST)  # over new year
    assert period_end(15, datetime(2026, 12, 20, tzinfo=KST)) == datetime(2027, 1, 15, tzinfo=KST)
    march = datetime(2027, 3, 1, 12, tzinfo=KST)
    assert period_start(31, march) == datetime(2027, 2, 28, tzinfo=KST)  # a month without a 31st: its last day
    assert period_end(31, march) == datetime(2027, 3, 31, tzinfo=KST)


def test_caps_usage_and_month_rollover(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch, {**EL, "monthly_hours_cap": 10, "reset_day": 15})
    el = registry.get("el")
    assert registry.budget(el, NOW) == {"used_hours": 0.0, "used_usd": 0.0, "cap_hours": 10, "cap_usd": None,
                                        "left_hours": 10.0, "resets": "2026-12-15"}
    spend(registry, "el", 8.5)
    assert registry.budget(el, NOW)["left_hours"] == 1.5 and registry.budget(el, NOW)["used_usd"] == 1.7
    assert registry.over_budget(el, 1.5, NOW) is None
    reason = registry.over_budget(el, 2.0, NOW)
    assert "monthly cap of 10 h" in reason and "1.5 h left" in reason and "resets 2026-12-15" in reason
    assert registry.not_eligible(el, hours=2.0, now=NOW)[1] == "budget"

    registry.reserve(7, "el", 1.0)  # a running job's hours count before they are billed
    assert registry.budget(el, NOW)["left_hours"] == 0.5 and registry.over_budget(el, 1.0, NOW)
    assert registry.over_budget(el, 1.0, NOW, but=7) is None  # the job's own reservation doesn't count against it
    registry.release(7)

    next_period = datetime(2026, 12, 15, 0, 5, tzinfo=KST)
    assert registry.budget(el, next_period)["left_hours"] == 10.0  # usage before the reset day no longer counts
    assert registry.over_budget(el, 9.0, next_period) is None


def test_cost_counts_only_hours_beyond_the_plan_and_usd_caps_follow(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch, {**EL, "included_hours": 5, "monthly_usd_cap": 1.0},
                    {**WHISPER, "cost_per_hour": 0}, stt_monthly_usd_cap=1.5)
    el, whisper = registry.get("el"), registry.get("whisper")
    assert registry.cost(el, 3, 0) == 0 and registry.cost(el, 3, 4) == 0.4 and registry.cost(el, 2, 6) == 0.4
    spend(registry, "el", 4)
    assert registry.usage(el, NOW)["usd"] == 0
    assert registry.budget(el, NOW)["left_hours"] == 6.0  # 1 h still included, then $1.00 buys 5 h at $0.20
    spend(registry, "el", 3)  # 1 h free + 2 h paid
    assert registry.usage(el, NOW)["usd"] == pytest.approx(0.4) and registry.budget(el, NOW)["left_hours"] == 3.0
    assert registry.over_budget(el, 3.0, NOW) is None and "cap of $1" in registry.over_budget(el, 3.5, NOW)
    assert registry.budget(whisper, NOW)["left_hours"] is None and registry.over_budget(whisper, 500, NOW) is None

    registry.save({**EL, "included_hours": 0, "monthly_usd_cap": None})  # only the cap for all providers is left
    el = registry.get("el")
    assert registry.over_budget(el, 5.0, NOW) is None  # $0.40 spent + $1.00
    assert "total monthly cap of $1.5" in registry.over_budget(el, 6.0, NOW)


# --- choosing ----------------------------------------------------------------------------------

LOCAL = {**WHISPER, "id": "local-ko", "priority": 30, "languages": ["ko"]}
COURSE = {"id": "1001", "code": "CSE1001", "name": "Data Structures"}


def ids(choices) -> list[str]:
    return [c.config.id for c in choices]


def test_an_explicit_model_is_that_one_or_an_error_that_lists_what_works(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch, EL, WHISPER, {**LOCAL, "enabled": False})
    [choice] = registry.choose(model="whisper")
    assert choice.config.id == "whisper" and choice.reason == "asked for by id (whisper)"
    with pytest.raises(Unavailable, match="no speech-to-text provider is called 'nobody'") as failure:
        registry.choose(model="nobody")
    assert failure.value.working == ["el", "whisper"]
    with pytest.raises(Unavailable, match="local-ko can't take this now: disabled") as failure:
        registry.choose(model="local-ko")
    assert failure.value.working == ["el", "whisper"] and not failure.value.budget
    monkeypatch.delenv("TEST_EL_KEY")
    with pytest.raises(Unavailable, match="no API key") as failure:
        registry.choose(model="el")
    assert failure.value.working == ["whisper"]


def test_an_explicit_model_over_its_cap_says_so(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch, {**EL, "monthly_hours_cap": 1}, WHISPER)
    assert ids(registry.choose(model="el", hours=0.5)) == ["el"]
    with pytest.raises(Unavailable, match="monthly cap of 1 h") as failure:
        registry.choose(model="el", hours=1.5)
    assert failure.value.budget and failure.value.working == ["whisper"]


def test_automatic_routing_rule_then_default_then_fallback_then_priority(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch, EL, WHISPER, LOCAL, {**WHISPER, "id": "spare", "priority": 5})
    settings = registry.settings
    assert ids(registry.choose()) == ["spare", "el", "whisper", "local-ko"]  # no default set: by priority
    settings.set("stt_default_provider", "el")
    settings.set("stt_fallback_order", ["local-ko", "whisper"])
    choices = registry.choose(course=COURSE, language="ko")
    assert ids(choices) == ["el", "local-ko", "whisper", "spare"]
    assert [c.reason for c in choices] == ["default provider", "fallback order", "fallback order", "by priority"]

    settings.set("stt_routing_rules", [{"course": "MTH2002", "provider": "spare"},
                                       {"course": "cse1001", "language": "ko", "provider": "local-ko"},
                                       {"language": "en", "provider": "whisper"}])
    choices = registry.choose(course=COURSE, language="ko")
    assert ids(choices)[:2] == ["local-ko", "el"] and choices[0].reason == "routing rule (course cse1001 and language ko)"
    assert ids(registry.choose(course=COURSE, language="en"))[0] == "whisper"  # the first rule that matches
    assert ids(registry.choose(course={"id": "1002", "code": "MTH2002"}, language=None))[0] == "spare"
    assert ids(registry.choose(course=COURSE, language=None))[0] == "el"  # no language known: language rules don't match
    assert ids(registry.choose(course={"id": "9"}, language="ja")) == ["el", "whisper", "spare"]  # local-ko is Korean only


def test_automatic_routing_skips_what_cannot_take_it_and_says_why(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch, {**EL, "monthly_hours_cap": 1}, WHISPER,
                    {**LOCAL, "capabilities": {"timestamps": "none", "max_upload_mb": None}})
    registry.settings.set("stt_default_provider", "el")
    choices = registry.choose(hours=2.0, language="ko")
    assert ids(choices) == ["whisper"]  # el is over its cap; local-ko gives no timestamps and segments are required
    assert choices[0].reason.startswith("by priority (el skipped: over its monthly cap")
    registry.settings.set("stt_min_timestamps", "none")
    assert ids(registry.choose(hours=2.0, language="ko")) == ["whisper", "local-ko"]
    registry.settings.set("stt_min_timestamps", "word")
    registry.save({**WHISPER, "capabilities": {"timestamps": "segment"}})
    assert ids(registry.choose(hours=0.5)) == ["el"]

    for _ in range(3):
        registry.record_failure("el", f"HTTP 500 with {SECRET}")
    with pytest.raises(Unavailable) as failure:
        registry.choose(hours=0.5)
    message = str(failure.value)
    assert "el: failing, skipped until" in message and "whisper: returns segment timestamps" in message
    assert SECRET not in message and not failure.value.budget


def test_no_provider_left_because_of_a_cap_is_a_budget_matter(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch, {**EL, "monthly_hours_cap": 1}, {**WHISPER, "enabled": False})
    with pytest.raises(Unavailable) as failure:
        registry.choose(hours=3.0)
    assert failure.value.budget  # the job can wait for the next period
    registry.delete("el")
    registry.delete("whisper")
    with pytest.raises(Unavailable, match="no speech-to-text provider is registered"):
        registry.choose()


def test_describe_gives_enough_to_choose_and_nothing_secret(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch, {**EL, "monthly_hours_cap": 100, "included_hours": 110}, WHISPER)
    spend(registry, "el", 12.5, datetime.now(KST))
    el, whisper = (registry.describe(c) for c in registry.all())
    assert el["default"] and el["ready"] and el["word_timestamps"] and el["cost_per_hour_usd"] == 0.2
    assert el["description"] == "hosted, word timestamps" and el["languages"] == "any" and el["key"] == "set"
    assert el["month"]["used_hours"] == 12.5 and el["month"]["left_hours"] == 87.5 and el["month"]["used_usd"] == 0
    assert el["health"]["state"] == "unknown"
    assert whisper["key"] == "not needed" and whisper["max_upload_mb"] is None and not whisper["default"]
    assert "config" not in el and "key_status" not in el
    assert SECRET not in json.dumps([el, whisper])


def test_the_cap_for_all_providers_counts_running_jobs_too(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch, EL, {**WHISPER, "cost_per_hour": 0.1}, stt_monthly_usd_cap=1.0)
    el, whisper = registry.get("el"), registry.get("whisper")
    spend(registry, "el", 2.5)  # $0.50 billed
    assert registry.over_budget(el, 2.0, NOW) is None  # $0.40 more fits
    registry.reserve(1, "el", 2.0)  # a job that is running now will be billed $0.40
    assert "total monthly cap of $1" in registry.over_budget(el, 2.0, NOW, but=2)  # so a second one doesn't fit
    assert "total monthly cap of $1" in registry.over_budget(whisper, 2.0, NOW, but=2)  # whichever provider it uses
    assert registry.over_budget(whisper, 0.5, NOW, but=2) is None
    registry.release(1)
    assert registry.over_budget(el, 2.0, NOW, but=2) is None


def test_a_used_up_cap_takes_nothing_not_even_a_lecture_of_unknown_length(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch, {**EL, "monthly_hours_cap": 2}, WHISPER)
    el = registry.get("el")
    assert registry.over_budget(el, 0.0, NOW) is None and registry.not_eligible(el, now=NOW) is None
    spend(registry, "el", 2.0)
    assert registry.over_budget(el, 0.0, NOW) == "its monthly cap of 2 h is used up (resets 2026-12-01)"
    assert registry.not_eligible(el, now=NOW)[1] == "budget"
    described = registry.describe(el, now=NOW)
    assert described["ready"] is False and "used up" in described["not_ready"]
    assert registry.working(now=NOW) == ["whisper"]


def test_a_stored_key_only_ever_goes_to_the_address_it_was_entered_for(tmp_path, monkeypatch):
    registry = make(tmp_path, monkeypatch)
    registry.set_key("whisper", OTHER_SECRET)
    registry.save({**WHISPER, "description": "same place, new words", "priority": 5})
    assert registry.key(registry.get("whisper")) == OTHER_SECRET  # an edit that leaves the address alone keeps it
    registry.save({**WHISPER, "base_url": "http://gpu.tailnet.example:8000/v2"})
    assert registry.key(registry.get("whisper")) == OTHER_SECRET  # the same host: still the same server
    registry.save({**WHISPER, "base_url": "https://collector.attacker.example/v1"})
    assert registry.key(registry.get("whisper")) is None  # another host: the key has to be entered again
    assert "whisper" not in registry._stored_keys()

    with pytest.raises(ValueError, match="only talks to elevenlabs.io"):  # nor can the ElevenLabs key be redirected
        registry.save({**EL, "base_url": "https://collector.attacker.example/v1"})
    with pytest.raises(ValueError, match="only talks to elevenlabs.io"):
        registry.save({**EL, "base_url": "https://api.elevenlabs.io.attacker.example/v1"})
    registry.save({**EL, "base_url": "https://api.eu.residency.elevenlabs.io/v1"})
    assert registry.get("el").base_url == "https://api.eu.residency.elevenlabs.io/v1"


def test_a_local_run_finds_provider_keys_kept_in_the_env_file(tmp_path, monkeypatch):
    from icampus.cli import export_dotenv
    env_file = tmp_path / ".env"
    env_file.write_text("ICAMPUS_PASSWORD='hunter2'\nTEST_LOCAL_STT_KEY=from-the-file-1234\nALREADY_SET=from-file\nEMPTY=\n")
    monkeypatch.delenv("TEST_LOCAL_STT_KEY", raising=False)
    monkeypatch.delenv("ICAMPUS_PASSWORD", raising=False)
    monkeypatch.setenv("ALREADY_SET", "from-the-shell")
    export_dotenv(str(env_file))
    import os
    assert os.environ["TEST_LOCAL_STT_KEY"] == "from-the-file-1234" and os.environ["ALREADY_SET"] == "from-the-shell"
    assert "ICAMPUS_PASSWORD" not in os.environ and "EMPTY" not in os.environ  # the settings read those themselves
    registry = make(tmp_path / "data", monkeypatch, {**EL, "api_key_env": "TEST_LOCAL_STT_KEY"})
    assert registry.key(registry.get("el")) == "from-the-file-1234"
    monkeypatch.delenv("TEST_LOCAL_STT_KEY")
    export_dotenv(str(tmp_path / "missing.env"))  # no file: nothing happens
