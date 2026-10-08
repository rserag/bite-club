import json
import time
from uuid import uuid4

import pytest
import sqlalchemy as sa
from aiohttp.test_utils import TestClient, TestServer

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import cursor, inbox, meal_revisions, meals, outbox
from nutrition_bot.miniapp.server import RequestCommand, create_app, update_id
from tests.helpers import message
from tests.test_food_storage import reviewed_food
from tests.test_miniapp_auth import signed


@pytest.fixture
async def client(service):
    value = TestClient(TestServer(create_app(service)))
    await value.start_server()
    yield value
    await value.close()


def headers():
    return {"X-Telegram-Init-Data": signed(timestamp=int(time.time()))}


def command(food, **changes):
    return {
        "request_id": str(uuid4()),
        "action": "meal",
        "items": [{"version_id": food.version_id, "grams": "150"}],
        **changes,
    }


@pytest.fixture
async def food(store):
    async with store.write() as connection:
        return await publish_reviewed_food(connection, reviewed_food())


async def test_static_no_credentials_and_api_requires_signed_owner(client, food):
    page = await client.get("/")
    assert page.status == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert "synthetic_token" not in await page.text()
    for path in ["/api/dashboard", "/api/foods", "/api/meals"]:
        response = await client.get(path)
        assert response.status == 401
        assert "synthetic" not in await response.text()
    query = await client.get("/api/foods?initData=secret", headers=headers())
    assert query.status == 400


async def test_browser_data_keeps_unknown_and_escapes_search(client, food):
    data = await (await client.get("/api/dashboard", headers=headers())).json()
    assert data["meal_count"] == 0
    assert data["status"] == "unknown"
    assert all(item["known"] is None for item in data["nutrients"])
    assert all(item["energy"] is None for item in data["energy_history"])
    catalog = await (await client.get("/api/foods", headers=headers())).json()
    assert catalog["foods"][0]["version_id"] == food.version_id
    values = {item["code"]: item for item in catalog["foods"][0]["nutrients"]}
    assert values["protein"]["per_100g"] == "10"
    assert values["vitamin_d"]["per_100g"] is None
    literal = await (await client.get("/api/foods?q=%25", headers=headers())).json()
    assert literal["foods"] == []


async def test_submission_is_durable_idempotent_and_does_not_touch_poll_cursor(
    client, food, service, store
):
    await service.accept([message(123, "/start")])
    assert await service.process_one()
    before = await service.offset()
    data = command(food)
    first = await client.post("/api/commands", json=data, headers=headers())
    assert first.status == 202
    duplicate = await client.post("/api/commands", json=data, headers=headers())
    assert duplicate.status == 200
    assert await service.offset() == before
    assert await service.process_one()
    assert not await service.process_one()
    receipt = await (
        await client.get("/api/requests/" + data["request_id"], headers=headers())
    ).json()
    assert receipt["status"] == "done"
    assert "Saved" in receipt["replies"][0]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 1
        assert await connection.scalar(sa.select(cursor.c.next_offset)) == before
    history = await (await client.get("/api/meals", headers=headers())).json()
    assert history["meals"][0]["items"][0]["grams"] == "150"


async def test_no_cursor_is_created_for_first_internal_request(client, food, service, store):
    response = await client.post("/api/commands", json=command(food), headers=headers())
    assert response.status == 202
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(cursor.c.next_offset)) is None
    assert await service.offset() == 0


async def test_same_uuid_cannot_authorize_changed_payload(client, food, store):
    data = command(food)
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    data["items"][0]["grams"] = "200"
    response = await client.post("/api/commands", json=data, headers=headers())
    assert response.status == 409
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(inbox)) == 1


async def test_old_revision_edit_is_rejected_without_second_ledger(client, food, service, store):
    data = command(food)
    await client.post("/api/commands", json=data, headers=headers())
    assert await service.process_one()
    first = (await (await client.get("/api/meals", headers=headers())).json())["meals"][0]
    edit = command(food, action="edit", reference=first["reference"])
    edit["items"][0]["grams"] = "120"
    assert (await client.post("/api/commands", json=edit, headers=headers())).status == 202
    assert await service.process_one()
    stale = command(food, action="edit", reference=first["reference"])
    stale["items"][0]["grams"] = "999"
    await client.post("/api/commands", json=stale, headers=headers())
    assert await service.process_one()
    receipt = await (
        await client.get("/api/requests/" + stale["request_id"], headers=headers())
    ).json()
    assert "current receipt" in receipt["replies"][0]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meal_revisions)) == 2


async def test_retained_receipt_is_plain_data_not_reflected_script(client, service, store):
    identifier = str(uuid4())
    await service.accept_internal([message(update_id(identifier, 101), "/start")])
    assert await service.process_one()
    async with store.write() as connection:
        await connection.execute(
            sa.update(outbox).values(payload={"text": "<script>alert('synthetic')</script>"})
        )
    response = await client.get("/api/requests/" + identifier, headers=headers())
    assert response.content_type == "application/json"
    assert "<script>" in (await response.json())["replies"][0]["text"]
    source = await (await client.get("/assets/app.js")).text()
    assert "innerHTML" not in source


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "approve"},
        {"items": []},
        {"items": [{"version_id": 1, "grams": "0"}]},
        {"items": [{"version_id": 1, "grams": "-1"}]},
        {"items": [{"version_id": 1, "grams": "1.0001"}]},
        {"items": [{"version_id": True, "grams": "1"}]},
        {"items": [{"version_id": 1, "grams": "50001"}]},
        {"action": "delete", "reference": "M1"},
        {"request_id": str(uuid4()).upper()},
        {"arbitrary": "secret"},
    ],
)
async def test_invalid_mutations_are_bounded_rejected_and_not_enqueued(
    client, food, store, changes
):
    data = command(food, **changes)
    response = await client.post("/api/commands", json=data, headers=headers())
    assert response.status == 400
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(inbox)) == 0


async def test_json_limits_type_and_uuid_namespace(client, food):
    assert update_id(str(uuid4()), 101) < -(1 << 61)
    response = await client.post("/api/commands", data="a" * 17000, headers=headers())
    assert response.status in {413, 415}
    malformed = await client.post(
        "/api/commands",
        data=json.dumps({"token": "synthetic"}),
        headers=headers() | {"Content-Type": "application/json"},
    )
    assert malformed.status == 400
    missing = await client.get("/api/requests/" + str(uuid4()), headers=headers())
    assert missing.status == 404


def test_compiled_edit_requires_current_reference_and_measured_exact_grams():
    value = RequestCommand.model_validate(
        {
            "request_id": str(uuid4()),
            "action": "edit",
            "reference": "M1r2",
            "items": [{"version_id": 2, "grams": "125.001"}],
        }
    )
    assert value.command() == "/edit M1r2 replace: 125.001g #2"
