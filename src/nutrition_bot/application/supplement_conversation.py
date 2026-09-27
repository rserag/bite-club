import re
from datetime import datetime
from decimal import Decimal
from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.supplements import (
    SupplementError,
    SupplementIntakeSnapshot,
    create_direct_supplement_intake,
    create_product_supplement_intake,
    create_reviewed_creatine_product,
    get_supplement_intake,
    list_supplement_products,
    recent_supplement_intakes,
    resolve_supplement_product,
    revise_supplement_intake,
    undo_supplement_intake,
)
from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.domain.supplements import (
    SUPPLEMENT_AMOUNT_SCALE,
    SupplementUnit,
    supplement_amount_scaled,
)

REFERENCE = r"S([1-9][0-9]*)r([1-9][0-9]*)"
AMOUNT = r"([0-9]+(?:\.[0-9]{1,3})?)\s*(g|mg)"


def _amount(text: str) -> int:
    matched = re.fullmatch(AMOUNT, text.strip(), re.IGNORECASE)
    if matched is None:
        raise SupplementError("Use an exact amount such as 5 g or 3000 mg.")
    return supplement_amount_scaled(matched[1], cast(SupplementUnit, matched[2].casefold()), "mg")


def _display(amount_scaled: int) -> str:
    milligrams = Decimal(amount_scaled) / SUPPLEMENT_AMOUNT_SCALE
    if milligrams >= 1000:
        return f"{_plain(milligrams / 1000)} g"
    return f"{_plain(milligrams)} mg"


def _plain(value: Decimal) -> str:
    return f"{value:f}".rstrip("0").rstrip(".") if "." in f"{value:f}" else f"{value:f}"


def _servings(value: str) -> int:
    parsed = Decimal(value)
    scaled = parsed * SUPPLEMENT_AMOUNT_SCALE
    if parsed <= 0 or parsed > 100 or scaled != scaled.to_integral_value():
        raise SupplementError("Use 0–6 decimal places and no more than 100 servings.")
    return int(scaled)


def receipt(snapshot: SupplementIntakeSnapshot, lead: str = "Saved supplement") -> MealReply:
    if snapshot.product_version_id is not None:
        assert snapshot.product_name is not None
        assert snapshot.serving_description is not None
        assert snapshot.servings_scaled is not None
        servings = Decimal(snapshot.servings_scaled) / SUPPLEMENT_AMOUNT_SCALE
        detail = (
            f"{snapshot.product_name} · {servings.normalize()} serving(s) "
            f"({snapshot.serving_description}) · "
            f"{_display(snapshot.component_amount_scaled)} creatine from reviewed label"
        )
    else:
        assert snapshot.amount_scaled is not None
        detail = f"Creatine monohydrate · {_display(snapshot.amount_scaled)} · exact reported dose"
    text = f"{lead} S{snapshot.id}r{snapshot.revision_number} · {snapshot.local_date}\n" + detail
    if snapshot.deleted:
        text += "\nDeleted from current supplement history; revisions are retained."
        buttons: tuple[str, ...] = ("undo",)
    else:
        correction = "1 serving" if snapshot.product_version_id else "5 g"
        text += (
            f"\nCorrect with /supplement edit S{snapshot.id}r{snapshot.revision_number} "
            f"{correction}. This is actual intake, not a planned dose."
        )
        buttons = ("edit", "delete", "undo")
    return MealReply(
        text,
        "supplement_receipt",
        buttons=buttons,
        supplement_intake_id=snapshot.id,
        supplement_intake_revision_id=snapshot.revision_id,
    )


async def handle_supplement_message(
    connection: AsyncConnection,
    text: str,
    *,
    action_key: str,
    reference: datetime,
    source_chat_id: int,
    source_message_id: int,
) -> MealReply | None:
    from nutrition_bot.application.supplement_report import handle_report

    report = await handle_report(connection, text, reference)
    if report is not None:
        return report

    from nutrition_bot.application.supplement_plan_conversation import handle_plan_message

    plan_reply = await handle_plan_message(
        connection,
        text,
        action_key=action_key,
        reference=reference,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
    )
    if plan_reply is not None:
        return plan_reply
    stripped = text.strip()
    lowered = stripped.casefold()
    if lowered in {"/supplement", "/supplements", "/supplement help"}:
        return MealReply(
            "Exact supplement logging is available for creatine monohydrate:\n"
            "creatine 5 g\nI took 5000 mg creatine\n"
            "Save an exact creatine label with:\n"
            "/supplement product add Name | 5 g | 1 scoop | aliases: my creatine\n"
            "Then log it with /supplement take 1 serving | my creatine.\n"
            "Use /supplement products or /supplement history. An amount-free message never "
            "invents a dose. Use /supplement plan for reviewed creatine plans.\n"
            "Reports: /supplements today and /supplements week show food, supplements, "
            "combined nutrients and plan adherence.",
            "supplement_help",
        )
    if lowered == "/supplement products":
        products = await list_supplement_products(connection)
        if not products:
            return MealReply("No reviewed supplement products yet.", "supplement_products")
        return MealReply(
            "Reviewed supplement products:\n"
            + "\n".join(
                f"P{product.id} · {product.name} · "
                f"{_display(product.creatine_amount_scaled)} per {product.serving_description} · "
                f"names: {', '.join(product.aliases)}"
                for product in products
            ),
            "supplement_products",
        )
    if lowered == "/supplement history":
        rows = await recent_supplement_intakes(connection)
        if not rows:
            return MealReply("No supplement intake yet.", "supplement_history")
        return MealReply(
            "Recent supplement intake:\n"
            + "\n".join(
                f"S{row.id}r{row.revision_number} · {row.local_date} · "
                + (
                    f"{row.product_name} {_display(row.component_amount_scaled)} creatine"
                    if row.product_name
                    else f"creatine {_display(row.component_amount_scaled)}"
                )
                + (" · deleted" if row.deleted else "")
                for row in rows
            ),
            "supplement_history",
        )
    try:
        product_add = re.fullmatch(
            r"/supplement\s+product\s+add\s+([^|]+)\|\s*([^|]+)\|\s*([^|]+)"
            r"\|\s*aliases:\s*(.+)",
            stripped,
            re.IGNORECASE,
        )
        if product_add:
            aliases = tuple(value.strip() for value in product_add[4].split(",") if value.strip())
            if not aliases:
                raise SupplementError("Include at least one short alias after 'aliases:'.")
            product = await create_reviewed_creatine_product(
                connection,
                action_key=action_key,
                name=product_add[1],
                amount_scaled=_amount(product_add[2]),
                serving_description=product_add[3],
                aliases=aliases,
            )
            return MealReply(
                f"Saved reviewed product P{product.id}: {product.name}\n"
                f"Exact label: {_display(product.creatine_amount_scaled)} creatine monohydrate "
                f"per {product.serving_description}.\n"
                f"Names: {', '.join(product.aliases)}. The label snapshot is now immutable.",
                "supplement_product_saved",
            )
        product_take = re.fullmatch(
            r"/supplement\s+take\s+([0-9]+(?:\.[0-9]{1,6})?)\s+servings?\s*\|\s*(.+)",
            stripped,
            re.IGNORECASE,
        )
        if product_take:
            product = await resolve_supplement_product(connection, product_take[2])
            return receipt(
                await create_product_supplement_intake(
                    connection,
                    action_key=action_key,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    local_date=reference.date(),
                    consumed_at=reference.timestamp(),
                    timezone=str(reference.tzinfo),
                    product=product,
                    product_servings_scaled=_servings(product_take[1]),
                )
            )
        mutation = re.fullmatch(
            rf"/supplement\s+(edit|delete|undo)\s+{REFERENCE}(?:\s+(.+))?",
            stripped,
            re.IGNORECASE,
        )
        if mutation:
            current = await get_supplement_intake(connection, int(mutation[2]))
            if current.revision_number != int(mutation[3]):
                raise SupplementError("Use the current S…r… reference from /supplement history.")
            operation = mutation[1].casefold()
            if operation == "edit":
                if mutation[4] is None:
                    raise SupplementError("Include the replacement dose, such as 5 g or 1 serving.")
                replacement = mutation[4].strip()
                serving_edit = re.fullmatch(
                    r"([0-9]+(?:\.[0-9]{1,6})?)\s+servings?", replacement, re.I
                )
                return receipt(
                    await revise_supplement_intake(
                        connection,
                        current.id,
                        current.revision_id,
                        action_key=action_key,
                        amount_scaled=None if serving_edit else _amount(replacement),
                        product_servings_scaled=(
                            _servings(serving_edit[1]) if serving_edit else None
                        ),
                    ),
                    "Updated supplement",
                )
            if mutation[4] is not None:
                raise SupplementError("Delete and undo do not take an amount.")
            updated = (
                await revise_supplement_intake(
                    connection,
                    current.id,
                    current.revision_id,
                    action_key=action_key,
                    delete=True,
                )
                if operation == "delete"
                else await undo_supplement_intake(
                    connection, current.id, current.revision_id, action_key=action_key
                )
            )
            return receipt(updated, "Updated supplement")
        patterns = (
            rf"(?:i\s+)?took\s+{AMOUNT}\s+(?:of\s+)?creatine(?:\s+monohydrate)?",
            rf"creatine(?:\s+monohydrate)?\s+{AMOUNT}",
        )
        matched = next(
            (match for pattern in patterns if (match := re.fullmatch(pattern, stripped, re.I))),
            None,
        )
        if matched is None:
            if "creatine" in lowered:
                raise SupplementError(
                    "Include the exact creatine amount, for example 'creatine 5 g'."
                )
            return None
        amount_scaled = supplement_amount_scaled(
            matched[1], cast(SupplementUnit, matched[2].casefold()), "mg"
        )
        return receipt(
            await create_direct_supplement_intake(
                connection,
                action_key=action_key,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                local_date=reference.date(),
                consumed_at=reference.timestamp(),
                timezone=str(reference.tzinfo),
                substance_code="creatine_monohydrate",
                amount_scaled=amount_scaled,
            )
        )
    except SupplementError as exc:
        return MealReply(f"{exc}\nNo supplement data changed.", "supplement_rejected")


async def handle_supplement_callback(
    connection: AsyncConnection,
    action: str,
    intake_id: int,
    revision_id: int,
    *,
    action_key: str,
) -> MealReply:
    try:
        current = await get_supplement_intake(connection, intake_id)
        if current.revision_id != revision_id:
            raise SupplementError(
                "That receipt is old. Open /supplement history for the latest one."
            )
        if action == "edit":
            example = "1 serving" if current.product_version_id else "5 g"
            return MealReply(
                f"Send /supplement edit S{current.id}r{current.revision_number} {example}.\n"
                "Nothing changed.",
                "supplement_edit_help",
            )
        updated = (
            await revise_supplement_intake(
                connection,
                current.id,
                current.revision_id,
                action_key=action_key,
                delete=True,
            )
            if action == "delete"
            else await undo_supplement_intake(
                connection, current.id, current.revision_id, action_key=action_key
            )
            if action == "undo"
            else None
        )
        if updated is None:
            raise SupplementError("Unknown supplement action.")
        return receipt(updated, "Updated supplement")
    except SupplementError as exc:
        return MealReply(f"{exc}\nNo supplement data changed.", "supplement_rejected")
