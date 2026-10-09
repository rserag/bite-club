"""Synthetic source discovery acceptance with no external requests or nutrition claims."""

import asyncio
import hashlib
import json
import time

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import actions, food_source_cache, food_versions, outbox
from nutrition_bot.adapters.database.schema_food_discovery import food_lookup_jobs
from nutrition_bot.adapters.database.schema_ui import ui_flows
from nutrition_bot.application.food_catalog import FoodCatalog, accept_cached_source, document_hash
from nutrition_bot.application.service import Service
from nutrition_bot.domain.food_source import FoodCandidate, ProviderError, SourceDocument
from tests.helpers import message
from tests.test_food_catalog import FakeProvider, source_document
from tests.test_navigation import tap
from tests.test_telegram_drafts import press as approve
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import current, food_record, ledger_counts, process


@pytest.fixture
def provider():
    return FakeProvider(
        documents={"111": source_document(name="Synthetic carrots, raw", preparation="raw")},
        candidates=(
            FoodCandidate(
                source_id="111",
                name="Synthetic carrots, raw",
                data_type="Foundation",
                preparation_hint="raw",
            ),
            FoodCandidate(
                source_id="222",
                name="Synthetic carrots, cooked, boiled",
                data_type="SR Legacy",
                preparation_hint="cooked",
            ),
        ),
    )


@pytest.fixture
def discovery_service(store, settings, provider):
    return Service(store, settings, food_catalog=FoodCatalog(store, provider))


async def food_result(service, store):
    assert await service.process_food_lookup()
    async with store.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    sa.select(outbox)
                    .join(actions)
                    .where(actions.c.kind == "food_lookup_result")
                    .order_by(outbox.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    row = dict(row)
    row["telegram_message_id"] = 1000 + row["id"]
    await service.finish_reply(row["id"], "sent", message_id=row["telegram_message_id"])
    return row


async def preview_flow(service, store, text="80g raw carrot"):
    await process(service, store, message(1, text))
    choices = await food_result(service, store)
    assert choices is not None
    choice = next(
        button["text"] for button in choices["payload"]["buttons"] if "raw" in button["text"]
    )
    await process(service, store, tap(choices, choice, 2))
    return await food_result(service, store)


async def test_raw_carrot_discovery_does_not_use_cooked_local_match(
    discovery_service, store, provider
):
    async with store.write() as connection:
        await publish_reviewed_food(
            connection, food_record("Synthetic carrots, cooked, boiled", energy="100")
        )
    preview = await preview_flow(discovery_service, store)
    assert "80g" in preview["payload"]["text"]
    assert "2023-11-14" in preview["payload"]["text"]
    assert provider.search_calls == ["raw carrot"]
    assert await ledger_counts(store) == (0, 0)
    update = tap(preview, "Confirm food & log", 3)
    receipt = await process(discovery_service, store, update)
    saved = await current(store)
    assert "Saved meal" in receipt["payload"]["text"]
    assert saved.items[0].edible_milligrams == 80_000
    assert saved.items[0].preparation == "raw"
    assert saved.items[0].provenance.external_id == "111"
    assert saved.local_date.isoformat() == "2023-11-14"
    await discovery_service.accept([update])
    assert not await discovery_service.process_one()
    assert await ledger_counts(store) == (1, 1)


async def test_saved_cooked_result_does_not_hide_remote_raw_option(discovery_service, store):
    async with store.write() as connection:
        await publish_reviewed_food(
            connection, food_record("Synthetic carrots, cooked, boiled", energy="100")
        )
    home = await process(discovery_service, store, message(1, "/home"))
    await process(discovery_service, store, tap(home, "Log food", 2))
    immediate = await process(discovery_service, store, message(3, "carrot"))
    assert "cooked, boiled" in immediate["payload"]["text"]
    remote = await food_result(discovery_service, store)
    assert "Synthetic carrots, raw" in remote["payload"]["text"]
    assert "Synthetic carrots, cooked, boiled" in remote["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)


async def test_other_measured_items_and_date_survive_discovery(
    discovery_service, store, catalog, provider
):
    preview = await preview_flow(
        discovery_service, store, "yesterday Lunch: 150g rice; 80g raw carrot"
    )
    assert "150g rice" in preview["payload"]["text"]
    assert "2023-11-13" in preview["payload"]["text"]
    assert provider.search_calls == ["raw carrot"]
    await process(discovery_service, store, tap(preview, "Confirm food & log", 3))
    saved = await current(store)
    assert saved.label == "Lunch"
    assert saved.local_date.isoformat() == "2023-11-13"
    assert [item.edible_milligrams for item in saved.items] == [150_000, 80_000]
    assert saved.items[0].food_version_id == catalog["rice"].version_id


async def test_unresolved_preparation_renders_full_current_log_confirmation(
    discovery_service, store, catalog, provider
):
    provider.documents["111"] = source_document(name="Synthetic carrots", preparation="unspecified")
    preview = await preview_flow(
        discovery_service, store, "yesterday Lunch: 150g rice; 80g raw carrot"
    )
    assert "Preparation is unresolved" in preview["payload"]["text"]
    reviewed = await process(discovery_service, store, tap(preview, "Raw", 3))
    text = reviewed["payload"]["text"]
    assert "Preparation: raw" in text
    assert "Source: USDA FoodData Central 111" in text
    assert "Nutrients per 100 g:" in text
    assert "The source did not specify preparation" in text
    assert "80g" in text and "150g rice" in text and "2023-11-13" in text
    assert "Confirm food & log" in [button["text"] for button in reviewed["payload"]["buttons"]]
    assert await ledger_counts(store) == (0, 0)
    assert provider.fetch_calls == ["111"]

    stale = await process(discovery_service, store, tap(preview, "Raw", 4))
    assert "changed or expired" in stale["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    await process(discovery_service, store, tap(reviewed, "Confirm food & log", 5))
    saved = await current(store)
    assert saved.label == "Lunch"
    assert saved.local_date.isoformat() == "2023-11-13"
    assert [item.edible_milligrams for item in saved.items] == [150_000, 80_000]
    assert saved.items[0].food_version_id == catalog["rice"].version_id
    assert saved.items[1].preparation == "raw"


async def test_cancelled_pending_job_never_calls_provider(discovery_service, store, provider):
    await process(discovery_service, store, message(1, "80g raw carrot"))
    await process(discovery_service, store, message(2, "/cancel"))
    assert await discovery_service.process_food_lookup()
    assert provider.search_calls == []
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(food_lookup_jobs))).mappings().one()
        assert row["status"] == "cancelled" and row["request"] is None
    assert await ledger_counts(store) == (0, 0)


async def test_slow_search_keeps_fast_commands_responsive_and_discards_cancelled_result(
    store, settings, provider
):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(FakeProvider):
        async def search(self, query):
            entered.set()
            await release.wait()
            return provider.candidates

    service = Service(store, settings, food_catalog=FoodCatalog(store, Slow()))
    await process(service, store, message(1, "80g raw carrot"))
    work = asyncio.create_task(service.process_food_lookup())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        today = await asyncio.wait_for(process(service, store, message(2, "/today")), 1)
        assert today is not None
        release.set()
        assert await asyncio.wait_for(work, 1)
    finally:
        release.set()
        await work
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(actions)
                .where(actions.c.kind == "food_lookup_result")
            )
            == 0
        )
    assert await ledger_counts(store) == (0, 0)


async def test_lookup_restart_recovers_interrupted_job(
    discovery_service, store, settings, provider
):
    await process(discovery_service, store, message(1, "80g raw carrot"))
    async with store.write() as connection:
        await connection.execute(
            sa.update(food_lookup_jobs).values(
                status="running", attempts=1, lease_until=time.time() + 999
            )
        )
    restarted = Service(store, settings, food_catalog=FoodCatalog(store, provider))
    await restarted.recover_food_lookups()
    choices = await food_result(restarted, store)
    assert "Synthetic carrots, raw" in choices["payload"]["text"]
    assert provider.search_calls == ["raw carrot"]


async def test_changed_source_preview_fails_without_saving_food_or_meal(
    discovery_service, store, provider
):
    preview = await preview_flow(discovery_service, store)
    provider.documents["111"] = source_document(
        name="Synthetic carrots, raw",
        preparation="raw",
        source_reference="Synthetic changed revision",
    )
    await discovery_service.food_catalog.lookup("111", refresh=True)
    rejected = await process(discovery_service, store, tap(preview, "Confirm food & log", 3))
    assert "changed or expired" in rejected["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 0


async def test_mixed_source_late_failure_rolls_back_all_published_foods(discovery_service, store):
    preview = await discovery_service.food_catalog.lookup("111")
    text = "/source-meal " + json.dumps(
        {
            "date": "2023-11-14",
            "items": [
                {
                    "source_id": "111",
                    "hash": preview.content_sha256,
                    "preparation": "raw",
                    "grams": "80",
                },
                {"source_id": "111", "hash": "0" * 64, "preparation": "raw", "grams": "80"},
            ],
        }
    )
    rejected = await process(discovery_service, store, message(1, text))
    assert "nothing was saved" in rejected["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 0


async def test_rough_amount_requires_exact_draft_approval(discovery_service, store):
    home = await process(discovery_service, store, message(1, "/home"))
    await process(discovery_service, store, tap(home, "Log food", 2))
    await process(discovery_service, store, message(3, "raw carrot"))
    choices = await food_result(discovery_service, store)
    await process(
        discovery_service, store, tap(choices, choices["payload"]["buttons"][0]["text"], 4)
    )
    preview = await food_result(discovery_service, store)
    await process(discovery_service, store, tap(preview, "Use this food", 5))
    review = await process(discovery_service, store, message(6, "about 80g"))
    draft = await process(discovery_service, store, tap(review, "Review estimate", 7))
    assert "Not in your totals" in draft["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    await process(
        discovery_service, store, approve(draft, update_id=8, callback_id="synthetic-food-approve")
    )
    saved = await current(store)
    assert saved.items[0].quantity_method == "approved_estimate"
    assert saved.items[0].approval_draft_revision == 1


async def test_offline_saved_choice_still_logs_and_does_not_fetch(service, store, catalog):
    home = await process(service, store, message(1, "/home"))
    await process(service, store, tap(home, "Log food", 2))
    choices = await process(service, store, message(3, "rice"))
    await process(service, store, tap(choices, choices["payload"]["buttons"][0]["text"], 4))
    review = await process(service, store, message(5, "80g"))
    await process(service, store, tap(review, "Confirm & log", 6))
    assert await ledger_counts(store) == (1, 1)
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(food_lookup_jobs)) == 0
        )


async def test_source_timeout_keeps_saved_choices_and_never_selects_by_itself(
    discovery_service, store, provider
):
    async with store.write() as connection:
        await publish_reviewed_food(
            connection,
            food_record("Synthetic carrots, raw", energy="100").model_copy(
                update={"preparation": "raw"}
            ),
        )
    provider.search_error = ProviderError("timeout")
    home = await process(discovery_service, store, message(1, "/home"))
    await process(discovery_service, store, tap(home, "Log food", 2))
    await process(discovery_service, store, message(3, "raw carrot"))
    result = await food_result(discovery_service, store)
    assert "Saved" in result["payload"]["text"]
    assert "unavailable" in result["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)


async def test_pre_provider_field_usda_preview_hash_still_selects_exact_snapshot(store):
    document = source_document(name="Synthetic carrots, raw", preparation="raw")
    legacy_document = document.model_dump(mode="json", exclude={"provider"})
    legacy_hash = hashlib.sha256(
        json.dumps(legacy_document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert document_hash(document) == legacy_hash
    now = time.time()
    async with store.write() as connection:
        await connection.execute(
            sa.insert(food_source_cache).values(
                provider="usda",
                external_id="111",
                content_sha256=legacy_hash,
                document=legacy_document,
                fetched_at=now,
                expires_at=now + 86400,
            )
        )
        food = await accept_cached_source(connection, "111", legacy_hash, "raw", now=now)
    assert food.record.name == "Synthetic carrots, raw"
    assert food.provenance.external_id == "111"


async def test_initial_rough_mass_discovers_source_without_sending_amount(
    discovery_service, store, provider
):
    preview = await preview_flow(discovery_service, store, "about 80g raw carrot")
    assert "about 80g" in preview["payload"]["text"]
    assert provider.search_calls == ["raw carrot"]
    review = await process(discovery_service, store, tap(preview, "Use this food", 3))
    assert "about 80g" in review["payload"]["text"]
    draft = await process(discovery_service, store, tap(review, "Review estimate", 4))
    assert "Not in your totals" in draft["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)


async def test_replacing_search_term_keeps_previously_reported_mass(
    discovery_service, store, provider
):
    await process(discovery_service, store, message(1, "80g carrot"))
    await process(discovery_service, store, message(2, "raw carrot"))
    choices = await food_result(discovery_service, store)
    await process(
        discovery_service, store, tap(choices, choices["payload"]["buttons"][0]["text"], 3)
    )
    preview = await food_result(discovery_service, store)
    assert "80g" in preview["payload"]["text"]
    assert "Confirm food & log" in [button["text"] for button in preview["payload"]["buttons"]]
    assert provider.search_calls == ["raw carrot"]


async def test_barcode_preview_cache_and_source_add_keep_provider_provenance(store, settings):
    document = SourceDocument(
        source_id="12345670",
        provider="openfoodfacts",
        record=source_document(name="Synthetic packaged label", preparation="as_sold").record,
        data_type="packaged label",
        adapter_version="synthetic-off-v1",
    )
    provider = FakeProvider(documents={"12345670": document})
    catalog = FoodCatalog(store, None, providers={"openfoodfacts": provider})
    service = Service(store, settings, food_catalog=catalog)
    await process(service, store, message(1, "/barcode 12345670"))
    preview = await food_result(service, store)
    assert "Open Food Facts" in preview["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    cached = await catalog.lookup("12345670", provider="openfoodfacts", offline=True)
    assert cached.document == document
    assert provider.fetch_calls == ["12345670"]
    saved = await process(
        service,
        store,
        message(2, f"/source-add 12345670 {cached.content_sha256} as_sold openfoodfacts"),
    )
    assert "no meal was logged" in saved["payload"]["text"]
    async with store.engine.connect() as connection:
        version = (await connection.execute(sa.select(food_versions))).mappings().one()
        assert (
            version["source_kind"] == "openfoodfacts"
            and version["source_external_id"] == "12345670"
        )
    assert await ledger_counts(store) == (0, 0)


async def test_background_results_keep_original_saved_choice_and_activity(discovery_service, store):
    async with store.write() as connection:
        await publish_reviewed_food(
            connection, food_record("Synthetic carrots, cooked", energy="100")
        )
    home = await process(discovery_service, store, message(1, "/home"))
    await process(discovery_service, store, tap(home, "Log food", 2))
    immediate = await process(discovery_service, store, message(3, "carrot"))
    async with store.engine.connect() as connection:
        before = (await connection.execute(sa.select(ui_flows))).mappings().one()
    await food_result(discovery_service, store)
    async with store.engine.connect() as connection:
        after = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert (after["revision"], after["updated_at"]) == (
            before["revision"],
            before["updated_at"],
        )
    amount = await process(
        discovery_service, store, tap(immediate, immediate["payload"]["buttons"][0]["text"], 4)
    )
    assert "How much" in amount["payload"]["text"]
    assert "expired" not in amount["payload"]["text"]


async def test_source_label_handoff_preserves_items_date_and_publishes_nothing(
    discovery_service, store, catalog
):
    preview = await discovery_service.food_catalog.lookup("111")
    command = "/source-label " + json.dumps(
        {
            "label": "Lunch",
            "date": "2023-11-13",
            "items": [
                {"version_id": catalog["rice"].version_id, "grams": "150"},
                {
                    "source_id": "111",
                    "hash": preview.content_sha256,
                    "preparation": "raw",
                    "grams": "80",
                },
            ],
        }
    )
    response = await process(discovery_service, store, message(1, command))
    assert response["payload"]["label_handoff"] is True
    async with store.engine.connect() as connection:
        flow = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert flow["stage"] == "label_name"
        context = flow["payload"]["continuation_payload"]
        assert context["label"] == "Lunch" and context["date"] == "2023-11-13"
        assert [item["quantity"] for item in context["items"]] == ["150g", "80g"]
        assert context["items"][1]["hash"] == preview.content_sha256
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 3
    assert await ledger_counts(store) == (0, 0)


async def test_source_label_handoff_rejects_changed_preview_without_success_marker(
    discovery_service, store
):
    await discovery_service.food_catalog.lookup("111")
    command = "/source-label " + json.dumps(
        {
            "label": "Lunch",
            "date": "2023-11-13",
            "items": [
                {"source_id": "111", "hash": "0" * 64, "preparation": "raw", "grams": "80"},
            ],
        }
    )
    response = await process(discovery_service, store, message(1, command))
    assert "label_handoff" not in response["payload"]
    assert "nothing was saved" in response["payload"]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(ui_flows)) == 0
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 0
    assert await ledger_counts(store) == (0, 0)
