from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.schema_supplement_plans import plan_proposals
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_intakes,
    supplement_regimen_revisions,
)
from nutrition_bot.adapters.database.supplement_plans import has_weight_context
from nutrition_bot.domain.supplement_plans import CreatinePlan, protocol_version
from tests.helpers import callback, message
from tests.test_telegram_meals import process


def today(settings):
    return datetime.now(ZoneInfo(settings.app_timezone)).date()


def msg(n, text):
    value = message(n, text).model_dump(mode="json", exclude_none=True)
    value["message"]["date"] = int(datetime.now().timestamp())
    return Update.model_validate(value)


def press(receipt, n, action="approve"):
    value = callback(
        receipt["button_token"],
        update_id=n,
        callback_id=f"plan-{n}",
        message_id=receipt["telegram_message_id"],
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"supplementplan:{action}:{receipt['button_token']}"
    return Update.model_validate(value)


async def approve(service, store, settings):
    preview = await process(
        service, store, msg(1, f"/supplement plan loading {today(settings)} 5 3")
    )
    async with store.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(supplement_intakes)) == 0
    result = await process(service, store, press(preview, 2))
    assert "Plan approved. No dose was logged." in result["payload"]["text"]
    return preview


def test_frozen_weight_calendar_and_loading_optional(settings):
    start = today(settings)
    plan = CreatinePlan(
        start=start,
        timezone=settings.app_timezone,
        option="weight_loading",
        loading_days=7,
        weight_grams=79400,
        maintenance_mg=3000,
        protocol_version=protocol_version(),
    )
    assert plan.phases()[0].dose_mg == 5955
    assert plan.phase_on(start - timedelta(days=1)) is None
    assert plan.phase_on(start + timedelta(days=6))[1].name == "loading"
    assert plan.phase_on(start + timedelta(days=7))[1].dose_mg == 3000
    assert "79.4 kg" in plan.preview()
    with pytest.raises(ValueError):
        plan.weight_grams = 80000


async def test_preview_requires_button_and_cannot_replay(service, store, settings):
    preview = await approve(service, store, settings)
    replay = await process(service, store, press(preview, 3))
    assert "already resolved" in replay["payload"]["text"]
    async with store.engine.connect() as c:
        assert (
            await c.scalar(sa.select(sa.func.count()).select_from(supplement_regimen_revisions))
            == 1
        )
        assert await c.scalar(sa.select(sa.func.count()).select_from(supplement_intakes)) == 0
        with pytest.raises(sa.exc.IntegrityError):
            await c.execute(sa.update(plan_proposals).values(plan={}))


async def test_taken_skipped_clear_and_duplicate_protection(service, store, settings):
    await approve(service, store, settings)
    taken = await process(service, store, msg(3, "/supplement dose R1r1 1 taken"))
    assert "dose 1: taken · actual 5 g" in taken["payload"]["text"]
    duplicate = await process(service, store, msg(4, "/supplement dose R1r1 1 taken"))
    assert "already marked" in duplicate["payload"]["text"]
    skipped = await process(service, store, msg(5, "/supplement dose R1r1 2 skipped"))
    assert "dose 2: skipped" in skipped["payload"]["text"]
    assert "dose 3: unconfirmed" in skipped["payload"]["text"]
    clear = await process(service, store, msg(6, "/supplement dose R1r1 1 unconfirmed"))
    assert "dose 1: unconfirmed" in clear["payload"]["text"]
    async with store.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(supplement_intakes)) == 1


async def test_state_changes_stale_references_and_calorie_guard(service, store, settings):
    await approve(service, store, settings)
    paused = await process(service, store, msg(3, "/supplement pause R1r1"))
    assert "R1r2 · paused" in paused["payload"]["text"]
    stale = await process(service, store, msg(4, "/supplement resume R1r1"))
    assert "Old plan reference" in stale["payload"]["text"]
    resumed = await process(service, store, msg(5, "/supplement resume R1r2"))
    assert "R1r3 · active" in resumed["payload"]["text"]
    review = await process(service, store, msg(6, "/adjust"))
    assert "Calorie review withheld" in review["payload"]["text"]
    stopped = await process(service, store, msg(7, "/supplement stop R1r3"))
    assert "R1r4 · stopped" in stopped["payload"]["text"]
    restart = await process(service, store, msg(8, "/supplement resume R1r4"))
    assert "cannot restart" in restart["payload"]["text"]
    async with store.engine.connect() as c:
        assert await has_weight_context(c, today(settings) + timedelta(days=5))
        assert not await has_weight_context(c, today(settings) + timedelta(days=33))


async def test_cancel_and_invalid_fields_do_not_activate(service, store, settings):
    preview = await process(service, store, msg(1, f"/supplement plan steady {today(settings)} 5"))
    cancelled = await process(service, store, press(preview, 2, "cancel"))
    assert "Plan cancelled" in cancelled["payload"]["text"]
    malformed = await process(
        service, store, msg(3, f"/supplement plan weight {today(settings)} 7 5 8..0")
    )
    assert "Invalid plan fields" in malformed["payload"]["text"]
    async with store.engine.connect() as c:
        assert (
            await c.scalar(sa.select(sa.func.count()).select_from(supplement_regimen_revisions))
            == 0
        )


async def test_existing_calorie_proposal_cannot_bypass_new_creatine_context(
    service, store, settings
):
    from nutrition_bot.adapters.database.schema_adaptive import adaptive_proposals
    from tests.test_adaptive_telegram import adaptive_press, seed_pending_proposal

    await seed_pending_proposal(service, store)
    receipt = await process(service, store, msg(903, "/adjust"))
    await approve(service, store, settings)
    rejected = await process(service, store, adaptive_press(receipt, "apply", update_id=904))
    assert "calorie adjustment is withheld" in rejected["payload"]["text"]
    async with store.engine.connect() as c:
        assert await c.scalar(sa.select(adaptive_proposals.c.state)) == "pending"


async def test_expired_preview_and_wrong_callback_namespace_are_rejected(service, store, settings):
    preview = await process(service, store, msg(1, f"/supplement plan steady {today(settings)} 5"))
    value = press(preview, 2).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"status:{preview['button_token']}"
    assert await process(service, store, Update.model_validate(value)) is None
    from nutrition_bot.adapters.database.supplement_plans import resolve
    from nutrition_bot.adapters.database.supplements import SupplementError

    async with store.write() as c:
        with pytest.raises(SupplementError, match="start date has passed"):
            await resolve(c, 1, "approve", "update:1", today(settings) + timedelta(days=1))


async def test_dose_day_uses_saved_plan_timezone_and_phase_boundary(service, store, settings):
    from datetime import UTC

    from nutrition_bot.adapters.database.schema_supplement_plans import plan_dose_marks
    from nutrition_bot.adapters.database.supplement_plans import describe, mark_dose

    await approve(service, store, settings)
    await process(service, store, msg(3, "/status"))
    plan_date = today(settings)
    local_time = datetime.combine(plan_date, datetime.min.time(), ZoneInfo(settings.app_timezone))
    async with store.write() as c:
        await mark_dose(
            c,
            1,
            1,
            plan_date - timedelta(days=1),
            1,
            "taken",
            "update:3",
            local_time.astimezone(UTC),
            101,
            333,
        )
        saved_day = await c.scalar(sa.select(plan_dose_marks.c.day))
        assert saved_day == plan_date
        text = await describe(c, 1, local_time.astimezone(UTC))
        assert "dose 1: taken" in text
        transition = await describe(c, 1, plan_date + timedelta(days=5))
        assert "dose 1: unconfirmed" in transition
        assert "dose 2:" not in transition
