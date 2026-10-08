"""Approval previews retain every synthetic food identity and estimate assumption."""

from datetime import UTC, datetime

import sqlalchemy as sa

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import outbox
from nutrition_bot.application.draft_conversation import draft_receipt
from nutrition_bot.domain.drafts import DraftContent, MealDraft, PlannedItem
from nutrition_bot.runtime.worker import send_one
from tests.helpers import FakeGateway
from tests.test_telegram_meals import food_record


async def process_parts(service, store, update):
    """Deliver a synthetic multi-message response through the real offline sender."""
    await service.accept([update])
    assert await service.process_one()
    key = (
        f"callback:{update.callback_query.id}"
        if update.callback_query
        else f"update:{update.update_id}"
    )
    async with store.engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    sa.select(outbox)
                    .where(
                        outbox.c.kind == "message",
                        sa.or_(
                            outbox.c.action_key == key,
                            outbox.c.payload["message_group"].as_string() == key,
                        ),
                    )
                    .order_by(outbox.c.id)
                )
            )
            .mappings()
            .all()
        )
    assert rows

    class PartGateway(FakeGateway):
        async def send_message(self, chat_id, text, button_token, buttons=None):
            await super().send_message(chat_id, text, button_token, buttons)
            return update.update_id * 1000 + len(self.messages)

    gateway = PartGateway()
    for _ in range(len(rows) + 10):
        if not await send_one(service, gateway):
            break
    else:
        raise AssertionError("Synthetic sender failed to drain bounded response")
    async with store.engine.connect() as connection:
        delivered = (
            (
                await connection.execute(
                    sa.select(outbox).where(outbox.c.id.in_([row["id"] for row in rows]))
                )
            )
            .mappings()
            .all()
        )
    assert all(row["status"] == "sent" for row in delivered)
    assert all(len(row["payload"]["text"].encode("utf-16-le")) // 2 <= 4096 for row in rows)
    assert all(not row["payload"].get("buttons") for row in rows[:-1])
    final = next(dict(row) for row in delivered if row["action_key"] == key)
    return final, rows


async def test_approval_keeps_full_itemized_names_quantities_and_repeated_basis(store):
    name = "Synthetic chicken, breast, skinless, boneless, cooked, roasted without added fat"
    basis = (
        "Synthetic reviewed visual portion: weighed comparison plate with the same cut, "
        "skin removed, cooked by roasting without oil; edible portion only, excluding bones."
    )
    reference = datetime(2026, 1, 15, 12, tzinfo=UTC)
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, food_record(name))
        planned = tuple(
            PlannedItem(
                food_version_id=food.version_id,
                edible_milligrams=mass,
                original_quantity=quantity,
                original_unit="g",
                estimate_basis=basis if index < 2 else None,
            )
            for index, (mass, quantity) in enumerate(
                ((150125, "150.125"), (200000, "200"), (75000, "75"))
            )
        )
        draft = MealDraft(
            id=1,
            revision=3,
            state="open",
            last_user_activity_at=reference.timestamp(),
            content=DraftContent(
                label="Synthetic lunch",
                local_date=reference.date(),
                timezone="UTC",
                consumed_at=reference.timestamp(),
                source_chat_id=101,
                source_message_id=1,
                items=planned,
            ),
        )
        rendered = await draft_receipt(connection, draft)

    assert rendered.text.count(name) == 3
    assert rendered.text.count(f"Basis: {basis}") == 2
    assert f"1. 150.125 g · {name} (cooked; #{food.version_id}; estimate)" in rendered.text
    assert f"2. 200 g · {name} (cooked; #{food.version_id}; estimate)" in rendered.text
    assert f"3. 75 g · {name} (cooked; #{food.version_id}; measured)" in rendered.text
    assert "D1r3" in rendered.text
    assert rendered.draft_revision == 3
    assert "Not in your totals" in rendered.text
    assert rendered.buttons == ("edit", "approve", "cancel")
