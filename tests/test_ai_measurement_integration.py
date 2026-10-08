"""Synthetic pipeline measurements without live calls or content-bearing telemetry."""

import asyncio
import json

import httpx
import pytest
import sqlalchemy as sa
from aiogram.types import PhotoSize

from nutrition_bot.adapters.database.schema_ai_metrics import ai_metrics
from nutrition_bot.application.ai_metrics import metrics_summary
from nutrition_bot.application.ai_service import AiService
from tests.helpers import FakeGateway, message
from tests.test_ai_drafts import TODAY, adapter_with, completion, manifest, proposed
from tests.test_telegram_drafts import press
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import ledger_counts, process, reply


async def test_local_inputs_are_counted_once_without_commands_corrections_or_unauthorized_users(
    service, store, catalog
):
    original = message(1, "150g rice")
    receipt = await process(service, store, original)
    await service.accept([original])
    assert not await service.process_one()
    await process(service, store, message(2, "/today"))
    await process(service, store, reply(3, "120g", receipt))
    await service.accept([message(4, "150g rice", user=202)])
    assert not await service.process_one()
    report = metrics_summary(service.settings.database_path)
    normal = report["sources"]["normal"]
    assert normal["requests"] == 1
    assert normal["handling"] == {"local": 1, "ai": 0}
    assert normal["inference_sent"] == 0
    assert normal["usage"]["input_tokens"]["total"] is None


async def test_measurements_follow_draft_revisions_without_recounting_approval_or_replay(
    service, store, catalog
):
    def transport(request):
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(200, json=completion(proposed(local_date=context["local_date"])))

    adapter, client = adapter_with(transport)
    service.ai_service = AiService(store, manifest(), adapter)
    try:
        first = await process(service, store, message(1, "Lunch included rice weighing 150 grams."))
        changed = await process(service, store, reply(2, "item 1: 120g", first))
        await process(service, store, press(first, update_id=3))
        assert await ledger_counts(store) == (0, 0)
        await process(service, store, press(changed, update_id=4))
        normal = metrics_summary(service.settings.database_path)["sources"]["normal"]
        assert normal["requests"] == normal["inference_sent"] == 1
        assert normal["handling"] == {"local": 0, "ai": 1}
        assert normal["drafts"]["saved_edited"] == 1
        assert normal["drafts"]["saved_unchanged"] == 0
        assert normal["usage"]["input_tokens"]["total"] == 100
        assert normal["usage"]["reasoning_tokens"]["total"] is None
        async with store.engine.connect() as connection:
            row = (await connection.execute(sa.select(ai_metrics))).mappings().one()
            assert row["initial_revision"] == 1 and row["draft_id"] == 1
        assert "Lunch included" not in json.dumps(normal)
    finally:
        await client.aclose()


async def test_schema_retry_totals_both_attempts_and_replay_preserves_first_measurement(
    store, settings, catalog
):
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        proposal = proposed(nutrients={"energy": 999}) if calls == 1 else proposed()
        return httpx.Response(200, json=completion(proposal))

    adapter, client = adapter_with(transport)
    service = AiService(store, manifest(), adapter)
    try:
        first = await service.interpret(
            request_key="synthetic-retry", text="rice", local_date=TODAY
        )
        repeated = await service.interpret(
            request_key="synthetic-retry", text="rice", local_date=TODAY
        )
        assert first == repeated and first.status == "ready" and calls == 2
        report = metrics_summary(settings.database_path)
        normal = report["sources"]["normal"]
        assert normal["requests"] == 1 and normal["attempts_sent"] == 2
        assert normal["usage"]["input_tokens"]["total"] == 200
        assert normal["usage"]["output_tokens"]["total"] == 100
        assert normal["usage"]["charged_micro_usd"]["total"] == 2000
    finally:
        await client.aclose()


async def test_missing_optional_metrics_table_cannot_break_a_measured_meal_or_ai_proposal(
    service, store, catalog
):
    async with store.write() as connection:
        await connection.exec_driver_sql("DROP TABLE ai_metrics")
    local = await process(service, store, message(1, "150g rice"))
    assert local["payload"]["meal_id"] == 1
    adapter, client = adapter_with(lambda request: httpx.Response(200, json=completion()))
    try:
        outcome = await AiService(store, manifest(), adapter).interpret(
            request_key="synthetic-no-metrics", text="rice", local_date=TODAY
        )
        assert outcome.status == "ready"
        assert await ledger_counts(store) == (1, 1)
    finally:
        await client.aclose()


async def test_cached_outcome_cannot_fabricate_zero_usage_after_a_measurement_gap(store, catalog):
    adapter, client = adapter_with(lambda request: httpx.Response(200, json=completion()))
    service = AiService(store, manifest(), adapter)
    try:
        first = await service.interpret(request_key="synthetic-gap", text="rice", local_date=TODAY)
        async with store.write() as connection:
            await connection.execute(sa.delete(ai_metrics))
        second = await service.interpret(request_key="synthetic-gap", text="rice", local_date=TODAY)
        assert first == second and first.status == "ready"
        async with store.engine.connect() as connection:
            assert await connection.scalar(sa.select(sa.func.count()).select_from(ai_metrics)) == 0
    finally:
        await client.aclose()


@pytest.mark.parametrize("missing_gateway", [False, True])
async def test_photo_download_failures_count_without_inference_or_content(
    service, store, catalog, missing_gateway
):
    class PhotoAi(AiService):
        def enabled_for(self, role):
            return role == "meal_photo"

    service.ai_service = PhotoAi(store)
    service.gateway = None if missing_gateway else FakeGateway()
    update = message(1, "synthetic caption")
    update = update.model_copy(
        update={
            "message": update.message.model_copy(
                update={
                    "text": None,
                    "photo": [
                        PhotoSize(
                            file_id="synthetic", file_unique_id="synthetic", width=1, height=1
                        )
                    ],
                }
            )
        }
    )
    await process(service, store, update)
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ai_metrics))).mappings().one()
        assert row["status"] == "unavailable" and row["failure_category"] == "download"
        assert row["inference_sent"] is False and row["attempts_sent"] == 0
    assert await ledger_counts(store) == (0, 0)


async def test_cancelled_sent_attempt_is_measured_as_unknown_without_invented_usage(store, catalog):
    started = asyncio.Event()

    async def transport(request):
        started.set()
        await asyncio.Event().wait()

    adapter, client = adapter_with(transport)
    service = AiService(store, manifest(), adapter)
    try:
        task = asyncio.create_task(
            service.interpret(request_key="synthetic-cancel", text="rice", local_date=TODAY)
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with store.engine.connect() as connection:
            row = (await connection.execute(sa.select(ai_metrics))).mappings().one()
            assert row["status"] == "unknown" and row["attempts_sent"] == 1
            assert row["input_tokens"] is None and row["charged_micro_usd"] is None
    finally:
        await client.aclose()
