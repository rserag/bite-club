"""Local Telegram meal interactions. No inference, network or hidden partial saves."""

import re
import time as clock_time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, localcontext
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from aiogram.types import Message
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.aliases import list_aliases, resolve_alias
from nutrition_bot.adapters.database.foods import get_food_version
from nutrition_bot.adapters.database.meals import (
    MealError,
    MealItemInput,
    MealSnapshot,
    create_meal,
    get_meal,
    input_from_snapshot,
    revise_meal,
    undo_meal,
)
from nutrition_bot.adapters.database.schema import food_versions, foods, meals, outbox
from nutrition_bot.application.recipe_display import portion_mass, share_lines
from nutrition_bot.domain.aliases import AliasError
from nutrition_bot.domain.drafts import DraftError
from nutrition_bot.domain.food import grams_to_milligrams
from nutrition_bot.domain.meal_text import MealTextError, ParsedMeal, parse_meal
from nutrition_bot.domain.recipe_portions import RecipeError
from nutrition_bot.domain.reuse import ReuseError, planned_from_meal

MAX_INPUT = 2000
REFERENCE = r"M([1-9][0-9]*)(?:r([1-9][0-9]*))?"


@dataclass(frozen=True)
class MealReply:
    text: str
    kind: str = "meal_help"
    meal_id: int | None = None
    revision_id: int | None = None
    buttons: tuple[str, ...] = ()
    draft_id: int | None = None
    draft_revision: int | None = None
    favorite_id: int | None = None
    favorite_version_id: int | None = None
    recipe_id: int | None = None
    recipe_version_id: int | None = None
    goal_proposal_id: int | None = None
    weight_id: int | None = None
    weight_revision_id: int | None = None
    daily_date: str | None = None
    weekly_end: str | None = None
    adaptive_proposal_id: int | None = None
    training_session_id: int | None = None
    training_revision_id: int | None = None
    recovery_id: int | None = None
    recovery_revision_id: int | None = None
    supplement_plan_proposal_id: int | None = None
    supplement_intake_id: int | None = None
    supplement_intake_revision_id: int | None = None


def _short(value: str, limit: int = 90) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _reference(meal: MealSnapshot) -> str:
    return f"M{meal.id}r{meal.revision_number}"


def receipt(meal: MealSnapshot, lead: str = "Meal") -> MealReply:
    lines = [
        f"{lead} {_reference(meal)} · {meal.local_date.isoformat()} · {_short(meal.label, 60)}"
    ]
    if meal.deleted:
        lines.append("Deleted from your diary; history is retained.")
    lines.extend(share_lines(item.recipe_share for item in meal.items))
    for index, item in enumerate(meal.items, 1):
        mass = portion_mass(item.edible_milligrams, item.recipe_share)
        name_limit = (
            35 if item.recipe_share else 55 if item.quantity_method == "approved_estimate" else 90
        )
        lines.append(
            f"{index}. {mass} "
            f"{_short(item.food_name, name_limit)} "
            f"({item.preparation}; #{item.food_version_id})"
            + (" · approved estimate" if item.quantity_method == "approved_estimate" else "")
        )
        if item.quantity_basis:
            lines.append(
                "   Basis: "
                + _short(" ".join(item.quantity_basis.split()), 45 if item.recipe_share else 60)
            )
    if not meal.deleted:
        totals = []
        for code, label, suffix in (
            ("energy", "Energy", "kcal"),
            ("protein", "P", "g"),
            ("carbohydrate", "C", "g"),
            ("fat", "F", "g"),
            ("fiber", "Fiber", "g"),
        ):
            known = 0
            missing = 0
            for item in meal.items:
                amount = next((n.amount_scaled for n in item.nutrients if n.code == code), None)
                if amount is None:
                    missing += 1
                else:
                    known += amount
            if missing == len(meal.items):
                totals.append(f"{label}: unknown")
            else:
                with localcontext() as decimal_context:
                    decimal_context.prec = 50
                    value = (Decimal(known) / 1_000_000).quantize(
                        Decimal("1" if code == "energy" else "0.1"), rounding=ROUND_HALF_UP
                    )
                totals.append(
                    f"{label}: {value} {suffix}"
                    + (f" known ({missing} unknown)" if missing else "")
                )
        lines.append(" · ".join(totals))
    lines.append(
        "Reply with 'portion 250g' (or servings), date, delete, or undo."
        if any(item.recipe_share for item in meal.items)
        else "Reply to this receipt to change an item, date, delete, or undo."
    )
    buttons = ["edit"]
    if not meal.deleted:
        buttons.append("delete")
    if meal.operation != "undo":
        buttons.append("undo")
    return MealReply("\n".join(lines), "meal_receipt", meal.id, meal.revision_id, tuple(buttons))


def help_reply() -> MealReply:
    return MealReply(
        "Log measured foods from your saved catalog:\n"
        "I ate 150g rice and 200g chicken\n"
        "/meal yesterday Lunch: 150g #12; 200g #18\n"
        "Use /foods rice to find an exact food/version; /meals shows recent meals.\n"
        "Save a receipt as a favorite: reply 'save as usual breakfast'. "
        "Use /favorites, /eat usual breakfast or reply 'same again'. "
        "Use /aliases for personal food names; /recipe for batch recipes.\n"
        "Use /today for daily totals and nutrient-data coverage; /today short for a quick view. "
        "Use /week or /week short for seven-day trends.\n"
        "Reply to a receipt: 'item 1: 120g', 'rice was 120g', 'replace: 120g rice; 200g chicken', "
        "'date yesterday', 'delete', or 'undo'.\n"
        "Rough amounts such as 'about 150g rice' create drafts for explicit button approval. "
        "Use /drafts to resume. Counts and cups still need measured grams or a reviewed estimate."
    )


async def _catalog_rows(
    connection: AsyncConnection, query: str, *, exact: bool = False
) -> list[sa.RowMapping]:
    latest = (
        sa.select(
            food_versions.c.food_id, sa.func.max(food_versions.c.version_number).label("number")
        )
        .where(food_versions.c.sealed.is_(True))
        .group_by(food_versions.c.food_id)
        .subquery()
    )
    statement = (
        sa.select(food_versions, foods.c.preparation)
        .join(foods)
        .join(
            latest,
            sa.and_(
                food_versions.c.food_id == latest.c.food_id,
                food_versions.c.version_number == latest.c.number,
            ),
        )
    )
    if exact:
        statement = statement.where(
            sa.func.unicode_casefold(food_versions.c.name) == query.casefold()
        )
    else:
        for word in query.casefold().split():
            escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(
                sa.func.unicode_casefold(food_versions.c.name).like(f"%{escaped}%", escape="\\")
            )
    return list(
        (
            await connection.execute(
                statement.order_by(food_versions.c.name, food_versions.c.id).limit(6)
            )
        )
        .mappings()
        .all()
    )


async def food_choices(connection: AsyncConnection, query: str) -> MealReply:
    if len(query) > 120:
        return MealReply("Use a shorter food search. Nothing was logged.")
    rows = await _catalog_rows(connection, query)
    aliases = [alias for alias in await list_aliases(connection, query=query) if alias.active]
    if not rows and not aliases:
        return MealReply(
            "No matching saved food. Add a reviewed food with the catalog tools first; "
            "nothing was logged."
        )
    lines = ["Saved food versions (choose the matching food and preparation):"]
    for row in rows:
        lines.append(f"#{row['id']} · {_short(row['name'], 160)} · {row['preparation']}")
    for alias in aliases[:3]:
        lines.append(
            f"Alias {alias.name} → #{alias.food_version_id} (A{alias.id}r{alias.revision})"
        )
    if len(aliases) > 3:
        lines.append("More aliases match; use /aliases to search your personal names.")
    example_id = rows[0]["id"] if rows else aliases[0].food_version_id
    lines.append(
        f"Use its number with a measured weight, e.g. /meal 150g #{example_id}. Nothing was logged."
    )
    return MealReply("\n".join(lines), "food_choices")


async def _resolve_items(
    connection: AsyncConnection, parsed: ParsedMeal, previous: MealSnapshot | None = None
) -> tuple[MealItemInput, ...]:
    result = []
    for item in parsed.items:
        by_id = re.fullmatch(r"#([1-9][0-9]*)", item.query)
        historic = (
            [old for old in previous.items if old.food_name.casefold() == item.query.casefold()]
            if previous
            else []
        )
        if by_id:
            version_id = int(by_id[1])
        elif len({old.food_version_id for old in historic}) == 1:
            version_id = historic[0].food_version_id
        elif alias := await resolve_alias(connection, item.query):
            version_id = alias.food_version_id
        else:
            candidates = await _catalog_rows(connection, item.query, exact=True)
            if len(candidates) != 1:
                choices = await food_choices(connection, item.query)
                raise MealError(
                    f"Food '{_short(item.query)}' needs an exact selection. {choices.text}"
                )
            version_id = candidates[0]["id"]
        try:
            await get_food_version(connection, version_id)
        except ValueError:
            raise MealError(
                "That saved food version does not exist. Use /foods to choose another."
            ) from None
        result.append(
            MealItemInput(
                food_version_id=version_id,
                edible_milligrams=grams_to_milligrams(item.grams),
                original_quantity=item.original_quantity,
                original_unit=item.original_unit,
            )
        )
    return tuple(result)


def _date_timestamp(day: date, reference: datetime) -> float:
    # A backdated date has no reported clock time; local noon is a date-only anchor.
    try:
        if day == reference.date():
            return reference.timestamp()
        return datetime.combine(day, time(12), reference.tzinfo).timestamp()
    except (ValueError, OverflowError, OSError):
        raise MealError(
            "That date cannot be represented in this time zone. Choose another date."
        ) from None


async def _explicit_target(
    connection: AsyncConnection, token: str, *, mutation: bool
) -> MealSnapshot:
    matched = re.fullmatch(REFERENCE, token, re.IGNORECASE)
    if matched is None:
        raise MealError("Choose a receipt with /meals first.")
    meal = await get_meal(connection, int(matched[1]))
    if mutation and (matched[2] is None or int(matched[2]) != meal.revision_number):
        raise MealError(f"Use the current receipt: /meal M{meal.id}. No change was saved.")
    return meal


async def _reply_target(
    connection: AsyncConnection,
    message: Message,
    bot_id: int,
    owner_id: int,
    retention_days: int,
) -> MealSnapshot | None:
    replied = message.reply_to_message
    if (
        replied is None
        or replied.from_user is None
        or replied.from_user.id != bot_id
        or not replied.from_user.is_bot
    ):
        return None
    row = (
        await connection.execute(
            sa.select(outbox.c.payload)
            .where(
                outbox.c.chat_id == message.chat.id,
                outbox.c.owner_user_id == owner_id,
                outbox.c.telegram_message_id == replied.message_id,
                outbox.c.kind == "message",
                outbox.c.status == "sent",
                outbox.c.sent_at >= clock_time.time() - retention_days * 86400,
            )
            .order_by(outbox.c.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not isinstance(row, dict) or "meal_id" not in row:
        return None
    meal = await get_meal(connection, row["meal_id"])
    if meal.revision_id != row.get("meal_revision_id"):
        raise MealError(
            f"That receipt is older than the current meal. Use /meal M{meal.id} before changing it."
        )
    return meal


def _correction_items(text: str, today: date) -> ParsedMeal:
    prefix = re.sub(r"^/meal(?:@[A-Za-z0-9_]+)?\s+", "", text, flags=re.IGNORECASE)
    prefix = re.sub(r"^I\s+ate\s+", "", prefix, flags=re.IGNORECASE)
    if re.match(r"(?:today|yesterday|[0-9]{4}-[0-9]{2}-[0-9]{2})(?:\s|$)", prefix, re.IGNORECASE):
        raise MealError("Change the date separately with 'date yesterday' or 'date YYYY-MM-DD'.")
    parsed = parse_meal(text, today)
    if parsed.label != "Meal" or parsed.local_date != today:
        raise MealError("Use item amounts only for this correction; change the date separately.")
    return parsed


async def _correct_measured(
    connection: AsyncConnection, meal: MealSnapshot, text: str, action_key: str, reference: datetime
) -> MealReply:
    folded = text.casefold().strip()
    if folded in {"delete", "delete meal"}:
        if meal.deleted:
            raise MealError("That meal is already deleted.")
        return receipt(
            await revise_meal(
                connection,
                meal.id,
                meal.revision_id,
                action_key=action_key,
                deleted=True,
                operation="delete",
            ),
            "Deleted",
        )
    if folded in {"undo", "undo last change"}:
        return receipt(
            await undo_meal(connection, meal.id, meal.revision_id, action_key=action_key), "Undone"
        )
    date_match = re.fullmatch(
        r"(?:date|move to|move this to)\s+(today|yesterday|\d{4}-\d{2}-\d{2})", folded
    )
    if date_match:
        day = parse_meal(f"/meal {date_match[1]} 1g #1", reference.date()).local_date
        try:
            anchor = datetime.combine(day, time(12), ZoneInfo(meal.timezone)).timestamp()
        except (ValueError, OverflowError, OSError):
            raise MealError(
                "That date cannot be represented in this time zone. Choose another date."
            ) from None
        return receipt(
            await revise_meal(
                connection,
                meal.id,
                meal.revision_id,
                action_key=action_key,
                local_date=day,
                consumed_at=anchor,
            ),
            "Updated",
        )
    replacement = re.match(r"(?:replace:|replace meal:)\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if replacement:
        parsed = _correction_items(replacement[1], reference.date())
        items = await _resolve_items(connection, parsed, meal)
        return receipt(
            await revise_meal(
                connection,
                meal.id,
                meal.revision_id,
                action_key=action_key,
                items=items,
                deleted=False,
            ),
            "Updated",
        )
    if any(item.recipe_share for item in meal.items):
        raise MealError(
            "For a recipe, use 'portion 250g' (or servings), or 'replace: ...' "
            "to replace the entire meal with directly logged foods."
        )
    indexed = re.fullmatch(r"item\s+([1-9][0-9]*):\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if indexed:
        index = int(indexed[1]) - 1
        if index >= len(meal.items):
            raise MealError("That item number is not in this receipt.")
        body = indexed[2]
        if body.casefold() == "delete":
            if len(meal.items) == 1:
                raise MealError("Use 'delete' to remove this one-item meal.")
            selected = None
        else:
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?\s*(?:mg|kg|g)", body, re.IGNORECASE):
                body += f" #{meal.items[index].food_version_id}"
            parsed = _correction_items(body, reference.date())
            if len(parsed.items) != 1:
                raise MealError(
                    "Change one item at a time, or use 'replace: ...' for the whole meal."
                )
            selected = (await _resolve_items(connection, parsed, meal))[0]
    else:
        natural = re.fullmatch(
            r"(.+?)\s+was\s+([0-9]+(?:\.[0-9]+)?\s*(?:mg|kg|g))", text, re.IGNORECASE
        )
        if natural:
            matches = [
                i
                for i, item in enumerate(meal.items)
                if item.food_name.casefold() == natural[1].casefold()
            ]
            if len(matches) != 1:
                raise MealError("Choose the item number, e.g. 'item 1: 120g'.")
            index = matches[0]
            parsed = parse_meal(
                f"{natural[2]} #{meal.items[index].food_version_id}", reference.date()
            )
        elif len(meal.items) == 1:
            index = 0
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?\s*(?:mg|kg|g)", text, re.IGNORECASE):
                text += f" #{meal.items[0].food_version_id}"
            parsed = _correction_items(text, reference.date())
        else:
            raise MealError(
                "Use 'item 1: 120g', 'item 2: 80g other food', 'replace: ...', "
                "'date yesterday', 'delete', or 'undo'."
            )
        if len(parsed.items) != 1:
            raise MealError("Use 'replace: ...' to replace the whole meal.")
        selected = (await _resolve_items(connection, parsed, meal))[0]
    updated_items = [input_from_snapshot(item) for item in meal.items]
    if selected is None:
        del updated_items[index]
    else:
        updated_items[index] = selected
    return receipt(
        await revise_meal(
            connection,
            meal.id,
            meal.revision_id,
            action_key=action_key,
            items=tuple(updated_items),
            deleted=False,
        ),
        "Updated",
    )


async def _correct(
    connection: AsyncConnection,
    meal: MealSnapshot,
    text: str,
    action_key: str,
    reference: datetime,
    message: Message,
) -> MealReply:
    from nutrition_bot.application.draft_conversation import correction_draft

    if re.match(r"portion(?:\s|$)", text, re.IGNORECASE):
        from nutrition_bot.adapters.database.drafts import create_draft
        from nutrition_bot.application.draft_conversation import draft_receipt
        from nutrition_bot.application.recipe_conversation import revised_recipe_items
        from nutrition_bot.domain.drafts import DraftContent

        if meal.deleted:
            raise MealError("Restore the deleted meal before changing its portion.")
        items = await revised_recipe_items(connection, planned_from_meal(meal), text)
        if any(item.estimate_basis for item in items):
            content = DraftContent(
                label=meal.label,
                local_date=meal.local_date,
                timezone=meal.timezone,
                consumed_at=meal.consumed_at,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                items=items,
                target_meal_id=meal.id,
                target_revision_id=meal.revision_id,
            )
            draft = await create_draft(
                connection, content, action_key=action_key, now=clock_time.time()
            )
            return await draft_receipt(connection, draft, "Recipe correction draft")
        exact = tuple(
            MealItemInput(
                food_version_id=item.food_version_id,
                edible_milligrams=item.edible_milligrams,
                original_quantity=item.original_quantity,
                original_unit=item.original_unit,
                recipe_share=item.recipe_share,
            )
            for item in items
            if item.edible_milligrams is not None
            and item.original_quantity is not None
            and item.original_unit is not None
        )
        return receipt(
            await revise_meal(
                connection, meal.id, meal.revision_id, action_key=action_key, items=exact
            ),
            "Updated portion",
        )

    rough = re.search(
        r"\b(?:about|around|approximately|approx|roughly|estimated)\b|~", text, re.IGNORECASE
    )
    if not rough:
        try:
            return await _correct_measured(connection, meal, text, action_key, reference)
        except MealTextError:
            pass
    return await correction_draft(
        connection, meal, text, message, action_key=action_key, reference=reference
    )


async def handle_message(
    connection: AsyncConnection,
    message: Message,
    *,
    action_key: str,
    timezone: str,
    bot_id: int,
    owner_id: int,
    edited: bool = False,
    retention_days: int = 30,
) -> MealReply:
    text = (message.text or "").strip()
    if not text:
        return MealReply(
            "Send food amounts as text. Photos are not supported yet; nothing was logged."
        )
    if len(text) > MAX_INPUT:
        return MealReply(
            "That message is too long. Log at most ten measured foods at a time; nothing was saved."
        )
    # Strip only a Telegram command's optional bot suffix, never arbitrary trailing text.
    text = re.sub(r"^(/[a-z]+)@[A-Za-z0-9_]+(?=\s|$)", r"\1", text, flags=re.IGNORECASE)
    event_time = message.edit_date or message.date
    event_time = (
        datetime.fromtimestamp(event_time, UTC) if isinstance(event_time, int) else event_time
    )
    reference = event_time.astimezone(ZoneInfo(timezone))
    try:
        from nutrition_bot.adapters.database.drafts import get_draft, track_draft_action
        from nutrition_bot.adapters.database.schema_drafts import meal_drafts
        from nutrition_bot.application.draft_conversation import (
            draft_receipt,
            handle_draft_message,
            new_draft,
        )

        if edited:
            from nutrition_bot.adapters.database.schema_supplements import supplement_intakes
            from nutrition_bot.adapters.database.supplements import get_supplement_intake
            from nutrition_bot.application.supplement_conversation import (
                receipt as supplement_receipt,
            )

            supplement_id = await connection.scalar(
                sa.select(supplement_intakes.c.id).where(
                    supplement_intakes.c.source_chat_id == message.chat.id,
                    supplement_intakes.c.source_message_id == message.message_id,
                )
            )
            if supplement_id:
                return supplement_receipt(
                    await get_supplement_intake(connection, supplement_id),
                    "Original message edited; supplement history unchanged. Current supplement",
                )
            meal_id = await connection.scalar(
                sa.select(meals.c.id).where(
                    meals.c.source_chat_id == message.chat.id,
                    meals.c.source_message_id == message.message_id,
                )
            )
            if meal_id:
                meal = await get_meal(connection, meal_id)
                return receipt(
                    meal,
                    "Original message edited; diary unchanged. "
                    "Reply to this current receipt to apply your correction.",
                )
            draft_id = await connection.scalar(
                sa.select(meal_drafts.c.id).where(
                    meal_drafts.c.source_chat_id == message.chat.id,
                    meal_drafts.c.source_message_id == message.message_id,
                )
            )
            if draft_id:
                draft = await get_draft(connection, draft_id)
                assert draft is not None
                await track_draft_action(connection, draft.id, action_key)
                return await draft_receipt(
                    connection, draft, "Original message edited; draft unchanged. Current draft"
                )
            return MealReply(
                "Editing that message did not create a meal. Send a new measured log to save it."
            )
        from nutrition_bot.application.allocation_conversation import handle_allocation_message

        allocation = await handle_allocation_message(
            connection, text, action_key=action_key, today=datetime.now(ZoneInfo(timezone)).date()
        )
        if allocation is not None:
            return allocation
        from nutrition_bot.application.daily_conversation import handle_daily_message

        daily = await handle_daily_message(
            connection,
            text,
            action_key=action_key,
            today=reference.date(),
            timezone=timezone,
        )
        if daily is not None:
            return daily
        from nutrition_bot.application.nutrient_conversation import handle_nutrient_message

        nutrient = await handle_nutrient_message(connection, text, today=reference.date())
        if nutrient is not None:
            return nutrient
        from nutrition_bot.application.weekly_conversation import handle_weekly_message

        weekly = await handle_weekly_message(connection, text, today=reference.date())
        if weekly is not None:
            return weekly
        from nutrition_bot.application.supplement_conversation import handle_supplement_message

        supplement = await handle_supplement_message(
            connection,
            text,
            action_key=action_key,
            reference=reference,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
        )
        if supplement is not None:
            return supplement
        from nutrition_bot.application.adaptive_conversation import handle_adaptive_message

        adaptive = await handle_adaptive_message(
            connection, text, action_key=action_key, today=reference.date()
        )
        if adaptive is not None:
            return adaptive
        from nutrition_bot.application.training_load_report import handle_training_load_message

        training_load = await handle_training_load_message(connection, text, today=reference.date())
        if training_load is not None:
            return training_load
        from nutrition_bot.application.recovery_conversation import handle_recovery_message

        recovery = await handle_recovery_message(
            connection, text, action_key=action_key, today=reference.date()
        )
        if recovery is not None:
            return recovery
        from nutrition_bot.application.training_conversation import handle_training_message

        training = await handle_training_message(
            connection,
            text,
            action_key=action_key,
            reference=reference,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
        )
        if training is not None:
            return training
        from nutrition_bot.application.goal_conversation import handle_goal_message

        goal_reply = await handle_goal_message(
            connection, text, action_key=action_key, today=reference.date()
        )
        if goal_reply is not None:
            return goal_reply
        from nutrition_bot.application.weight_conversation import handle_weight_message

        weight_reply = await handle_weight_message(
            connection,
            text,
            action_key=action_key,
            today=reference.date(),
            timezone=timezone,
            event_time=reference,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
        )
        if weight_reply is not None:
            return weight_reply
        if text in {"/meal", "/help"}:
            return help_reply()
        if text == "/foods" or text.startswith("/foods "):
            return await food_choices(connection, text[6:].strip())
        if text == "/meals":
            rows = (
                (
                    await connection.execute(
                        sa.select(meals.c.id).order_by(meals.c.id.desc()).limit(10)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return MealReply(
                    "No meals logged yet. Use /foods to find a saved food, "
                    "then log its measured weight."
                )
            snapshots = [await get_meal(connection, value) for value in rows]
            return MealReply(
                "Recent meals:\n"
                + "\n".join(
                    f"{_reference(value)} · {value.local_date} · {_short(value.label, 50)}"
                    + (" · deleted" if value.deleted else "")
                    for value in snapshots
                )
                + f"\nOpen a receipt with /meal M{snapshots[0].id}.",
                "meal_list",
            )
        view = re.fullmatch(r"/meal\s+(" + REFERENCE + ")", text, re.IGNORECASE)
        if view:
            return receipt(await _explicit_target(connection, view[1], mutation=False))
        explicit = re.fullmatch(
            r"/(edit|delete|undo)\s+(" + REFERENCE + r")(?:\s+(.+))?",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if explicit:
            meal = await _explicit_target(connection, explicit[2], mutation=True)
            operation = explicit[1].lower()
            body = explicit[5] or ""
            if operation in {"delete", "undo"}:
                if body:
                    raise MealError("This command does not accept extra text; nothing changed.")
                body = operation
            elif not body:
                raise MealError(
                    "Add the correction after the meal reference, or reply to its receipt."
                )
            return await _correct(connection, meal, body, action_key, reference, message)
        from nutrition_bot.application.recipe_conversation import handle_recipe_message

        recipe_reply = await handle_recipe_message(
            connection, message, text, action_key=action_key, reference=reference
        )
        if recipe_reply is not None:
            return recipe_reply
        from nutrition_bot.application.reuse_conversation import handle_reuse_message

        reuse_reply = await handle_reuse_message(
            connection,
            message,
            text,
            action_key=action_key,
            reference=reference,
            bot_id=bot_id,
            owner_id=owner_id,
            retention_days=retention_days,
        )
        if reuse_reply is not None:
            return reuse_reply
        draft_reply = await handle_draft_message(
            connection,
            message,
            text,
            action_key=action_key,
            reference=reference,
            bot_id=bot_id,
            owner_id=owner_id,
            retention_days=retention_days,
        )
        if draft_reply is not None:
            return draft_reply
        replied = await _reply_target(connection, message, bot_id, owner_id, retention_days)
        if replied:
            return await _correct(connection, replied, text, action_key, reference, message)
        if message.reply_to_message is not None:
            raise MealError(
                "I could not match a current, retained meal receipt. "
                "Open one with /meals, or send a new message to log a new meal."
            )
        if text.startswith("/") and not text.startswith("/meal "):
            return help_reply()
        try:
            parsed = parse_meal(text, reference.date())
        except MealTextError:
            return await new_draft(
                connection, text, message, action_key=action_key, reference=reference
            )
        resolved = await _resolve_items(connection, parsed)
        saved = await create_meal(
            connection,
            items=resolved,
            label=parsed.label,
            local_date=parsed.local_date,
            timezone=timezone,
            consumed_at=_date_timestamp(parsed.local_date, reference),
            action_key=action_key,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
        )
        return receipt(saved, "Saved")
    except ValidationError:
        return MealReply(
            "That draft amount or date is unsupported. Enter a short amount in grams.\n"
            "Nothing was changed.",
            "meal_rejected",
        )
    except (MealError, MealTextError, DraftError, AliasError, ReuseError, RecipeError) as exc:
        return MealReply(_short(str(exc), 2000) + "\nNothing was changed.", "meal_rejected")


async def handle_callback(
    connection: AsyncConnection, action: str, meal_id: int, revision_id: int, *, action_key: str
) -> MealReply:
    try:
        meal = await get_meal(connection, meal_id)
        if meal.revision_id != revision_id:
            return receipt(
                meal, "That button belongs to an older receipt. No change saved. Current meal:"
            )
        if action == "edit":
            result = receipt(
                meal,
                "Reply to this receipt with 'item 1: 120g', 'replace: ...', "
                "'date yesterday', 'delete', or 'undo'.",
            )
            return result
        if action == "delete":
            if meal.deleted:
                raise MealError("That meal is already deleted.")
            meal = await revise_meal(
                connection,
                meal.id,
                meal.revision_id,
                action_key=action_key,
                deleted=True,
                operation="delete",
            )
        elif action == "undo":
            meal = await undo_meal(connection, meal.id, meal.revision_id, action_key=action_key)
        else:
            raise MealError("Unknown meal action.")
        return receipt(meal, "Updated")
    except MealError as exc:
        return MealReply(str(exc) + "\nNothing was changed.", "meal_rejected")
