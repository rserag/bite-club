import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.schema_recovery import recovery_checkins, recovery_revisions
from tests.helpers import callback, message
from tests.test_telegram_meals import process


def recovery_press(receipt, operation, *, update_id=2):
    value = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=f"synthetic-recovery-{update_id}",
        message_id=receipt["telegram_message_id"],
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"recovery:{operation}:{receipt['button_token']}"
    return Update.model_validate(value)


async def test_partial_recovery_keeps_missing_unknown_and_infers_nothing(service, store):
    saved = await process(service, store, message(1, "recovery sleep 7.5h fatigue 3"))
    text = saved["payload"]["text"]
    assert "Sleep: 7.5 h" in text
    assert "Soreness: unknown" in text
    assert "Fatigue: 3/5" in text
    assert "Readiness: unknown" in text
    assert "no diagnosis or readiness score was inferred" in text
    assert {item["text"] for item in saved["payload"]["buttons"]} == {
        "Edit",
        "Delete",
        "Undo",
    }
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(recovery_revisions))).mappings().one()
    assert row["sleep_minutes"] == 450
    assert row["soreness"] is None and row["readiness"] is None


async def test_recovery_edit_is_replacement_and_delete_undo_are_immutable(service, store):
    saved = await process(service, store, message(1, "recovery soreness 4"))
    edited = await process(
        service,
        store,
        message(2, "/recovery edit R1r1 sleep 8h readiness 4"),
    )
    assert "Soreness: unknown" in edited["payload"]["text"]
    stale = await process(service, store, recovery_press(saved, "delete", update_id=3))
    assert "older receipt" in stale["payload"]["text"]
    deleted = await process(service, store, recovery_press(edited, "delete", update_id=4))
    restored = await process(service, store, recovery_press(deleted, "undo", update_id=5))
    assert "Sleep: 8 h" in restored["payload"]["text"]
    assert "Readiness: 4/5" in restored["payload"]["text"]
    async with store.engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    sa.select(recovery_revisions).order_by(recovery_revisions.c.id)
                )
            )
            .mappings()
            .all()
        )
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(recovery_checkins)) == 1
        )
    assert [row["operation"] for row in rows] == ["create", "edit", "delete", "undo"]


async def test_recovery_validation_and_one_current_checkin_per_date(service, store):
    invalid = await process(service, store, message(1, "recovery fatigue 6"))
    assert "Fatigue must be a whole number from 1 to 5" in invalid["payload"]["text"]
    await process(service, store, message(2, "recovery readiness 3"))
    duplicate = await process(service, store, message(3, "recovery sleep 7h"))
    assert "already has a recovery check-in" in duplicate["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(recovery_checkins)) == 1
        )
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(recovery_revisions)) == 1
        )
