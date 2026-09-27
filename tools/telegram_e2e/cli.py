import argparse
import asyncio
import getpass
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

from nutrition_bot.runtime.lock import database_lock
from tools.telegram_e2e.config import (
    PRIVATE,
    ROOT,
    InfrastructureError,
    Settings,
    SetupError,
    confined,
    load,
)
from tools.telegram_e2e.driver import Driver
from tools.telegram_e2e.environment import preflight
from tools.telegram_e2e.reporting import Recorder, write_report


def client_for(settings: Settings) -> Any:
    try:
        from telethon import TelegramClient
    except ImportError:
        raise SetupError("install_e2e_dependency_group") from None
    session = confined(PRIVATE / "user.session", PRIVATE)
    if session.exists() and session.stat().st_mode & 0o077:
        raise SetupError("session_requires_mode_0600")
    client = TelegramClient(
        str(session),
        settings.api_id,
        settings.api_hash.get_secret_value(),
        request_retries=0,
        connection_retries=1,
        flood_sleep_threshold=0,
        auto_reconnect=False,
        catch_up=False,
        sequential_updates=True,
    )
    client.session.save_entities = False
    return client


@contextmanager
def exclusive() -> Iterator[None]:
    confined(PRIVATE / "runner.lock", PRIVATE)
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if PRIVATE.stat().st_mode & 0o077:
        raise SetupError("test_directory_requires_mode_0700")
    # Stronger than per-account locking: one local live run/login at a time.
    with database_lock(PRIVATE / "runner"):
        yield


async def login(settings: Settings) -> None:
    from telethon.errors import SessionPasswordNeededError

    client = client_for(settings)
    driver = Driver(settings, client, Recorder(settings))
    try:
        await driver.network(client.connect)
        if not await driver.network(client.is_user_authorized):
            phone = getpass.getpass("Test-account phone number (hidden): ")
            await driver.network(lambda: client.send_code_request(phone))
            code = getpass.getpass("Telegram login code (hidden): ")
            try:
                # Password-needed is an expected login branch, not a transport failure.
                async with asyncio.timeout(settings.step_timeout):
                    await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                password = getpass.getpass("Two-step verification password (hidden): ")
                await driver.network(lambda: client.sign_in(password=password))
        me = await driver.network(client.get_me)
        if me is None or me.bot or (settings.user_id and me.id != settings.user_id):
            raise SetupError("test_user_identity_mismatch")
        print(f"Authenticated user ID: {me.id}. Set user_id to this value in the private config.")
    finally:
        await client.disconnect()


async def doctor(settings: Settings) -> None:
    driver = Driver(settings, client_for(settings), Recorder(settings))
    try:
        await driver.connect()
        await preflight(settings)
        print("Test user, bot identity and polling configuration verified. No messages sent.")
    finally:
        await driver.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Isolated real-user Telegram tests (development only)"
    )
    parser.add_argument("--config", type=Path, default=PRIVATE / "config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Interactive private user authentication")
    commands.add_parser("doctor", help="Read-only identity and webhook checks")
    run = commands.add_parser(
        "run", help="Run serial live scenarios against the dedicated test bot"
    )
    run.add_argument("suite", choices=["smoke", "regression", "supplements", "nutrients"])
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)  # Provider exception strings may contain private data.
    try:
        settings = load(args.config, allow_unknown_user=args.command == "login")
        with exclusive() if args.command != "run" else nullcontext():
            if args.command == "login":
                if not sys.stdin.isatty():
                    raise SetupError("login_requires_interactive_terminal")
                asyncio.run(login(settings))
            elif args.command == "doctor":
                asyncio.run(doctor(settings))
            else:
                import pytest

                output = confined(PRIVATE / "runs" / uuid4().hex, PRIVATE)
                output.mkdir(parents=True, mode=0o700)
                write_report(output, [])
                # Prevent ambient pytest plugins/options from exporting credentials or collecting
                # unrelated tests. Explicitly register just asyncio and this runner's adapter.
                os.environ.pop("PYTEST_ADDOPTS", None)
                os.environ.pop("PYTEST_PLUGINS", None)
                os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
                arguments = [
                    str(ROOT / "tools/telegram_e2e/scenarios.py"),
                    "-q",
                    "-x",
                    "--tb=no",
                    "--show-capture=no",
                    "-p",
                    "pytest_asyncio.plugin",
                    "-p",
                    "tools.telegram_e2e.plugin",
                    "--e2e-config",
                    str(args.config.resolve()),
                    "--e2e-output",
                    str(output),
                    "-o",
                    "addopts=",
                    "-o",
                    "asyncio_mode=auto",
                    "-o",
                    "cache_dir=" + str(output / "cache"),
                ]
                if args.suite == "smoke":
                    arguments.extend(["-k", "smoke"])
                elif args.suite == "supplements":
                    arguments.extend(["-k", "supplement_"])
                elif args.suite == "nutrients":
                    arguments.extend(["-k", "nutrient_"])
                code = pytest.main(arguments)
                print(f"Private report: {output / 'report.html'}")
                raise SystemExit(int(code))
    except (SetupError, InfrastructureError) as exc:
        print(str(exc))
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print("Interrupted; inspect the private report before rerunning.")
        raise SystemExit(130) from None
    except Exception as exc:
        print("runner_failed: " + type(exc).__name__)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
