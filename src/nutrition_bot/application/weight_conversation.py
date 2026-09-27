import re
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.weights import (
    WeightError,
    WeightSnapshot,
    create_weight,
    daily_weights,
    get_weight,
    recent_weights,
    revise_weight,
    undo_weight,
)
from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.domain.weight_trends import summarize_weight

REFERENCE = r"W([1-9][0-9]*)(?:r([1-9][0-9]*))?"
MEASUREMENT = re.compile(
    r"(?:(today|yesterday|[0-9]{4}-[0-9]{2}-[0-9]{2})\s+)?"
    r"([0-9]+(?:\.[0-9]+)?)\s*(?:kg)?(?:\s+(morning|unspecified))?",
    re.IGNORECASE,
)


def _kg(grams: int | Fraction) -> str:
    value = (
        Decimal(grams.numerator) / grams.denominator
        if isinstance(grams, Fraction)
        else Decimal(grams)
    ) / 1000
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize())


def _parse_measurement(text: str, today: date) -> tuple[date, int, str, bool, bool]:
    matched = MEASUREMENT.fullmatch(text.strip())
    if matched is None:
        raise WeightError("Use a weight such as 'weight 79.4' or '/weight yesterday 79.4 morning'.")
    token = (matched[1] or "today").casefold()
    if token == "today":
        day = today
    elif token == "yesterday":
        day = today - timedelta(days=1)
    else:
        try:
            day = date.fromisoformat(token)
        except ValueError:
            raise WeightError("Use today, yesterday, or a valid YYYY-MM-DD date.") from None
        if day > today:
            raise WeightError("Future weight measurements cannot be logged.")
    weight = Decimal(matched[2])
    grams_decimal = weight * 1000
    if grams_decimal != grams_decimal.to_integral_value() or not Decimal(
        20000
    ) <= grams_decimal <= Decimal(500000):
        raise WeightError("Weight must be 20–500 kg with no more than three decimal places.")
    timing_token = matched[3].casefold() if matched[3] else "unspecified"
    return day, int(grams_decimal), timing_token, matched[1] is not None, matched[3] is not None


def _receipt(snapshot: WeightSnapshot, lead: str = "Saved weight") -> MealReply:
    text = (
        f"{lead} W{snapshot.id}r{snapshot.revision_number} · {snapshot.local_date} · "
        f"{_kg(snapshot.weight_grams)} kg" + (" · morning" if snapshot.timing == "morning" else "")
    )
    if snapshot.deleted:
        text += "\nDeleted from trend calculations; revision history is retained."
    text += (
        f"\nCorrect with /weight edit W{snapshot.id}r{snapshot.revision_number} 79.4"
        if not snapshot.deleted
        else "\nUse Undo to restore the previous revision."
    )
    buttons = ("edit", "delete", "undo") if not snapshot.deleted else ("undo",)
    return MealReply(
        text,
        "weight_receipt",
        buttons=buttons,
        weight_id=snapshot.id,
        weight_revision_id=snapshot.revision_id,
    )


async def _trend(connection: AsyncConnection, today: date) -> str:
    points = await daily_weights(connection, start=today - timedelta(days=27), end=today)
    trend = summarize_weight(points, as_of=today)
    if trend.latest is None:
        return "No weight measurements yet. Send 'weight 79.4' to log one."
    lines = [
        f"Latest: {_kg(trend.latest.grams)} kg on {trend.latest.day} "
        f"({trend.latest.measurement_count} measurement"
        f"{'s' if trend.latest.measurement_count != 1 else ''} that day"
        f"{' · morning preferred' if trend.latest.used_morning else ''})."
    ]
    if trend.rolling_7d_grams is None:
        lines.append(
            f"7-day average: unavailable ({trend.rolling_dates}/7 measured dates; need 4)."
        )
    else:
        lines.append(
            f"7-day average: {_kg(trend.rolling_7d_grams)} kg "
            f"({trend.rolling_dates}/7 measured dates)."
        )
    if trend.weekly_rate_grams is None:
        lines.append(
            f"Multiweek rate: unavailable ({trend.trend_dates} measured dates over "
            f"{trend.trend_span_days} days; need at least 7 dates spanning 14 days)."
        )
    else:
        sign = "+" if trend.weekly_rate_grams > 0 else ""
        lines.append(
            f"Robust 28-day rate: {sign}{_kg(trend.weekly_rate_grams)} kg/week "
            f"({trend.trend_dates} measured dates over {trend.trend_span_days} days)."
        )
    lines.append(
        "This trend is descriptive; target adjustments require additional complete intake evidence."
    )
    return "\n".join(lines)


async def handle_weight_message(
    connection: AsyncConnection,
    message_text: str,
    *,
    action_key: str,
    today: date,
    timezone: str,
    event_time: datetime,
    source_chat_id: int,
    source_message_id: int,
) -> MealReply | None:
    text = message_text.strip()
    lowered = text.casefold()
    natural = lowered.startswith("weight ")
    command = lowered == "/weight" or lowered.startswith("/weight ")
    if not (natural or command):
        return None
    try:
        body = text.split(maxsplit=1)[1] if " " in text else ""
        if not body:
            return MealReply(await _trend(connection, today), "weight_report")
        if body.casefold() == "history":
            rows = await recent_weights(connection)
            if not rows:
                return MealReply("No weight measurements yet.", "weight_history")
            return MealReply(
                "Recent weight entries:\n"
                + "\n".join(
                    f"W{row.id}r{row.revision_number} · {row.local_date} · "
                    f"{_kg(row.weight_grams)} kg"
                    + (" · morning" if row.timing == "morning" else "")
                    + (" · deleted" if row.deleted else "")
                    for row in rows
                ),
                "weight_history",
            )
        view = re.fullmatch(REFERENCE, body, re.IGNORECASE)
        if view:
            return _receipt(await get_weight(connection, int(view[1])), "Weight entry")
        mutation = re.fullmatch(
            r"(edit|delete|undo)\s+" + REFERENCE + r"(?:\s+(.+))?",
            body,
            re.IGNORECASE,
        )
        if mutation:
            current = await get_weight(connection, int(mutation[2]))
            if mutation[3] is None or current.revision_number != int(mutation[3]):
                raise WeightError("Use the current W…r… reference from /weight history.")
            operation, replacement = mutation[1].casefold(), mutation[4]
            if operation == "edit":
                if not replacement:
                    raise WeightError("Add the corrected weight after the W…r… reference.")
                day, grams, timing, date_explicit, timing_explicit = _parse_measurement(
                    replacement, today
                )
                updated = await revise_weight(
                    connection,
                    current.id,
                    current.revision_id,
                    action_key=action_key,
                    weight_grams=grams,
                    local_date=day if date_explicit else None,
                    timing=timing if timing_explicit else None,
                )
            elif operation == "delete":
                if replacement:
                    raise WeightError("Delete does not accept extra text.")
                updated = await revise_weight(
                    connection,
                    current.id,
                    current.revision_id,
                    action_key=action_key,
                    delete=True,
                )
            else:
                if replacement:
                    raise WeightError("Undo does not accept extra text.")
                updated = await undo_weight(
                    connection, current.id, current.revision_id, action_key=action_key
                )
            return _receipt(updated, "Updated weight")
        day, grams, timing, _, _ = _parse_measurement(body, today)
        measured_at = (
            event_time.timestamp()
            if day == today
            else datetime.combine(day, time(12), event_time.tzinfo).timestamp()
        )
        saved = await create_weight(
            connection,
            action_key=action_key,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            local_date=day,
            measured_at=measured_at,
            timezone=timezone,
            weight_grams=grams,
            timing=timing,
        )
        receipt = _receipt(saved)
        return MealReply(
            receipt.text + "\n\n" + await _trend(connection, today),
            receipt.kind,
            buttons=receipt.buttons,
            weight_id=receipt.weight_id,
            weight_revision_id=receipt.weight_revision_id,
        )
    except WeightError as exc:
        return MealReply(f"{exc}\nNothing changed.", "weight_rejected")


async def handle_weight_callback(
    connection: AsyncConnection,
    action: str,
    weight_id: int,
    revision_id: int,
    *,
    action_key: str,
) -> MealReply:
    try:
        current = await get_weight(connection, weight_id)
        if current.revision_id != revision_id:
            return _receipt(current, "That button belongs to an older receipt. Current weight")
        if action == "edit":
            return _receipt(
                current,
                f"Use /weight edit W{current.id}r{current.revision_number} "
                "followed by the corrected value.",
            )
        if action == "delete":
            current = await revise_weight(
                connection,
                current.id,
                current.revision_id,
                action_key=action_key,
                delete=True,
            )
        elif action == "undo":
            current = await undo_weight(
                connection, current.id, current.revision_id, action_key=action_key
            )
        else:
            raise WeightError("Unknown weight action.")
        return _receipt(current, "Updated weight")
    except WeightError as exc:
        return MealReply(f"{exc}\nNothing changed.", "weight_rejected")
