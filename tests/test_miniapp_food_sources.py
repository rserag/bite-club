"""Synthetic source discovery and exact-preview measured meals through the durable API."""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import food_source_cache, food_versions, meals
from nutrition_bot.adapters.database.schema_ui import ui_flows
from nutrition_bot.application.food_catalog import FoodCatalog
from nutrition_bot.domain.food_source import FoodCandidate, ProviderError, SourceDocument
from tests.test_food_catalog import FakeProvider, source_document
from tests.test_miniapp_server import client as client
from tests.test_miniapp_server import food as food
from tests.test_miniapp_server import headers
from tests.test_telegram_meals import current


@pytest.fixture
def source_provider(service, store):
    provider = FakeProvider(
        documents={"111": source_document(name="Synthetic carrots, raw", preparation="raw")},
        candidates=(
            FoodCandidate(
                source_id="111",
                name="Synthetic carrots, raw",
                data_type="Foundation",
                preparation_hint="raw",
            ),
        ),
    )
    service.food_catalog = FoodCatalog(store, provider)
    return provider


async def preview(client):
    response = await client.get("/api/food-sources/111", headers=headers())
    assert response.status == 200
    return await response.json()


def source_meal(service, snapshot, food):
    return {
        "request_id": str(uuid4()),
        "action": "source_meal",
        "label": "Lunch",
        "day": datetime.now(ZoneInfo(service.settings.app_timezone)).date().isoformat(),
        "items": [
            {"version_id": food.version_id, "grams": "100"},
            {
                "source_id": "111",
                "hash": snapshot["content_sha256"],
                "preparation": "raw",
                "grams": "80",
            },
        ],
    }


async def test_source_search_preview_and_mixed_meal_are_reviewed_durable_and_idempotent(
    client, service, store, food, source_provider
):
    denied = await client.get("/api/food-sources?q=carrot")
    assert denied.status == 401
    assert source_provider.search_calls == []
    search = await client.get("/api/food-sources?q=carrot&preparation=raw", headers=headers())
    assert search.status == 200
    assert (await search.json())["remote"][0]["name"] == "Synthetic carrots, raw"
    displayed = await preview(client)
    assert displayed["document"]["record"]["preparation"] == "raw"
    assert any(item["per_100g"] is None for item in displayed["nutrients"])
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0
    data = source_meal(service, displayed, food)
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 200
    assert await service.process_one()
    assert not await service.process_one()
    meal = await current(store)
    assert len(meal.items) == 2
    assert meal.items[0].food_version_id == food.version_id
    assert meal.items[1].food_name == "Synthetic carrots, raw"
    assert meal.items[1].preparation == "raw"
    assert meal.items[1].provenance.external_id == "111"
    assert meal.items[1].edible_milligrams == 80000
    assert all(item.quantity_method == "measured" for item in meal.items)
    assert source_provider.fetch_calls == ["111"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 2
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 1


@pytest.mark.parametrize("invalid", ["hash", "expired", "preparation"])
async def test_changed_expired_or_wrong_preparation_source_cannot_save_partial_meal(
    client, service, store, food, source_provider, invalid
):
    displayed = await preview(client)
    data = source_meal(service, displayed, food)
    if invalid == "hash":
        data["items"][1]["hash"] = "0" * 64
    elif invalid == "preparation":
        data["items"][1]["preparation"] = "cooked"
    else:
        async with store.write() as connection:
            await connection.execute(sa.update(food_source_cache).values(expires_at=0))
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert await service.process_one()
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0


async def test_source_failure_is_explicit_and_discovery_calls_are_bounded(client, source_provider):
    source_provider.search_error = ProviderError("timeout")
    response = await client.get("/api/food-sources?q=carrot", headers=headers())
    assert response.status == 200
    result = await response.json()
    assert result["source_status"] == "timeout"
    assert result["remote"] == []
    for _ in range(11):
        assert (await client.get("/api/food-sources?q=carrot", headers=headers())).status == 200
    assert (await client.get("/api/food-sources?q=carrot", headers=headers())).status == 429
    assert len(source_provider.search_calls) == 12


async def test_adding_source_only_never_records_consumption(
    client, service, store, food, source_provider
):
    displayed = await preview(client)
    data = {
        "request_id": str(uuid4()),
        "action": "source_add",
        "source": {
            "source_id": "111",
            "hash": displayed["content_sha256"],
            "preparation": "raw",
            "provider": "usda",
        },
    }
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert await service.process_one()
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 2
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0


async def test_label_handoff_keeps_exact_mixed_context_without_logging_and_retries_once(
    client, service, store, food, source_provider
):
    displayed = await preview(client)
    data = source_meal(service, displayed, food)
    data["action"] = "label_handoff"
    data["day"] = (
        datetime.now(ZoneInfo(service.settings.app_timezone)).date() - timedelta(days=1)
    ).isoformat()
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 200
    queued = await (
        await client.get("/api/requests/" + data["request_id"], headers=headers())
    ).json()
    assert queued["status"] == "pending"
    assert queued["replies"] == []
    assert await service.process_one()
    assert not await service.process_one()
    receipt = await (
        await client.get("/api/requests/" + data["request_id"], headers=headers())
    ).json()
    assert receipt["status"] == "done"
    assert receipt["replies"][0]["continue_in_chat"] is True
    async with store.engine.connect() as connection:
        flow = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert flow["stage"] == "label_name"
        context = flow["payload"]["continuation_payload"]
        assert context["date"] == data["day"]
        assert context["label"] == "Lunch"
        assert context["items"][0] == {
            "food_id": food.version_id,
            "name": food.record.name,
            "quantity": "100g",
        }
        assert context["items"][1]["quantity"] == "80g"
        assert context["items"][1]["provider"] == "usda"
        assert context["items"][1]["source_id"] == "111"
        assert context["items"][1]["hash"] == displayed["content_sha256"]
        assert context["items"][1]["preparation"] == "raw"
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0
    assert source_provider.fetch_calls == ["111"]


async def test_rejected_label_handoff_has_no_chat_acceptance_or_partial_state(
    client, service, store, food, source_provider
):
    displayed = await preview(client)
    data = source_meal(service, displayed, food)
    data["action"] = "label_handoff"
    data["items"][1]["hash"] = "0" * 64
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert await service.process_one()
    receipt = await (
        await client.get("/api/requests/" + data["request_id"], headers=headers())
    ).json()
    assert receipt["status"] == "done"
    assert all(reply["continue_in_chat"] is False for reply in receipt["replies"])
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0
        assert await connection.scalar(sa.select(sa.func.count()).select_from(ui_flows)) == 0


async def test_label_handoff_leaves_room_for_label_food_and_accepts_empty_meal(
    client, service, store, food
):
    day = datetime.now(ZoneInfo(service.settings.app_timezone)).date().isoformat()
    data = {
        "request_id": str(uuid4()),
        "action": "label_handoff",
        "day": day,
        "items": [{"version_id": food.version_id, "grams": "100"}] * 10,
    }
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 400
    data["items"] = []
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert await service.process_one()
    receipt = await (
        await client.get("/api/requests/" + data["request_id"], headers=headers())
    ).json()
    assert receipt["replies"][0]["continue_in_chat"] is True
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0


async def test_later_invalid_source_rolls_back_earlier_new_food_in_same_meal(
    client, service, store, food, source_provider
):
    displayed = await preview(client)
    data = source_meal(service, displayed, food)
    data["items"] = [
        data["items"][1],
        {"source_id": "222", "hash": "0" * 64, "preparation": "raw", "grams": "80"},
    ]
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert await service.process_one()
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0


async def test_barcode_provider_and_identity_survive_exact_source_review(
    client, service, store, food, source_provider
):
    code = "5901234123457"
    document = SourceDocument.model_validate(
        source_document(
            source_id=code, name="Synthetic packaged food", preparation="as_sold"
        ).model_dump()
        | {"provider": "openfoodfacts", "data_type": "Branded"}
    )
    barcode_provider = FakeProvider(documents={code: document})
    service.food_catalog.providers["openfoodfacts"] = barcode_provider
    invalid = await client.get(
        "/api/food-sources/5901234123458?provider=openfoodfacts", headers=headers()
    )
    assert invalid.status == 400
    assert barcode_provider.fetch_calls == []
    response = await client.get(
        "/api/food-sources/" + code + "?provider=openfoodfacts", headers=headers()
    )
    assert response.status == 200
    displayed = await response.json()
    assert displayed["document"]["provider"] == "openfoodfacts"
    data = {
        "request_id": str(uuid4()),
        "action": "source_add",
        "source": {
            "source_id": code,
            "hash": displayed["content_sha256"],
            "preparation": "as_sold",
            "provider": "openfoodfacts",
        },
    }
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert await service.process_one()
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 2
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0
        assert (
            await connection.scalar(
                sa.select(food_versions.c.source_kind).where(
                    food_versions.c.name == "Synthetic packaged food"
                )
            )
            == "openfoodfacts"
        )


async def test_shared_local_search_matches_words_latest_version_brand_and_preparation(
    client, service, store, food
):
    from nutrition_bot.adapters.database.foods import publish_reviewed_food
    from tests.test_food_storage import reviewed_food

    async with store.write() as connection:
        first = await publish_reviewed_food(
            connection,
            reviewed_food(
                name="Synthetic carrots, raw", brand="Synthetic market", preparation="raw"
            ),
        )
        latest = await publish_reviewed_food(
            connection,
            reviewed_food(
                name="Synthetic carrots, raw",
                brand="Synthetic market",
                preparation="raw",
                nutrients=[{"code": "energy", "amount": "42", "unit": "kcal"}],
            ),
            food_id=first.food_id,
        )
        await publish_reviewed_food(
            connection, reviewed_food(name="Synthetic carrots, cooked", preparation="cooked")
        )
    response = await client.get("/api/foods?q=market%20carrot%20raw", headers=headers())
    matches = (await response.json())["foods"]
    assert [item["version_id"] for item in matches] == [latest.version_id]
    filtered = await client.get("/api/foods?q=carrot&preparation=cooked", headers=headers())
    assert all(item["preparation"] == "cooked" for item in (await filtered.json())["foods"])
