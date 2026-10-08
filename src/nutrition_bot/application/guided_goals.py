"""One input at a time; final proposals use the existing goal command/approval."""

import re
from datetime import datetime
from decimal import Decimal

from aiogram.types import Message
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.domain.goals import GoalError, estimate_targets

STEPS = ("mode", "weight", "height", "age", "coefficient", "activity", "rate", "review")
PROMPTS = {
    "mode": "What is your goal? Choose loss, maintenance or gain.",
    "weight": "What reference weight should this plan use? Send kilograms, e.g. 80 kg. "
    "This setup input does not create a weight measurement.",
    "height": "What is your height in centimetres? E.g. 180 cm.",
    "age": "What is your age in whole years? This starting equation supports adults 18–100.",
    "coefficient": "Choose the Mifflin–St Jeor equation coefficient explicitly: +5 or -161. "
    "This is an equation input; it is not inferred from your profile.",
    "activity": "Choose the total-activity factor for this starting estimate, including your usual "
    "training: 1.4 (lower), 1.6 (typical) or 1.8 (higher). Nothing is selected automatically.",
    "rate": "What intended rate should the proposal use, in kg per week? E.g. 0.4 kg/week. "
    "The existing goal checks will validate it against your reference weight.",
}
CHOICES = {
    "mode": (("Loss", "loss"), ("Maintenance", "maintenance"), ("Gain", "gain")),
    "coefficient": (("Coefficient +5", "+5"), ("Coefficient -161", "-161")),
    "activity": (("1.4 · Lower", "1.4"), ("1.6 · Typical", "1.6"), ("1.8 · Higher", "1.8")),
}


def _buttons(step: str, revision: int) -> tuple[tuple[str, str], ...]:
    values = tuple(
        (label, f"flow:{revision}:goal:{step}={value}") for label, value in CHOICES.get(step, ())
    )
    if step == "review":
        values = (
            ("Create reviewed proposal", f"flow:{revision}:goal:create"),
            ("Change inputs", f"flow:{revision}:goal:restart"),
        )
    return values + (("Cancel setup", f"flow:{revision}:goal:cancel"),)


def _command(values: dict[str, str]) -> str:
    rate = "0" if values["mode"] == "maintenance" else values["rate"] + "kg/week"
    return (
        f"/goal estimate mode={values['mode']} weight={values['weight']}kg "
        f"height={values['height']}cm age={values['age']} coefficient={values['coefficient']} "
        f"activity={values['activity']} rate={rate}"
    )


def _validate_complete(values: dict[str, str]) -> None:
    proposal = estimate_targets(
        mode=values["mode"],
        weight_kg=Decimal(values["weight"]),
        height_cm=Decimal(values["height"]),
        age_years=int(values["age"]),
        rmr_coefficient=int(values["coefficient"]),
        activity_factor=Decimal(values["activity"]),
        rate_kg_per_week=Decimal(values["rate"]),
    )
    if not (
        1 <= proposal.energy_kcal <= 10000
        and 20 <= proposal.protein_grams <= 500
        and 20 <= proposal.fat_grams <= 300
        and 0 <= proposal.carbohydrate_grams <= 1200
    ):
        raise GoalError("This estimate exceeds the supported plan range. Use /goal manual.")


def _review(values: dict[str, str]) -> str:
    return (
        "Review your setup inputs\n"
        f"Goal: {values['mode']} · reference weight {values['weight']} kg\n"
        f"Height: {values['height']} cm · age {values['age']} years\n"
        f"Equation coefficient: {values['coefficient']} · total activity {values['activity']}\n"
        f"Intended rate: {values['rate']} kg/week\n"
        "Create a proposal to review its calories and macros. Only Apply on that exact "
        "proposal can activate the plan. You can also use /goal manual for your own targets."
    )


async def _advance(
    connection: AsyncConnection, owner: int, step: str, values: dict[str, str]
) -> MealReply:
    from nutrition_bot.application.navigation import begin, menu

    revision = await begin(connection, owner, "goal_" + step, values)
    return menu(_review(values) if step == "review" else PROMPTS[step], _buttons(step, revision))


async def start_goal_setup(connection: AsyncConnection, owner: int) -> MealReply:
    return await _advance(connection, owner, "mode", {})


def _parse(step: str, text: str) -> str:
    value = text.strip().casefold()
    if step in CHOICES:
        if value not in {item for _, item in CHOICES[step]}:
            raise ValueError
        return value
    if step == "age":
        if not re.fullmatch(r"[0-9]{2,3}", value) or not 18 <= int(value) <= 100:
            raise ValueError
        return str(int(value))
    units = {"weight": r"\s*kg", "height": r"\s*cm", "rate": r"\s*kg/week"}
    matched = re.fullmatch(r"([0-9]{1,3}(?:\.[0-9]{1,3})?)(?:" + units[step] + r")?", value)
    if matched is None:
        raise ValueError
    number = Decimal(matched[1])
    lower, upper = {
        "weight": (Decimal("30"), Decimal("300")),
        "height": (Decimal("120"), Decimal("230")),
        "rate": (Decimal("0.001"), Decimal("3")),
    }[step]
    if not lower <= number <= upper:
        raise ValueError
    return format(number, "f")


async def _answer(
    connection: AsyncConnection, row: RowMapping, text: str, *, owner_id: int
) -> MealReply:
    from nutrition_bot.application.navigation import menu

    step = row["stage"][5:]
    if step == "review":
        return menu(
            "Use Create reviewed proposal or Change inputs.", _buttons(step, row["revision"])
        )
    try:
        value = _parse(step, text)
    except ValueError:
        bounds = {
            "weight": "Use a reference weight from 30 to 300 kg.",
            "height": "Use a height from 120 to 230 cm.",
            "age": "Use a whole age from 18 to 100 years.",
        }
        return menu(
            bounds.get(step, "Choose a valid value.") + "\n" + PROMPTS[step],
            _buttons(step, row["revision"]),
        )
    values = dict(row["payload"])
    values[step] = value
    next_step = STEPS[STEPS.index(step) + 1]
    if step == "activity" and values["mode"] == "maintenance":
        values["rate"] = "0"
        next_step = "review"
    if next_step == "review":
        try:
            _validate_complete(values)
        except GoalError as error:
            return menu(str(error) + "\n" + PROMPTS[step], _buttons(step, row["revision"]))
    return await _advance(connection, owner_id, next_step, values)


async def goal_message(
    connection: AsyncConnection, row: RowMapping, text: str, *, owner_id: int
) -> MealReply:
    return await _answer(connection, row, text, owner_id=owner_id)


async def goal_action(
    connection: AsyncConnection,
    row: RowMapping,
    operation: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
) -> MealReply:
    from nutrition_bot.application.navigation import _command as run_command
    from nutrition_bot.application.navigation import cancel, home, menu

    operation = operation.removeprefix("goal:")
    if operation == "cancel":
        await cancel(connection, owner_id)
        return home()
    if operation == "restart":
        return await start_goal_setup(connection, owner_id)
    if operation == "create" and row["stage"] == "goal_review":
        result = await run_command(
            connection,
            _command(row["payload"]),
            message,
            action_key=action_key,
            reference=reference,
            bot_id=bot_id,
            owner_id=owner_id,
            retention_days=retention_days,
        )
        if result.kind != "goal_rejected":
            await cancel(connection, owner_id)
        return result
    if "=" in operation:
        step, value = operation.split("=", 1)
        if row["stage"] == "goal_" + step and step in CHOICES:
            return await _answer(connection, row, value, owner_id=owner_id)
    return menu("That setup step has changed. Open the setup guide again.")
