"""Offline tests of the live runner. No test here connects to Telegram."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.telegram_e2e.config import InfrastructureError, Settings, SetupError, confined, load
from tools.telegram_e2e.driver import Collector, Driver, Message
from tools.telegram_e2e.environment import Environment, drain
from tools.telegram_e2e.reporting import Recorder, write_report


@pytest.fixture
def config():
    return Settings(
        dedicated_test_bot=True,
        api_id=12345,
        api_hash="a" * 32,
        bot_token="678901:synthetic_token_for_offline_tests_only",
        bot_id=678901,
        bot_username="synthetic_test_bot",
        user_id=54321,
    )


def write_config(tmp_path, config):
    path = tmp_path / "config.toml"
    values = config.model_dump()
    values["api_hash"] = config.api_hash.get_secret_value()
    values["bot_token"] = config.bot_token.get_secret_value()
    path.write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items()))
    path.chmod(0o600)
    return path


def test_configuration_missing_private_and_identity_guards(tmp_path, config):
    path = write_config(tmp_path, config)
    assert load(path, root=tmp_path) == config
    path.chmod(0o644)
    with pytest.raises(SetupError, match="0600"):
        load(path, root=tmp_path)
    path.chmod(0o600)
    path.write_text(
        path.read_text().replace("dedicated_test_bot = true", "dedicated_test_bot = false")
    )
    with pytest.raises(SetupError, match="invalid_test_configuration"):
        load(path, root=tmp_path)


def test_config_refuses_production_identity(tmp_path, config, monkeypatch):
    path = write_config(tmp_path, config)
    monkeypatch.setattr(
        "dotenv.dotenv_values",
        lambda _: {"TELEGRAM_BOT_TOKEN": config.bot_token.get_secret_value()},
    )
    with pytest.raises(SetupError, match="production_bot"):
        load(path, root=tmp_path)


def test_login_unknown_user_and_no_error_secret(tmp_path, config):
    path = write_config(tmp_path, config.model_copy(update={"user_id": 0}))
    assert load(path, root=tmp_path, allow_unknown_user=True).user_id == 0
    with pytest.raises(SetupError, match="user_id_required"):
        load(path, root=tmp_path)
    path.write_text(path.read_text().replace("api_id = 12345", 'api_id = "private-secret"'))
    with pytest.raises(SetupError) as exc:
        load(path, root=tmp_path)
    assert "private-secret" not in str(exc.value)


def test_no_traversal_or_symlink(tmp_path):
    outside = tmp_path.parent / "outside"
    with pytest.raises(SetupError):
        confined(outside, tmp_path)
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(SetupError, match="symlink"):
        confined(tmp_path / "link" / "anything", tmp_path)


async def test_event_before_wait_and_same_id_edit():
    events = Collector(100)
    events.feed(100, 100, Message(1, "saved"))
    assert (await events.expect(0, "saved", 0.1)).id == 1
    events.feed(100, 100, Message(1, "saved"))
    assert len(events.events) == 1
    events.feed(100, 100, Message(1, "changed", edited=True))
    assert (await events.expect(1, "changed", 0.1)).edited


async def test_ignores_other_chat_and_waits_for_new_event():
    events = Collector(100)
    events.feed(101, 100, Message(1, "wrong chat"))
    events.feed(100, 101, Message(1, "wrong sender"))
    with pytest.raises(InfrastructureError, match="not_retried"):
        await events.expect(0, "saved", 0.01)
    waiter = asyncio.create_task(events.expect(0, "saved", 1))
    await asyncio.sleep(0)
    events.feed(100, 100, Message(4, "saved"))
    assert (await waiter).id == 4


async def test_wrong_content_and_duplicate_responses_fail():
    events = Collector(100)
    events.feed(100, 100, Message(1, "100 kcal"))
    with pytest.raises(AssertionError, match="999 kcal"):
        await events.expect(0, "999 kcal", 1)
    events.feed(100, 100, Message(2, "100 kcal"))
    with pytest.raises(AssertionError, match="duplicate"):
        await events.expect(0, "100 kcal", 1)


async def test_ambiguous_send_does_not_retry(config):
    client = SimpleNamespace(send_message=AsyncMock(side_effect=OSError("private payload")))
    driver = Driver(config, client, Recorder(config))
    with pytest.raises(InfrastructureError) as exc:
        await driver.send("100g rice", "Saved")
    assert "private payload" not in str(exc.value)
    assert client.send_message.await_count == 1


async def test_button_validation_and_late_event(config):
    driver = Driver(config, None, Recorder(config))
    with pytest.raises(AssertionError, match="ambiguous"):
        await driver.click(Message(1, "draft", ("Approve", "Approve")), "Approve", "Saved")
    driver.collector.feed(config.bot_id, config.bot_id, Message(1, "late"))
    with pytest.raises(AssertionError, match="late"):
        driver.assert_idle()


async def test_real_callback_handle_and_separate_ack(config):
    recorder = Recorder(config)
    driver = Driver(config, None, recorder)

    async def click(**kwargs):
        assert kwargs == {"text": "Approve"}
        driver.collector.feed(config.bot_id, config.bot_id, Message(999, "Saved"))
        return SimpleNamespace(message="Approved")

    raw = SimpleNamespace(click=click)
    result = await driver.click(Message(555, "draft", ("Approve",), raw), "Approve", "Saved")
    assert result.id == 999
    assert any(event["kind"] == "callback_ack" for event in recorder.events)


def test_private_reports_redact_and_escape(tmp_path, config):
    recorder = Recorder(config)
    recorder.add(
        "bot",
        f"{config.bot_token.get_secret_value()} {config.api_hash.get_secret_value()} "
        f"{config.user_id} <script>",
    )
    results = [{"scenario": "quantity", "status": "assertion_failure", "events": recorder.events}]
    write_report(tmp_path, results)
    body = (tmp_path / "report.html").read_text()
    assert config.bot_token.get_secret_value() not in body
    assert config.api_hash.get_secret_value() not in body
    assert str(config.user_id) not in body
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert b"<failure" in (tmp_path / "junit.xml").read_bytes()


async def test_environment_seed_cursor_read_only_and_cleanup(tmp_path, config, monkeypatch):
    monkeypatch.setenv("USDA_API_KEY", "must-not-be-inherited")
    env = Environment(config, tmp_path / "scenario", tmp_path)
    try:
        assert "USDA_API_KEY" not in env.child_env()
        await env.prepare(789)
        assert env.read("SELECT next_offset FROM telegram_cursor") == [(789,)]
        assert env.read("SELECT count(*) FROM food_versions") == [(2,)]
        assert env.read("SELECT count(*) FROM meals") == [(0,)]
        import sqlite3

        with pytest.raises(sqlite3.OperationalError):
            env.read("DELETE FROM food_versions")
        with pytest.raises(FileExistsError):
            await env.prepare(0)
    finally:
        await env.close()
    assert not env.path.exists()


async def test_does_not_remove_existing_or_unmarked_directory(tmp_path, config):
    target = tmp_path / "existing"
    target.mkdir()
    (target / "valuable").write_text("keep")
    env = Environment(config, target, tmp_path)
    with pytest.raises(FileExistsError):
        await env.prepare(0)
    await env.close()
    assert (target / "valuable").read_text() == "keep"


async def test_backlog_acknowledged_before_handoff(config, monkeypatch):
    calls = []

    class FakeBot:
        def __init__(self, token):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_updates(self, **kwargs):
            calls.append(kwargs["offset"])
            return [SimpleNamespace(update_id=77)] if len(calls) == 1 else []

    monkeypatch.setattr("tools.telegram_e2e.environment.Bot", FakeBot)
    assert await drain(config) == 78
    assert calls == [0, 78]


async def test_flood_wait_stops_without_mutation_retry(config):
    from telethon.errors import FloodWaitError

    operation = AsyncMock(side_effect=FloodWaitError(request=None, capture=42))
    driver = Driver(config, None, Recorder(config))
    with pytest.raises(InfrastructureError, match="flood_wait_seconds_42"):
        await driver.network(operation)
    assert operation.await_count == 1


async def test_identity_refused_before_sending(config):
    client = SimpleNamespace(
        connect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=SimpleNamespace(bot=False, id=1)),
        send_message=AsyncMock(),
    )
    driver = Driver(config, client, Recorder(config))
    with pytest.raises(SetupError, match="identity_mismatch"):
        await driver.connect()
    client.send_message.assert_not_called()


async def test_worker_process_stopped_before_directory_cleanup(tmp_path, config):
    import sys

    env = Environment(config, tmp_path / "scenario", tmp_path)
    await env.prepare(0)
    env.process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )
    process = env.process
    await env.close()
    assert process.returncode is not None
    assert not env.path.exists()


def test_live_lock_is_exclusive(tmp_path, monkeypatch):
    from tools.telegram_e2e import cli

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(cli, "PRIVATE", private)
    with cli.exclusive():
        with pytest.raises(RuntimeError, match="owns this database"):
            with cli.exclusive():
                pass


@pytest.mark.parametrize(
    "error,status",
    [
        (AssertionError("expected 999 kcal"), "assertion_failure"),
        (InfrastructureError("telegram_transport_error"), "infrastructure_failure"),
        (SetupError("login_required"), "setup_failure"),
    ],
)
def test_report_classification(tmp_path, config, error, status):
    from tools.telegram_e2e import plugin

    stash = pytest.Stash()
    stash[plugin.RECORDER] = Recorder(config)
    session = pytest.Stash()
    session[plugin.RESULTS] = []
    item = SimpleNamespace(
        name="synthetic-failure",
        stash=stash,
        config=SimpleNamespace(stash=session, getoption=lambda _: str(tmp_path)),
    )

    def fail():
        raise error

    call = pytest.CallInfo.from_call(fail, "call")
    hook = plugin.pytest_runtest_makereport(item, call)
    next(hook)
    with pytest.raises(StopIteration):
        next(hook)
    teardown = plugin.pytest_runtest_makereport(
        item, pytest.CallInfo.from_call(lambda: None, "teardown")
    )
    next(teardown)
    with pytest.raises(StopIteration):
        next(teardown)
    result = json.loads((tmp_path / "report.json").read_text())["results"][0]
    assert result["status"] == status


def test_pytest_bootstrap_after_cli_import(tmp_path):
    import os
    import subprocess
    import sys

    from tools.telegram_e2e.config import ROOT

    # Importing the CLI loads stdlib asyncio. The short plugin name "asyncio"
    # collides with it under warning-as-error; use the actual plugin module.
    script = """
import sys
from tools.telegram_e2e import cli
import pytest
raise SystemExit(pytest.main([
    'tools/telegram_e2e/scenarios.py', '--collect-only', '-q',
    '-p', 'pytest_asyncio.plugin', '-p', 'tools.telegram_e2e.plugin',
    '--e2e-config', 'tools/telegram_e2e/config.example.toml',
    '--e2e-output', sys.argv[1],
]))
"""
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "10 tests collected" in result.stdout
