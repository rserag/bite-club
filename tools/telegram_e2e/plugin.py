"""Pytest adapter. Network fixtures are registered only by the explicit CLI."""

import asyncio
from collections.abc import AsyncIterator, Generator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from tools.telegram_e2e.config import InfrastructureError, SetupError, load
from tools.telegram_e2e.driver import Driver
from tools.telegram_e2e.environment import Environment, drain, preflight
from tools.telegram_e2e.reporting import Recorder, write_report

RECORDER = pytest.StashKey[Recorder]()
RECORD = pytest.StashKey[dict[str, Any]]()
RESULTS = pytest.StashKey[list[dict[str, object]]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--e2e-config", required=True)
    parser.addoption("--e2e-output", required=True)


def pytest_configure(config: pytest.Config) -> None:
    config.stash[RESULTS] = []


@pytest.fixture(scope="session", autouse=True)
def live_lock() -> Iterator[None]:
    from tools.telegram_e2e.cli import exclusive

    with exclusive():
        yield


@pytest_asyncio.fixture
async def e2e(request: pytest.FixtureRequest) -> AsyncIterator[tuple[Driver, Environment]]:
    from tools.telegram_e2e.cli import client_for

    settings = load(Path(request.config.getoption("--e2e-config")))
    output = Path(request.config.getoption("--e2e-output"))
    recorder = Recorder(settings)
    request.node.stash[RECORDER] = recorder
    request.node.stash[RECORD] = {
        "scenario": request.node.name,
        "status": "passed",
        "events": recorder.events,
        "detail": "",
    }
    driver = Driver(settings, client_for(settings), recorder)
    env = Environment(settings, output / ("state-" + uuid4().hex), output)
    try:
        await driver.connect()
        await preflight(settings)
        offset = await drain(settings)
        await env.prepare(offset)
        await env.start()
        driver.assert_idle()
        yield driver, env
        await env.settled()
        driver.assert_idle()
    finally:
        # Disconnect even if stopping/removing the disposable environment fails.
        try:
            await env.close()
        finally:
            async with asyncio.timeout(10):
                await driver.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Generator[None]:
    yield
    default: dict[str, Any] = {"scenario": item.name, "status": "passed", "events": []}
    record = item.stash.get(RECORD, default)
    item.stash[RECORD] = record
    if call.excinfo is not None:
        error = call.excinfo.value
        if isinstance(error, AssertionError):
            status = "assertion_failure"
        elif isinstance(error, SetupError):
            status = "setup_failure"
        elif isinstance(error, InfrastructureError):
            status = "infrastructure_failure"
        else:
            status = "runner_failure"
        # No raw exception messages/tracebacks: assertion context is in the redacted timeline.
        record["status"] = status
        record["detail"] = type(error).__name__
        if isinstance(error, AssertionError) and RECORDER in item.stash:
            record["detail"] = item.stash[RECORDER].clean(str(error))
        if isinstance(error, SetupError | InfrastructureError):
            record["detail"] = str(error)  # These exceptions contain only fixed safe codes.
    if call.when == "teardown":
        item.config.stash[RESULTS].append(record)
        write_report(Path(item.config.getoption("--e2e-output")), item.config.stash[RESULTS])
