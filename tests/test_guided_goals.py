"""Synthetic setup steps never assume an equation input or activate a plan."""

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.schema_goals import goal_proposals, target_plans
from nutrition_bot.adapters.database.schema_ui import ui_flows
from nutrition_bot.application.service import Service
from tests.helpers import message
from tests.test_goals import goal_press
from tests.test_navigation import tap
from tests.test_telegram_meals import process


async def count(store, table):
    async with store.engine.connect() as connection:
        return await connection.scalar(sa.select(sa.func.count()).select_from(table))


async def setup_to_activity(service, store, *, mode="Loss", coefficient="Coefficient +5"):
    start = await process(service, store, message(1, "/goal setup"))
    weight = await process(service, store, tap(start, mode, 2))
    assert "reference weight" in weight["payload"]["text"]
    height = await process(service, store, message(3, "80 kg"))
    assert "height" in height["payload"]["text"]
    age = await process(service, store, message(4, "180 cm"))
    assert "age" in age["payload"]["text"]
    coefficient_prompt = await process(service, store, message(5, "35"))
    assert "explicitly" in coefficient_prompt["payload"]["text"]
    activity = await process(service, store, tap(coefficient_prompt, coefficient, 6))
    assert "Nothing is selected automatically" in activity["payload"]["text"]
    return start, coefficient_prompt, activity


async def test_inputs_review_then_normal_proposal_requires_exact_apply(service, store):
    _, _, activity = await setup_to_activity(service, store)
    rate = await process(service, store, tap(activity, "1.6 · Typical", 7))
    assert "kg per week" in rate["payload"]["text"]
    review = await process(service, store, message(8, "0.4 kg/week"))
    assert "Equation coefficient: +5" in review["payload"]["text"]
    assert "total activity 1.6" in review["payload"]["text"]
    assert await count(store, goal_proposals) == await count(store, target_plans) == 0
    proposal = await process(service, store, tap(review, "Create reviewed proposal", 9))
    assert "2350 kcal" in proposal["payload"]["text"]
    assert "goal_proposal_id" in proposal["payload"]
    assert await count(store, goal_proposals) == 1
    assert await count(store, target_plans) == 0
    await process(service, store, goal_press(proposal, update_id=10))
    assert await count(store, target_plans) == 1
    await process(service, store, goal_press(proposal, update_id=11))
    assert await count(store, target_plans) == 1


async def test_maintenance_derives_zero_rate_from_explicit_mode_only(service, store):
    _, _, activity = await setup_to_activity(
        service, store, mode="Maintenance", coefficient="Coefficient -161"
    )
    review = await process(service, store, tap(activity, "1.4 · Lower", 7))
    assert "Intended rate: 0 kg/week" in review["payload"]["text"]
    proposal = await process(service, store, tap(review, "Create reviewed proposal", 8))
    assert "maintenance" in proposal["payload"]["text"]
    async with store.engine.connect() as connection:
        saved = (await connection.execute(sa.select(goal_proposals))).mappings().one()
        assert saved["target_rate_grams_per_week"] == 0
        assert saved["inputs"]["rmr_coefficient"] == -161
        assert saved["inputs"]["activity_factor"] == "1.4"
    assert await count(store, target_plans) == 0


async def test_old_choices_and_cancel_cannot_mutate_new_setup(service, store):
    start, coefficient, _ = await setup_to_activity(service, store)
    await process(service, store, message(7, "/goal setup"))
    stale = await process(service, store, tap(coefficient, "Coefficient -161", 8))
    assert "changed or expired" in stale["payload"]["text"]
    cancel = await process(service, store, tap(start, "Cancel setup", 9))
    assert "changed or expired" in cancel["payload"]["text"]
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert row["stage"] == "goal_mode"
        assert row["payload"] == {}
    assert await count(store, goal_proposals) == 0


async def test_invalid_rate_keeps_current_step_and_no_proposal(service, store):
    _, _, activity = await setup_to_activity(service, store)
    await process(service, store, tap(activity, "1.6 · Typical", 7))
    rejected = await process(service, store, message(8, "2 kg/week"))
    assert "0.25–0.5%" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert row["stage"] == "goal_rate"
        assert "rate" not in row["payload"]
    assert await count(store, goal_proposals) == 0


@pytest.mark.parametrize("value", ["0", "301 kg", "NaN", "80.1234 kg", "80 kg extra", "9" * 1000])
async def test_invalid_reference_weight_cannot_advance_or_invent_fields(service, store, value):
    start = await process(service, store, message(1, "/goal setup"))
    await process(service, store, tap(start, "Loss", 2))
    response = await process(service, store, message(3, value))
    assert "30 to 300" in response["payload"]["text"]
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert row["stage"] == "goal_weight"
        assert row["payload"] == {"mode": "loss"}
    assert await count(store, goal_proposals) == 0


async def test_setup_survives_restart_without_assuming_pending_coefficient(
    service, store, settings
):
    start = await process(service, store, message(1, "/goal setup"))
    await process(service, store, tap(start, "Gain", 2))
    restarted = Service(store, settings)
    response = await process(restarted, store, message(3, "80"))
    assert "height" in response["payload"]["text"]
    async with store.engine.connect() as connection:
        payload = (await connection.execute(sa.select(ui_flows.c.payload))).scalar_one()
        assert payload == {"mode": "gain", "weight": "80"}


async def test_changed_input_review_button_cannot_create_old_proposal(service, store):
    _, _, activity = await setup_to_activity(service, store, mode="Maintenance")
    review = await process(service, store, tap(activity, "1.6 · Typical", 7))
    await process(service, store, tap(review, "Change inputs", 8))
    stale = await process(service, store, tap(review, "Create reviewed proposal", 9))
    assert "changed or expired" in stale["payload"]["text"]
    assert await count(store, goal_proposals) == await count(store, target_plans) == 0


async def test_manual_goal_command_remains_available_and_cancels_wizard(service, store):
    await process(service, store, message(1, "/goal setup"))
    proposal = await process(
        service,
        store,
        message(
            2, "/goal manual mode=maintenance weight=80kg rate=0 calories=2400 protein=150g fat=70g"
        ),
    )
    assert "Manual targets" in proposal["payload"]["text"]
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert row["stage"] == "idle"
    assert await count(store, target_plans) == 0
