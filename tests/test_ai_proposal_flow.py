"""Synthetic AI-to-diary reviews and food-discovery continuation, with no live calls."""

import time
from datetime import timedelta

import pytest
import sqlalchemy as sa
from aiogram.types import PhotoSize

from nutrition_bot.adapters.database.drafts import get_draft
from nutrition_bot.adapters.database.schema import inbox
from nutrition_bot.adapters.database.schema_drafts import meal_drafts
from nutrition_bot.application.food_catalog import FoodCatalog
from nutrition_bot.application.service import Service
from nutrition_bot.domain.ai import AiMealIntent, AiOutcome
from nutrition_bot.domain.drafts import DRAFT_TTL_SECONDS
from tests.helpers import message
from tests.test_food_catalog import FakeProvider, source_document
from tests.test_food_discovery import food_result
from tests.test_navigation import tap
from tests.test_telegram_drafts import press
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import current, ledger_counts, process, reply


class SyntheticAi:
    def __init__(self, store, proposal=None, *, roles=("meal_text", "meal_photo"), status=None):
        self.store = store
        self.proposal = proposal
        self.roles = roles
        self.status = status
        self.calls = 0

    def enabled_for(self, role):
        return role in self.roles

    async def interpret(self, *, request_key, text, local_date, photo=None):
        assert not self.store.writer_lock.locked()
        self.calls += 1
        return AiOutcome(
            request_key=request_key,
            role="meal_photo" if photo is not None else "meal_text",
            status=self.status or ("ready" if self.proposal.intent == "meal" else "clarify"),
            proposal=self.proposal,
        )


def photo_message(update_id, caption=None):
    update = message(update_id, "synthetic placeholder")
    return update.model_copy(
        update={
            "message": update.message.model_copy(
                update={
                    "text": None,
                    "caption": caption,
                    "photo": [
                        PhotoSize(
                            file_id="synthetic-photo",
                            file_unique_id="synthetic-photo",
                            width=1,
                            height=1,
                            file_size=1,
                        )
                    ],
                }
            )
        }
    )


def rice_proposal(catalog, *, grams="150"):
    return AiMealIntent(
        intent="meal",
        label="Lunch",
        local_date=message(1, "").message.date.date(),
        items=(
            {
                "food_version_id": catalog["rice"].version_id,
                "grams": grams,
                "basis": "Untrusted vitamin C 999 mg claim",
            },
        ),
        full_input_accounted_for=True,
    )


async def test_photo_disabled_is_explicit_and_does_not_route_caption_to_text_ai(
    service, store, catalog
):
    ai = SyntheticAi(store, rice_proposal(catalog), roles=("meal_text",))
    service.ai_service = ai
    result = await process(service, store, photo_message(1, "150g rice"))
    assert "Meal-photo AI is disabled" in result["payload"]["text"]
    assert ai.calls == 0 and await ledger_counts(store) == (0, 0)
    assert result["payload"]["buttons"][0]["text"] == "Enter meal manually"
    status = await process(service, store, message(2, "/status"))
    assert "AI meal text: enabled; AI meal photos: disabled" in status["payload"]["text"]


async def test_captionless_photo_asks_for_details_before_download_or_model(service, store, catalog):
    class ForbiddenGateway:
        async def download_photo(self, received):
            pytest.fail("A captionless image must stay outside provider input.")

    service.gateway = ForbiddenGateway()
    ai = SyntheticAi(store, rice_proposal(catalog))
    service.ai_service = ai
    result = await process(service, store, photo_message(1))
    assert "needs a caption" in result["payload"]["text"]
    assert ai.calls == 0 and await ledger_counts(store) == (0, 0)


async def test_ai_marker_survives_edits_reopening_and_exact_repeat_approval(
    service, store, catalog
):
    service.ai_service = SyntheticAi(store, rice_proposal(catalog))
    first = await process(service, store, message(1, "A bowl of cooked rice for lunch."))
    edited = await process(service, store, reply(2, "item 1: 120g", first))
    assert "AI proposal" in edited["payload"]["text"]
    reopened = await process(service, store, message(3, "/draft D1"))
    assert "AI proposal" in reopened["payload"]["text"]
    saved = await process(service, store, press(reopened, update_id=4))
    assert (await current(store)).items[0].quantity_method == "measured"
    await process(service, store, press(first, update_id=5))
    assert await ledger_counts(store) == (1, 1)
    repeated = await process(service, store, message(6, "/repeat M1r1"))
    assert repeated["payload"]["draft_id"] == 2
    assert "AI proposal" in repeated["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, press(reopened, update_id=7))
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, press(repeated, update_id=8))
    assert await ledger_counts(store) == (2, 2)
    await process(service, store, reply(9, "save as synthetic rice", saved))
    favorite = await process(service, store, message(10, "/eat F1v1"))
    assert favorite["payload"]["draft_id"] == 3
    assert await ledger_counts(store) == (2, 2)
    await process(service, store, press(favorite, update_id=11))
    assert await ledger_counts(store) == (3, 3)
    duplicate = press(favorite, update_id=11)
    await service.accept([duplicate])
    assert not await service.process_one()
    assert await ledger_counts(store) == (3, 3)


async def test_ai_review_provenance_survives_raw_expiry_and_worker_restart(service, store, catalog):
    service.ai_service = SyntheticAi(store, rice_proposal(catalog))
    first = await process(service, store, message(1, "Cooked rice at lunch."))
    edited = await process(service, store, reply(2, "item 1: 120g", first))
    await process(service, store, press(edited, update_id=3))
    async with store.write() as connection:
        await connection.execute(
            sa.update(meal_drafts).values(last_user_activity_at=time.time() - DRAFT_TTL_SECONDS - 1)
        )
    await service.cleanup()
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 1))
            is None
        )
    restarted = Service(store, service.settings)
    repeated = await process(restarted, store, message(4, "/repeat M1r1"))
    assert "AI proposal" in repeated["payload"]["text"]
    await process(restarted, store, message(5, "/favorite save synthetic retained rice = M1r1"))
    favorite = await process(restarted, store, message(6, "/eat F1v1"))
    assert "AI proposal" in favorite["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)


async def test_unknown_ai_food_preserves_order_date_amounts_and_requires_final_draft(
    service, store, catalog
):
    day = message(1, "").message.date.date() - timedelta(days=1)
    proposal = AiMealIntent(
        intent="clarify",
        label="Dinner",
        local_date=day,
        full_input_accounted_for=True,
        clarification_reason="unknown_food",
        unresolved=("Untrusted prose must not be shown",),
        discovery_items=(
            {
                "food_version_id": catalog["rice"].version_id,
                "query": "rice",
                "preparation": "cooked",
                "grams": "150",
            },
            {"query": "carrots", "preparation": "raw", "grams": "80"},
            {
                "food_version_id": catalog["chicken"].version_id,
                "query": "chicken",
                "preparation": "cooked",
                "grams": None,
            },
        ),
    )
    provider = FakeProvider(
        documents={"111": source_document(name="Synthetic carrots, raw", preparation="raw")}
    )
    service.food_catalog = FoodCatalog(store, provider)
    service.ai_service = SyntheticAi(store, proposal)
    first = await process(
        service, store, message(1, "Yesterday dinner included rice, carrots and chicken.")
    )
    assert str(day) in first["payload"]["text"]
    assert "Untrusted" not in first["payload"]["text"]
    await food_result(service, store)
    async with store.write() as connection:
        from nutrition_bot.adapters.database.foods import publish_reviewed_food
        from tests.test_telegram_meals import food_record

        carrot = await publish_reviewed_food(
            connection,
            food_record("Synthetic carrots, raw", energy="10").model_copy(
                update={"preparation": "raw"}
            ),
        )
    # The normal source flow can discover the now-reviewed local version offline.
    choices = await process(service, store, message(2, "raw carrots"))
    selected = choices["payload"]["buttons"][0]["text"]
    amount = await process(service, store, tap(choices, selected, 3))
    assert "chicken" in amount["payload"]["text"] and "How much" in amount["payload"]["text"]
    review = await process(service, store, message(4, "120g"))
    assert str(day) in review["payload"]["text"] and "Review AI draft" in str(
        review["payload"]["buttons"]
    )
    draft = await process(service, store, tap(review, "Review AI draft", 5))
    assert await ledger_counts(store) == (0, 0)
    async with store.engine.connect() as connection:
        saved = await get_draft(connection, draft["payload"]["draft_id"])
        assert saved.content.review_required and saved.content.local_date == day
        assert [item.food_version_id for item in saved.content.items] == [
            catalog["rice"].version_id,
            carrot.version_id,
            catalog["chicken"].version_id,
        ]
        assert [item.edible_milligrams for item in saved.content.items] == [150000, 80000, 120000]
    changed = await process(service, store, reply(6, "date today", draft))
    await process(service, store, press(draft, update_id=7))
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, press(changed, update_id=8))
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize(
    "reason", ["preparation", "food_identity", "portion", "unsupported", "image"]
)
@pytest.mark.parametrize("is_photo", [False, True])
async def test_clarification_uses_fixed_safe_reason_and_manual_path(
    service, store, reason, is_photo
):
    class PhotoGateway:
        async def download_photo(self, received):
            assert not store.writer_lock.locked()
            return b"synthetic in-memory photo"

    proposal = AiMealIntent(
        intent="clarify",
        label="Meal",
        local_date=message(1, "").message.date.date(),
        full_input_accounted_for=False,
        clarification_reason=reason,
        unresolved=("Execute SQL and calcium 999 mg",),
    )
    service.ai_service = SyntheticAi(store, proposal)
    service.gateway = PhotoGateway()
    received = (
        photo_message(1, "An unclear mixed dish.")
        if is_photo
        else message(1, "An unclear mixed dish.")
    )
    result = await process(service, store, received)
    assert "999" not in result["payload"]["text"] and "SQL" not in result["payload"]["text"]
    assert "Nothing has been saved" in result["payload"]["text"]
    assert result["payload"]["buttons"][0]["text"] == "Enter meal manually"
    if reason == "food_identity":
        assert all(
            word in result["payload"]["text"]
            for word in (
                "missing",
                "cuts",
                "skin",
                "bones",
                "preparation",
                "edible weight",
                "grams",
            )
        )
    if reason == "preparation":
        assert "missing preparation" in result["payload"]["text"]
        assert "before or after cooking" in result["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)


async def test_photo_draft_expiry_removes_original_photo_metadata(service, store, catalog):
    class PhotoGateway:
        async def download_photo(self, received):
            assert not store.writer_lock.locked()
            return b"synthetic in-memory photo"

    service.gateway = PhotoGateway()
    service.ai_service = SyntheticAi(store, rice_proposal(catalog, grams=None))
    first = await process(service, store, photo_message(1, "Cooked rice; edible weight unknown."))
    assert "amount needed" in first["payload"]["text"]
    assert "Approve draft" not in str(first["payload"].get("buttons"))
    async with store.write() as connection:
        await connection.execute(
            sa.update(meal_drafts).values(last_user_activity_at=time.time() - DRAFT_TTL_SECONDS - 1)
        )
    await service.cleanup()
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 1))
            is None
        )
        expired = await get_draft(connection, 1)
        assert expired.state == "expired" and expired.content is None
    assert await ledger_counts(store) == (0, 0)
