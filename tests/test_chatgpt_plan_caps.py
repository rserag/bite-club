"""Independent subscription allowances using immutable synthetic invocation history."""

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from nutrition_bot.adapters.ai.chatgpt_auth import save_credentials
from nutrition_bot.adapters.ai.chatgpt_plan import ChatGPTPlanAdapter
from nutrition_bot.adapters.database.schema_ai import ai_attempts, ai_plan_invocations, ai_requests
from nutrition_bot.adapters.database.schema_ai_metrics import ai_metrics
from nutrition_bot.application.ai_service import AiService
from tests.test_ai_drafts import TODAY
from tests.test_chatgpt_plan_ai import credentials, policy, private_path, stream
from tests.test_telegram_meals import catalog as catalog


@asynccontextmanager
async def plan_pair(store, tmp_path, *, fail=False, **policy_changes):
    path = private_path(tmp_path)
    save_credentials(path, credentials())
    calls = []

    def transport(request):
        assert not store.writer_lock.locked()
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200, json={"models": [{"slug": "synthetic-model-v1", "visibility": "list"}]}
            )
        return httpx.Response(503 if fail else 200, content=stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = ChatGPTPlanAdapter(path, policy(**policy_changes), client=client)
        yield (
            AiService(store, plan_adapter=adapter),
            AiService(store, plan_adapter=adapter, evaluation_prefix="eval:synthetic:"),
            calls,
            adapter,
        )


async def attempt(service, key):
    return await service.interpret(request_key=key, text="synthetic rice", local_date=TODAY)


def test_legacy_policy_defaults_to_independent_full_allowances():
    selected = policy()
    assert selected.daily_invocation_limit == selected.daily_evaluation_invocation_limit == 100
    legacy = selected.model_dump()
    del legacy["daily_evaluation_invocation_limit"]
    restored = selected.__class__.model_validate(legacy)
    assert restored.daily_evaluation_invocation_limit == 100


@pytest.mark.parametrize("limit", [0, 1, 100])
def test_evaluation_cap_accepts_independent_bounded_integers(limit):
    assert (
        policy(daily_evaluation_invocation_limit=limit).daily_evaluation_invocation_limit == limit
    )


@pytest.mark.parametrize("limit", [-1, 101, True, "100", 1.0, None])
def test_evaluation_cap_rejects_invalid_values(limit):
    with pytest.raises(ValidationError):
        policy(daily_evaluation_invocation_limit=limit)


@pytest.mark.parametrize("limit", [0, -1, 101, True, "100", 1.0])
def test_bot_cap_keeps_existing_validation_boundaries(limit):
    with pytest.raises(ValidationError):
        policy(daily_invocation_limit=limit)


@pytest.mark.parametrize("first_pool", ["bot", "evaluation"])
async def test_each_exhausted_pool_leaves_other_pool_available(
    store, catalog, tmp_path, first_pool
):
    async with plan_pair(
        store, tmp_path, daily_invocation_limit=1, daily_evaluation_invocation_limit=1
    ) as (bot, evaluation, calls, _):
        pools = {"bot": (bot, "normal"), "evaluation": (evaluation, "eval:synthetic:")}
        first, prefix = pools[first_pool]
        other, other_prefix = pools["evaluation" if first_pool == "bot" else "bot"]
        initial = await attempt(first, prefix + "one")
        assert initial.status == "ready"
        assert (await attempt(first, prefix + "two")).status == "quota"
        assert (await attempt(first, prefix + "one")) == initial
        assert (await attempt(other, other_prefix + "one")).status == "ready"
        assert (await attempt(other, other_prefix + "two")).status == "quota"
    assert calls.count("POST") == 2
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(ai_plan_invocations))
            == 2
        )
        assert await connection.scalar(sa.select(sa.func.count()).select_from(ai_attempts)) == 0


async def test_evaluation_runs_share_the_validation_pool(store, catalog, tmp_path):
    async with plan_pair(store, tmp_path, daily_evaluation_invocation_limit=1) as (
        bot,
        evaluation,
        calls,
        adapter,
    ):
        other_run = AiService(store, plan_adapter=adapter, evaluation_prefix="eval:different:")
        assert (await attempt(evaluation, "eval:synthetic:one")).status == "ready"
        assert (await attempt(other_run, "eval:different:one")).status == "quota"
        assert (await attempt(bot, "normal-one")).status == "ready"
    assert calls.count("POST") == 2


@pytest.mark.parametrize("state", ["done", "unknown"])
async def test_hundred_legacy_validation_rows_do_not_consume_bot_pool(
    store, catalog, tmp_path, state
):
    day = datetime.now(UTC).date().isoformat()
    async with store.write() as connection:
        rows = [
            {
                "request_key": f"eval:legacy:{index}",
                "role": "meal_text",
                "state": state,
                "created_at": time.time(),
            }
            for index in range(100)
        ]
        await connection.execute(sa.insert(ai_requests), rows)
        await connection.execute(
            sa.insert(ai_plan_invocations),
            [
                {
                    "request_key": row["request_key"],
                    "day": day,
                    "state": state,
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        )
    async with plan_pair(store, tmp_path) as (bot, evaluation, calls, _):
        assert (await attempt(evaluation, "eval:synthetic:one")).status == "quota"
        assert (await attempt(bot, "normal-one")).status == "ready"
    assert calls.count("POST") == 1
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(ai_plan_invocations))
            == 101
        )
        assert await connection.scalar(
            sa.select(sa.func.count())
            .select_from(ai_plan_invocations)
            .where(ai_plan_invocations.c.state == state)
        ) == 100 + (state == "done")


@pytest.mark.parametrize("pool", ["bot", "evaluation"])
async def test_unknown_attempt_consumes_only_its_pool_and_is_never_retried(
    store, catalog, tmp_path, pool
):
    async with plan_pair(
        store, tmp_path, fail=True, daily_invocation_limit=1, daily_evaluation_invocation_limit=1
    ) as (bot, evaluation, calls, _):
        selected = evaluation if pool == "evaluation" else bot
        other = bot if pool == "evaluation" else evaluation
        prefix = "eval:synthetic:" if pool == "evaluation" else "normal-"
        other_prefix = "normal-" if pool == "evaluation" else "eval:synthetic:"
        initial = await attempt(selected, prefix + "one")
        assert initial.status == "unavailable"
        assert await attempt(selected, prefix + "one") == initial
        assert (await attempt(selected, prefix + "two")).status == "quota"
        assert (await attempt(other, other_prefix + "one")).status == "unavailable"
    assert calls.count("POST") == 2
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(ai_plan_invocations)
                .where(ai_plan_invocations.c.state == "unknown")
            )
            == 2
        )


async def test_evaluation_cap_zero_stops_validation_without_borrowing_bot_allowance(
    store, catalog, tmp_path
):
    async with plan_pair(store, tmp_path, daily_evaluation_invocation_limit=0) as (
        bot,
        evaluation,
        calls,
        _,
    ):
        assert (await attempt(evaluation, "eval:synthetic:one")).status == "quota"
        assert calls == []
        assert (await attempt(bot, "normal-one")).status == "ready"
    assert calls.count("POST") == 1


async def test_independent_caps_preserve_cross_pool_single_flight(store, catalog, tmp_path):
    path = private_path(tmp_path)
    save_credentials(path, credentials())
    entered = asyncio.Event()
    release = asyncio.Event()
    posts = 0

    async def transport(request):
        nonlocal posts
        assert not store.writer_lock.locked()
        if request.method == "GET":
            return httpx.Response(
                200, json={"models": [{"slug": "synthetic-model-v1", "visibility": "list"}]}
            )
        posts += 1
        if posts == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200, content=stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = ChatGPTPlanAdapter(path, policy(), client=client)
        bot = AiService(store, plan_adapter=adapter)
        evaluation = AiService(store, plan_adapter=adapter, evaluation_prefix="eval:synthetic:")
        task = asyncio.create_task(attempt(bot, "normal-one"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            assert (await attempt(evaluation, "eval:synthetic:one")).status == "unknown"
            assert posts == 1
        finally:
            release.set()
            await task
        assert (await attempt(evaluation, "eval:synthetic:one")).status == "ready"
        assert posts == 2
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(ai_plan_invocations))
            == 2
        )


@pytest.mark.parametrize(
    ("pool", "key"),
    [
        ("bot", "eval:synthetic:one"),
        ("bot", "eval:other:one"),
        ("evaluation", "normal-one"),
        ("evaluation", "eval:other:one"),
        ("evaluation", "eval:synthetic-other:one"),
        ("evaluation", "eval:Synthetic:one"),
    ],
)
async def test_pool_spoofing_is_rejected_before_network_ledger_or_measurement(
    store, catalog, tmp_path, pool, key
):
    async with plan_pair(store, tmp_path) as (bot, evaluation, calls, _):
        with pytest.raises(ValueError, match="request key is invalid"):
            await attempt(evaluation if pool == "evaluation" else bot, key)
        assert calls == []
    async with store.engine.connect() as connection:
        for table in (ai_requests, ai_plan_invocations, ai_metrics, ai_attempts):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0


async def test_reserved_evaluation_prefix_rejected_even_with_disabled_provider(store):
    with pytest.raises(ValueError, match="request key is invalid"):
        await attempt(AiService(store), "eval:synthetic:one")
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(ai_metrics)) == 0


async def test_caps_reset_at_utc_day_boundary_without_changing_legacy_history(
    store, catalog, tmp_path, monkeypatch
):
    fixed = datetime.combine(TODAY, datetime.min.time(), UTC) + timedelta(hours=23, minutes=59)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz)

    monkeypatch.setattr("nutrition_bot.application.ai_service.datetime", Clock)
    async with plan_pair(
        store, tmp_path, daily_invocation_limit=1, daily_evaluation_invocation_limit=1
    ) as (bot, evaluation, calls, _):
        assert (await attempt(bot, "normal-one")).status == "ready"
        assert (await attempt(evaluation, "eval:synthetic:one")).status == "ready"
        assert (await attempt(bot, "normal-two")).status == "quota"
        assert (await attempt(evaluation, "eval:synthetic:two")).status == "quota"
        fixed += timedelta(minutes=2)
        assert (await attempt(bot, "normal-two")).status == "ready"
        assert (await attempt(evaluation, "eval:synthetic:two")).status == "ready"
    assert calls.count("POST") == 4
    async with store.engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.select(ai_plan_invocations.c.day, sa.func.count())
                .group_by(ai_plan_invocations.c.day)
                .order_by(ai_plan_invocations.c.day)
            )
        ).all()
        assert rows == [(TODAY.isoformat(), 2), ((TODAY + timedelta(days=1)).isoformat(), 2)]
