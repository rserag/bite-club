import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.schema_supplements import (
    supplement_intake_revisions,
    supplement_intakes,
    supplement_product_aliases,
    supplement_product_components,
    supplement_product_versions,
    supplement_products,
)
from tests.helpers import callback, message
from tests.test_telegram_meals import process


def supplement_press(receipt, operation, *, update_id=2, callback_id=None):
    value = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=callback_id or f"synthetic-supplement-{update_id}",
        message_id=receipt["telegram_message_id"],
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"supplement:{operation}:{receipt['button_token']}"
    return Update.model_validate(value)


async def test_exact_creatine_log_is_an_immutable_actual_dose(service, store):
    saved = await process(service, store, message(1, "creatine 5 g"))
    payload = saved["payload"]
    assert payload["supplement_intake_id"] == 1
    assert "Creatine monohydrate · 5 g · exact reported dose" in payload["text"]
    assert "actual intake, not a planned dose" in payload["text"]
    assert {button["text"] for button in payload["buttons"]} == {
        "Edit",
        "Delete",
        "Undo",
    }
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(supplement_intake_revisions))).mappings().one()
        assert row["amount_scaled"] == 5_000_000_000
        assert row["sealed"]
        try:
            await connection.execute(sa.update(supplement_intake_revisions).values(amount_scaled=1))
        except sa.exc.IntegrityError:
            pass
        else:
            raise AssertionError("sealed supplement revision update was not rejected")


async def test_milligram_form_and_amount_free_message_never_infer_a_dose(service, store):
    saved = await process(service, store, message(1, "I took 5000 mg creatine"))
    assert "Creatine monohydrate · 5 g" in saved["payload"]["text"]
    rejected = await process(service, store, message(2, "I took creatine"))
    assert "Include the exact creatine amount" in rejected["payload"]["text"]
    assert "No supplement data changed" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(supplement_intakes)) == 1
        )


async def test_edit_delete_undo_and_stale_receipts_append_history(service, store):
    saved = await process(service, store, message(1, "creatine 5 g"))
    edited = await process(service, store, message(2, "/supplement edit S1r1 3 g"))
    assert "S1r2" in edited["payload"]["text"]
    assert "Creatine monohydrate · 3 g" in edited["payload"]["text"]
    stale = await process(service, store, supplement_press(saved, "delete", update_id=3))
    assert "receipt is old" in stale["payload"]["text"]
    deleted = await process(service, store, supplement_press(edited, "delete", update_id=4))
    assert "Deleted from current supplement history" in deleted["payload"]["text"]
    restored = await process(service, store, supplement_press(deleted, "undo", update_id=5))
    assert "S1r4" in restored["payload"]["text"]
    assert "Creatine monohydrate · 3 g" in restored["payload"]["text"]
    async with store.engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    sa.select(supplement_intake_revisions).order_by(
                        supplement_intake_revisions.c.id
                    )
                )
            )
            .mappings()
            .all()
        )
    assert [row["operation"] for row in rows] == ["create", "edit", "delete", "undo"]
    assert [row["amount_scaled"] for row in rows] == [
        5_000_000_000,
        3_000_000_000,
        3_000_000_000,
        3_000_000_000,
    ]


async def test_history_and_original_message_edit_preserve_current_entry(service, store):
    await process(service, store, message(1, "creatine 5 g"))
    history = await process(service, store, message(2, "/supplement history"))
    assert "S1r1" in history["payload"]["text"]
    edited_original = await process(
        service,
        store,
        message(3, "creatine 9 g", edited=True).model_copy(
            update={"edited_message": message(1, "creatine 9 g", edited=True).edited_message}
        ),
    )
    assert "supplement history unchanged" in edited_original["payload"]["text"]
    assert "Creatine monohydrate · 5 g" in edited_original["payload"]["text"]


async def test_reviewed_product_alias_logs_exact_servings_and_keeps_label_snapshot(service, store):
    product = await process(
        service,
        store,
        message(
            1,
            "/supplement product add Plain Creatine | 5 g | 1 scoop | "
            "aliases: my creatine, blue tub",
        ),
    )
    assert "Saved reviewed product P1" in product["payload"]["text"]
    assert "Exact label: 5 g creatine monohydrate per 1 scoop" in product["payload"]["text"]
    logged = await process(
        service, store, message(2, "/supplement take 1.5 servings | my creatine")
    )
    assert "Plain Creatine · 1.5 serving(s) (1 scoop)" in logged["payload"]["text"]
    assert "7.5 g creatine from reviewed label" in logged["payload"]["text"]
    edited = await process(service, store, message(3, "/supplement edit S1r1 2 servings"))
    assert "10 g creatine from reviewed label" in edited["payload"]["text"]
    async with store.engine.connect() as connection:
        version = (
            (await connection.execute(sa.select(supplement_product_versions))).mappings().one()
        )
        component = (
            (await connection.execute(sa.select(supplement_product_components))).mappings().one()
        )
        assert version["sealed"] and version["source_kind"] == "manual_label"
        assert component["source_amount"] == "5000"
        assert component["amount_scaled"] == 5_000_000_000
        assert (
            await connection.scalar(
                sa.select(sa.func.count()).select_from(supplement_product_aliases)
            )
            == 3
        )
        try:
            await connection.execute(
                sa.update(supplement_product_versions).values(serving_description="changed")
            )
        except sa.exc.IntegrityError:
            pass
        else:
            raise AssertionError("sealed product label update was not rejected")


async def test_product_alias_collision_is_atomic(service, store):
    await process(
        service,
        store,
        message(1, "/supplement product add First | 5 g | scoop | aliases: mine"),
    )
    rejected = await process(
        service,
        store,
        message(2, "/supplement product add Second | 3 g | scoop | aliases: mine"),
    )
    assert "already in use" in rejected["payload"]["text"]
    assert "No supplement data changed" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(supplement_products))
            == 1
        )
