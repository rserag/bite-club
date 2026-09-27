import re
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.recovery import (
    RecoveryError,
    RecoverySnapshot,
    create_recovery,
    get_recovery,
    recent_recovery,
    revise_recovery,
    undo_recovery,
)
from nutrition_bot.application.meal_conversation import MealReply

REFERENCE = r"R([1-9][0-9]*)r([1-9][0-9]*)"


def _parse(body: str) -> tuple[int | None, int | None, int | None, int | None]:
    normalized = re.sub(r"\s*=\s*", " ", body.casefold())
    matches = list(
        re.finditer(
            r"\b(sleep|soreness|fatigue|readiness)\s+([0-9]+(?:\.[0-9]+)?)(?:\s*(h|hr|hrs|hours?))?\b",
            normalized,
        )
    )
    if not matches:
        raise RecoveryError("Include at least one: sleep 7.5h, soreness 2, fatigue 3, readiness 4.")
    remainder = normalized
    values: dict[str, int] = {}
    for match in reversed(matches):
        remainder = remainder[: match.start()] + " " + remainder[match.end() :]
    if remainder.strip(" ,"):
        raise RecoveryError("Use only sleep, soreness, fatigue and readiness values.")
    for match in matches:
        key, raw, unit = match.groups()
        if key in values:
            raise RecoveryError(f"Provide {key} once.")
        try:
            number = Decimal(raw)
        except InvalidOperation:
            raise RecoveryError(f"{key} must be numeric.") from None
        if key == "sleep":
            if unit is None:
                raise RecoveryError("Sleep needs hours, for example sleep 7.5h.")
            minutes = number * 60
            if minutes != minutes.to_integral_value() or not 0 <= minutes <= 1440:
                raise RecoveryError("Sleep must be 0–24 hours in whole minutes.")
            values[key] = int(minutes)
        else:
            if unit is not None or number != number.to_integral_value() or not 1 <= number <= 5:
                raise RecoveryError(f"{key.capitalize()} must be a whole number from 1 to 5.")
            values[key] = int(number)
    return (
        values.get("sleep"),
        values.get("soreness"),
        values.get("fatigue"),
        values.get("readiness"),
    )


def receipt(snapshot: RecoverySnapshot, lead: str = "Saved recovery") -> MealReply:
    sleep = (
        "unknown"
        if snapshot.sleep_minutes is None
        else f"{Decimal(snapshot.sleep_minutes) / 60:g} h"
    )

    def scale(value: int | None) -> str:
        return "unknown" if value is None else f"{value}/5"

    lines = [
        f"{lead} R{snapshot.id}r{snapshot.revision_number} · {snapshot.local_date}",
        f"Sleep: {sleep} · Soreness: {scale(snapshot.soreness)} · "
        f"Fatigue: {scale(snapshot.fatigue)} · Readiness: {scale(snapshot.readiness)}",
        "These are your reported signals; no diagnosis or readiness score was inferred.",
    ]
    if snapshot.deleted:
        lines.append("Deleted from current recovery history; revisions are retained.")
    else:
        lines.append(
            f"Correct with /recovery edit R{snapshot.id}r{snapshot.revision_number} "
            "sleep 7.5h fatigue 3"
        )
    return MealReply(
        "\n".join(lines),
        "recovery_receipt",
        buttons=("edit", "delete", "undo") if not snapshot.deleted else ("undo",),
        recovery_id=snapshot.id,
        recovery_revision_id=snapshot.revision_id,
    )


async def handle_recovery_message(
    connection: AsyncConnection, text: str, *, action_key: str, today: date
) -> MealReply | None:
    stripped = text.strip()
    lowered = stripped.casefold()
    try:
        if lowered in {"/recovery", "recovery"}:
            return MealReply(
                "Log any subset: recovery sleep 7.5h soreness 2 fatigue 3 readiness 4\n"
                "Each 1–5 value is reported independently; omitted values remain unknown. "
                "Use /recovery history to correct entries.",
                "recovery_help",
            )
        if lowered == "/recovery history":
            rows = await recent_recovery(connection)
            if not rows:
                return MealReply("No recovery check-ins yet.", "recovery_history")
            return MealReply(
                "Recent recovery:\n"
                + "\n".join(
                    f"R{row.id}r{row.revision_number} · {row.local_date}"
                    + (" · deleted" if row.deleted else "")
                    for row in rows
                ),
                "recovery_history",
            )
        edit = re.fullmatch(rf"/recovery\s+edit\s+{REFERENCE}\s+(.+)", stripped, re.IGNORECASE)
        if edit:
            current = await get_recovery(connection, int(edit[1]))
            if current.revision_number != int(edit[2]):
                raise RecoveryError("Use the current R…r… reference from /recovery history.")
            values = _parse(edit[3])
            return receipt(
                await revise_recovery(
                    connection,
                    current.id,
                    current.revision_id,
                    action_key=action_key,
                    sleep_minutes=values[0],
                    soreness=values[1],
                    fatigue=values[2],
                    readiness=values[3],
                ),
                "Updated recovery",
            )
        mutation = re.fullmatch(
            rf"/recovery\s+(delete|undo)\s+{REFERENCE}", stripped, re.IGNORECASE
        )
        if mutation:
            current = await get_recovery(connection, int(mutation[2]))
            if current.revision_number != int(mutation[3]):
                raise RecoveryError("Use the current R…r… reference from /recovery history.")
            updated = (
                await revise_recovery(
                    connection, current.id, current.revision_id, action_key=action_key, delete=True
                )
                if mutation[1].casefold() == "delete"
                else await undo_recovery(
                    connection, current.id, current.revision_id, action_key=action_key
                )
            )
            return receipt(updated, "Updated recovery")
        matched = re.match(r"^/?recovery\b\s+(.+)$", stripped, re.IGNORECASE)
        if matched is None:
            return None
        values = _parse(matched[1])
        return receipt(
            await create_recovery(
                connection,
                action_key=action_key,
                local_date=today,
                sleep_minutes=values[0],
                soreness=values[1],
                fatigue=values[2],
                readiness=values[3],
            )
        )
    except RecoveryError as exc:
        return MealReply(f"{exc}\nNo recovery check-in changed.", "recovery_rejected")


async def handle_recovery_callback(
    connection: AsyncConnection, action: str, recovery_id: int, revision_id: int, *, action_key: str
) -> MealReply:
    try:
        current = await get_recovery(connection, recovery_id)
        if current.revision_id != revision_id:
            raise RecoveryError("That is an older receipt. Open /recovery history.")
        if action == "edit":
            return MealReply(
                f"Reply with /recovery edit R{current.id}r{current.revision_number} "
                "sleep 7.5h fatigue 3",
                "recovery_edit_help",
            )
        updated = (
            await revise_recovery(
                connection, recovery_id, revision_id, action_key=action_key, delete=True
            )
            if action == "delete"
            else await undo_recovery(connection, recovery_id, revision_id, action_key=action_key)
            if action == "undo"
            else None
        )
        if updated is None:
            raise RecoveryError("Choose Edit, Delete or Undo.")
        return receipt(updated, "Updated recovery")
    except RecoveryError as exc:
        return MealReply(f"{exc}\nNo recovery check-in changed.", "recovery_rejected")
