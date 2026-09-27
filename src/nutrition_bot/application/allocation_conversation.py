from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.allocations import create_allocation, resolve_allocation
from nutrition_bot.adapters.database.goals import current_plan
from nutrition_bot.application.meal_conversation import MealReply

HELP = (
    "Training allocation moves calories within one future Monday–Sunday week.\n"
    "/allocation YYYY-MM-DD rest gym bjj rest double unknown rest shift=100\n"
    "Supply seven explicit day types and your chosen shift (0–300 kcal, multiples of 4). "
    "Unknown days keep baseline targets. Gym/BJJ have one planning share; double has two. "
    "The shift is a redistribution preference, not estimated exercise calories. "
    "Review the exact dated preview before applying its proposal ID."
)


async def handle_allocation_message(
    connection: AsyncConnection, text: str, *, today: date, action_key: str
) -> MealReply | None:
    parts = text.casefold().split()
    if not parts or parts[0] != "/allocation":
        return None
    try:
        if len(parts) == 3 and parts[1] in {"apply", "cancel"}:
            if not parts[2].startswith("a") or not parts[2][1:].isdigit():
                raise ValueError("Use the exact A-number from the displayed preview.")
            result = await resolve_allocation(
                connection, int(parts[2][1:]), parts[1], today=today, action_key=action_key
            )
            return MealReply(result, "allocation_result")
        if len(parts) != 10 or not parts[-1].startswith("shift="):
            return MealReply(HELP, "allocation_help")
        week = date.fromisoformat(parts[1])
        proposal = await create_allocation(
            connection,
            week=week,
            today=today,
            kinds=parts[2:9],
            step_kcal=int(parts[9].split("=", 1)[1]),
            action_key=action_key,
        )
        baseline = await current_plan(connection, on_date=week, include_allocation=False)
        assert baseline is not None
        lines = [f"Review allocation A{proposal['id']} · baseline T{baseline.id}"]
        for index, (kind, delta) in enumerate(
            zip(proposal["kinds"], proposal["deltas"], strict=True)
        ):
            lines.append(
                f"{week + timedelta(days=index)} · {kind}: {baseline.energy_kcal + delta} kcal "
                f"({delta:+d}) · C {baseline.carbohydrate_grams + delta // 4} g"
            )
        lines.extend(
            [
                f"Every day: P {baseline.protein_grams} g · F {baseline.fat_grams} g.",
                f"Weekly calories: {baseline.energy_kcal * 7} → {baseline.energy_kcal * 7} kcal.",
                "Planned types are not completed sessions. No exercise-calorie add-back. "
                "Once the week starts, its allocation is fixed to preserve history and budget.",
                f"Approve exactly this preview: /allocation apply A{proposal['id']}",
                f"Cancel before the week starts: /allocation cancel A{proposal['id']}",
            ]
        )
        return MealReply("\n".join(lines), "allocation_proposal")
    except ValueError as exc:
        return MealReply(
            f"{exc}\nNothing changed. Use /allocation for the format.", "allocation_rejected"
        )
