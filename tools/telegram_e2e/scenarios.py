"""Live scenarios collected only by the explicit runner, never by default pytest."""

from datetime import UTC, datetime, timedelta

import pytest

from tools.telegram_e2e.driver import Driver, Message
from tools.telegram_e2e.environment import Environment

pytestmark = pytest.mark.asyncio


async def nutrient_report(driver: Driver, command: str) -> str:
    result = await driver.send(command, "Nutrient review")
    parts = [result.text]
    for _ in range(20):
        footer = result.text.splitlines()[-1]
        if " More: " not in footer:
            return "\n".join(parts)
        result = await driver.send(footer.split(" More: ", 1)[1], "Nutrient review")
        parts.append(result.text)
    raise AssertionError("nutrient_report_pagination_did_not_end")


async def test_nutrient_coverage(e2e: tuple[Driver, Environment]) -> None:
    driver, env = e2e
    await driver.send("/nutrients", "No group is selected automatically")
    await driver.send("/nutrients references", "Bundled set 1")
    await driver.send("/nutrients references", "Bundled set 1")
    assert env.read("SELECT count(*) FROM nutrient_reference_sets") == [(1,)]
    assert env.read("SELECT count(*) FROM nutrient_reference_values") == [(136,)]
    today = datetime.now(UTC).date()
    end = today - timedelta(days=today.weekday() + 1)
    command = f"/nutrients review 1 male-31-50 {end}"
    empty = await nutrient_report(driver, command)
    assert "W1=0/7, W2=0/7" in empty and "Possible intake gap" not in empty
    # Synthetic rice has no reviewed micronutrient values. Complete food logs
    # must not turn those unknowns into a positive gap, even with enough days.
    for offset in (0, 1, 2, 3, 4, 7, 8, 9, 10, 11):
        day = end - timedelta(days=offset)
        await driver.send(f"/meal {day} 100g rice", "Saved M")
        daily = await driver.send(f"/today {day}", "Daily food log")
        await env.delivered(daily)
        await driver.click(daily, "All food logged", "All food logged for this date")
    review = await nutrient_report(driver, command)
    assert "W1=5/7, W2=5/7" in review
    assert "magnesium · RDA 420 mg/day · total: Coverage uncertain" in review
    assert "Possible intake gap" not in review
    assert "AI/limit/UL: not used to infer an intake gap" in review
    ledger(env, 10, 10)


def contains(message: Message, *parts: str) -> None:
    for part in parts:
        assert part in message.text, f"Expected visible content: {part}"


def ledger(env: Environment, meals: int, revisions: int) -> None:
    assert env.read("SELECT count(*) FROM meals") == [(meals,)], "Unexpected meal count"
    assert env.read("SELECT count(*) FROM meal_revisions") == [(revisions,)], (
        "Unexpected revision count"
    )


async def test_smoke_start_status(e2e: tuple[Driver, Environment]) -> None:
    driver, env = e2e
    await driver.send("/start", "What would you like to do?")
    status = await driver.send("/status", "Nutrition diary is running")
    await env.delivered(status)
    await driver.click(status, "Refresh status", "Nutrition diary is running")
    await driver.send("/start", "What would you like to do?")
    assert env.read("SELECT count(*) FROM profile") == [(1,)]


async def test_smoke_measured_and_unknown(e2e: tuple[Driver, Environment]) -> None:
    driver, env = e2e
    receipt = await driver.send("150g rice; 200g chicken", "Saved M1r1")
    contains(receipt, "Energy: 550 kcal", "P: 55.0 g", "C: unknown", "F: 0.0 g")
    today = await driver.send("/today", "550")
    contains(today, "unknown")
    ledger(env, 1, 1)


async def test_rough_revision_and_repeat_approval(e2e: tuple[Driver, Environment]) -> None:
    driver, env = e2e
    first = await driver.send("about 150g rice", "Not in your totals")
    contains(first, "150", "rice")
    assert "Approve estimate" in first.buttons
    ledger(env, 0, 0)
    await driver.send("/today", "No meals logged")
    await env.delivered(first)
    second = await driver.send("item 1: about 120g", "Not in your totals", reply_to=first)
    contains(second, "120")
    await driver.click(first, "Approve estimate", "Old button")
    ledger(env, 0, 0)
    await env.delivered(second)
    saved = await driver.click(second, "Approve estimate", "Saved")
    contains(saved, "120", "estimate")
    await driver.click(second, "Approve estimate", "already saved")
    ledger(env, 1, 1)
    assert env.read(
        "SELECT edible_milligrams,quantity_method,approval_draft_revision FROM meal_items"
    ) == [(120000, "approved_estimate", 2)]


async def test_reply_correction(e2e: tuple[Driver, Environment]) -> None:
    driver, env = e2e
    receipt = await driver.send("150g rice", "Saved M1r1")
    await env.delivered(receipt)
    changed = await driver.send("rice was 120g", "Updated M1r2", reply_to=receipt)
    contains(changed, "120 g", "120 kcal")
    ledger(env, 1, 2)


async def test_original_edit_guidance(e2e: tuple[Driver, Environment]) -> None:
    driver, env = e2e
    receipt = await driver.send("100g rice", "Saved M1r1")
    original = driver.last_sent
    assert original is not None
    await env.delivered(receipt)
    guidance = await driver.edit(original, "200g rice", "diary unchanged")
    contains(guidance, "100 g rice")
    ledger(env, 1, 1)
    assert env.read("SELECT edible_milligrams FROM meal_items") == [(100000,)]


async def test_delete_and_undo(e2e: tuple[Driver, Environment]) -> None:
    driver, env = e2e
    receipt = await driver.send("100g rice", "Saved M1r1")
    await env.delivered(receipt)
    more = await driver.click(receipt, "More", "More meal actions")
    await env.delivered(more)
    deleted = await driver.click(more, "Delete", "Deleted")
    await driver.send("/today", "No meals logged")
    await env.delivered(deleted)
    await driver.click(deleted, "Restore meal", "100 g rice")
    await driver.send("/today", "100")
    ledger(env, 1, 3)


async def test_restart(e2e: tuple[Driver, Environment]) -> None:
    driver, env = e2e
    receipt = await driver.send("100g rice", "Saved M1r1")
    await env.delivered(receipt)
    await env.settled()
    await env.stop()
    await env.start()
    await driver.send("/today", "100")
    ledger(env, 1, 1)


async def supplement_report(driver: Driver, command: str = "/supplements today") -> str:
    result = await driver.send(command, "Supplement nutrition")
    parts = [result.text]
    for _ in range(20):
        footer = result.text.splitlines()[-1]
        if " More: " not in footer:
            return "\n".join(parts)
        result = await driver.send(footer.split(" More: ", 1)[1], "Supplement nutrition")
        parts.append(result.text)
    raise AssertionError("supplement_report_pagination_did_not_end")


async def test_supplement_reporting(e2e: tuple[Driver, Environment]) -> None:
    driver, env = e2e
    await driver.send("100g rice", "Saved M1r1")
    await driver.send(
        "/supplement product add E2E Creatine A | 5 g | 1 scoop | aliases: e2e alpha",
        "Saved reviewed product P1",
    )
    await driver.send(
        "/supplement product add E2E Creatine B | 5 g | 1 scoop | aliases: e2e beta",
        "Saved reviewed product P2",
    )
    first = await driver.send("/supplement take 1 serving | e2e alpha", "Saved supplement S1r1")
    await driver.send("/supplement take 1 serving | e2e beta", "Saved supplement S2r1")
    report = await supplement_report(driver)
    assert "Creatine monohydrate: 10000 mg" in report
    assert "occurs in 2 logged products" in report
    assert "F 100 kcal" in report and "C 100 kcal" in report
    assert "Upper limits not assessed" in report
    await env.delivered(first)
    deleted = await driver.click(first, "Delete", "Deleted from current supplement history")
    report = await supplement_report(driver, "/supplements week")
    assert "Creatine monohydrate: 5000 mg" in report
    assert "occurs in 2 logged products" not in report
    await env.delivered(deleted)
    await driver.click(deleted, "Undo", "Updated supplement S1r3")
    assert "Creatine monohydrate: 10000 mg" in await supplement_report(driver)


async def test_supplement_adherence(e2e: tuple[Driver, Environment]) -> None:
    from datetime import datetime

    driver, env = e2e
    today = datetime.now(UTC).date()
    preview = await driver.send(f"/supplement plan loading {today} 5 3", "Review plan")
    await env.delivered(preview)
    await driver.click(
        preview, "Approve plan — no listed concerns", "Plan approved. No dose was logged."
    )
    report = await supplement_report(driver)
    assert "4 scheduled; 0 taken" in report and "4 unconfirmed" in report
    assert "No active-ingredient doses logged" in report
    await driver.send("/supplement dose R1r1 1 taken", "Today dose 1: taken")
    await driver.send("/supplement dose R1r1 2 skipped", "Today dose 2: skipped")
    report = await supplement_report(driver)
    assert "4 scheduled; 1 taken (1 at planned amount); 1 skipped; 2 unconfirmed" in report
    assert "Creatine monohydrate: 5000 mg" in report
    await driver.send("/supplement dose R1r1 1 unconfirmed", "Today dose 1: unconfirmed")
    await driver.send("creatine 5 g", "Saved supplement")
    report = await supplement_report(driver, "/supplements week")
    assert "4 scheduled; 0 taken (0 at planned amount); 1 skipped; 3 unconfirmed" in report
    assert "Creatine monohydrate: 5000 mg" in report  # Direct log is not matched to a slot.
