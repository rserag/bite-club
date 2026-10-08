"""Local quantity drafts: explicit Telegram consent, never text-based estimate approval."""

import re
import time
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from aiogram.types import Message
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.drafts import (
    close_draft,
    create_draft,
    get_draft,
    revise_draft,
    touch_draft,
    track_draft_action,
)
from nutrition_bot.adapters.database.foods import get_food_version
from nutrition_bot.adapters.database.meals import (
    MealError,
    MealItemInput,
    MealSnapshot,
    create_meal,
    revise_meal,
)
from nutrition_bot.adapters.database.schema import outbox
from nutrition_bot.adapters.database.schema_drafts import draft_action_links, meal_drafts
from nutrition_bot.application.meal_conversation import (
    MealReply,
    _date_timestamp,
    _resolve_items,
    _short,
    receipt,
)
from nutrition_bot.application.portion_suggestions import suggest_portion
from nutrition_bot.application.recipe_display import portion_mass, share_lines
from nutrition_bot.domain.drafts import DraftContent, DraftError, MealDraft, PlannedItem
from nutrition_bot.domain.food import MAX_INTEGER, grams_to_milligrams, milligrams_to_grams
from nutrition_bot.domain.meal_draft_text import ParsedDraftMeal, parse_draft_meal
from nutrition_bot.domain.meal_text import MealTextError, MeasuredFood, ParsedMeal
from nutrition_bot.domain.recipe_portions import RecipeError

DRAFT_REFERENCE = r"D([1-9][0-9]{0,18})(?:r([1-9][0-9]{0,18}))?"
MASS_ONLY = (
    r"(?:about\s+|around\s+|approximately\s+|approx\s+|roughly\s+|estimated\s+|~\s*)?"
    r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s*(?:mg|kg|g)"
)
TERMINAL_TEXT = {
    "expired": "This draft expired after seven days without activity. "
    "Start a new meal draft; nothing was logged from this approval.",
    "cancelled": "This draft was cancelled. Start a new meal draft if needed; "
    "nothing was logged from this approval.",
    "saved": "This draft was already saved. It cannot create another meal.",
}


async def _planned(
    connection: AsyncConnection, parsed: ParsedDraftMeal, previous: MealSnapshot | None = None
) -> tuple[PlannedItem, ...]:
    result = []
    for item in parsed.items:
        resolved = await _resolve_items(
            connection,
            ParsedMeal(
                "Meal", parsed.local_date, (MeasuredFood(item.query, Decimal(1), "1", "g"),)
            ),
            previous,
        )
        food = await get_food_version(connection, resolved[0].food_version_id)
        if food.record.preparation == "unspecified":
            raise MealError("Choose a food with a clear raw, cooked or packaged preparation first.")
        if item.grams is None:
            suggestion = await suggest_portion(connection, food.version_id)
            mass = suggestion.edible_milligrams if suggestion else None
            quantity = format(milligrams_to_grams(mass), "f") if mass is not None else None
            basis = suggestion.basis if suggestion else None
            unit = "g" if mass is not None else None
        else:
            mass = grams_to_milligrams(item.grams)
            quantity, unit, basis = item.original_quantity, item.original_unit, item.estimate_basis
        result.append(
            PlannedItem(
                food_version_id=food.version_id,
                edible_milligrams=mass,
                original_quantity=quantity,
                original_unit=unit,
                estimate_basis=basis,
            )
        )
    return tuple(result)


async def draft_receipt(
    connection: AsyncConnection, draft: MealDraft, lead: str = "Meal draft"
) -> MealReply:
    if draft.state != "open" or draft.content is None:
        text = TERMINAL_TEXT.get(draft.state, "This draft is unavailable.")
        if draft.saved_meal_id is not None:
            text += f" Open its receipt with /meal M{draft.saved_meal_id}."
        return MealReply(text, "draft_status")
    content = draft.content
    lines = [
        f"{lead} D{draft.id}r{draft.revision} · {content.local_date} · {_short(content.label, 40)}"
    ]
    if content.target_meal_id is not None:
        lines.append(
            f"Proposed correction to M{content.target_meal_id}; the saved meal is unchanged."
        )
    lines.extend(share_lines(item.recipe_share for item in content.items))
    for index, item in enumerate(content.items, 1):
        food = await get_food_version(connection, item.food_version_id)
        amount = (
            portion_mass(item.edible_milligrams, item.recipe_share)
            if item.edible_milligrams is not None
            else "amount needed"
        )
        state = (
            "estimate"
            if item.estimate_basis
            else "calculated"
            if item.recipe_share
            else "measured"
            if item.edible_milligrams is not None
            else "unresolved"
        )
        lines.append(
            f"{index}. {amount} · {_short(food.record.name, 30 if item.recipe_share else 40)} "
            f"({food.record.preparation}; #{food.version_id}; {state})"
        )
        if item.estimate_basis:
            lines.append(
                "   Basis: "
                + _short(" ".join(item.estimate_basis.split()), 45 if item.recipe_share else 80)
            )
    unresolved = any(item.edible_milligrams is None for item in content.items)
    lines.append(
        "Not in your totals. Review the food choices and quantities, then tap Approve draft."
        if content.review_required
        else "Not in your totals. No food is saved until the quantities are resolved "
        "or you tap Approve estimate."
    )
    if unresolved:
        lines.append("An amount is still missing, so this draft cannot be approved yet.")
    if any(item.recipe_share for item in content.items):
        lines.append(
            "Change with 'portion 250g' (or servings). Recipe ingredient/yield estimates "
            "still require approval; update the recipe to change those assumptions."
        )
    else:
        lines.append("Reply 'item 1: 150g' with a measured amount.")
    lines.append(
        "You can also reply 'replace: ...', 'date yesterday', or 'cancel'. "
        "Text such as 'yes' never approves estimates."
    )
    lines.append("Use /drafts to resume. Expires after seven days without opening or editing it.")
    buttons = ("edit", "cancel") if unresolved else ("edit", "approve", "cancel")
    return MealReply(
        "\n".join(lines),
        "draft_receipt",
        buttons=buttons,
        draft_id=draft.id,
        draft_revision=draft.revision,
        review_required=content.review_required,
    )


async def new_draft(
    connection: AsyncConnection,
    text: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
) -> MealReply:
    parsed = parse_draft_meal(text, reference.date())
    items = await _planned(connection, parsed)
    content = DraftContent(
        label=parsed.label,
        local_date=parsed.local_date,
        timezone=str(reference.tzinfo),
        consumed_at=_date_timestamp(parsed.local_date, reference),
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        items=items,
    )
    if all(item.edible_milligrams is not None and item.estimate_basis is None for item in items):
        raise MealError("Send the measured meal as a new message; no estimate approval is needed.")
    draft = await create_draft(connection, content, action_key=action_key, now=time.time())
    return await draft_receipt(connection, draft)


def _correction_parse(text: str, today: date) -> ParsedDraftMeal:
    prefix = re.sub(r"^/meal(?:@[A-Za-z0-9_]+)?\s+", "", text, flags=re.IGNORECASE)
    prefix = re.sub(r"^I\s+ate\s+", "", prefix, flags=re.IGNORECASE)
    if re.match(r"(?:today|yesterday|[0-9]{4}-[0-9]{2}-[0-9]{2})(?:\s|$)", prefix, re.IGNORECASE):
        raise MealError("Change the date separately with 'date yesterday' or 'date YYYY-MM-DD'.")
    parsed = parse_draft_meal(text, today)
    if parsed.label != "Meal" or parsed.local_date != today:
        raise MealError("Use item amounts only; change the date separately.")
    return parsed


async def _changed_items(
    connection: AsyncConnection,
    items: tuple[PlannedItem, ...],
    text: str,
    today: date,
    previous: MealSnapshot | None = None,
) -> tuple[PlannedItem, ...]:
    replacement = re.fullmatch(
        r"(?:replace:|replace meal:)\s*(.+)", text, re.IGNORECASE | re.DOTALL
    )
    if replacement:
        return await _planned(connection, _correction_parse(replacement[1], today), previous)
    if any(item.recipe_share for item in items):
        if re.match(r"portion(?:\s|$)", text, re.IGNORECASE):
            from nutrition_bot.application.recipe_conversation import revised_recipe_items

            return await revised_recipe_items(connection, items, text)
        raise MealError(
            "For a recipe use 'portion 250g' (or servings), or 'replace: ...' "
            "to replace all ingredients with directly logged foods."
        )
    indexed = re.fullmatch(r"item\s+([1-9][0-9]{0,18}):\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if indexed:
        index, body = int(indexed[1]) - 1, indexed[2]
        if index >= len(items):
            raise MealError("That item number is not in this draft.")
    else:
        natural = re.fullmatch(r"(.+?)\s+was\s+(.+)", text, re.IGNORECASE | re.DOTALL)
        if natural:
            matches = []
            for i, item in enumerate(items):
                food = await get_food_version(connection, item.food_version_id)
                if food.record.name.casefold() == natural[1].casefold():
                    matches.append(i)
            if len(matches) != 1:
                raise MealError("Choose the item number, e.g. 'item 1: 150g'.")
            index, body = matches[0], natural[2]
        elif len(items) == 1:
            index, body = 0, text
        else:
            raise MealError("Choose an item, e.g. 'item 1: 150g', or use 'replace: ...'.")
    updated = list(items)
    if body.casefold() == "delete":
        if len(items) == 1:
            raise MealError("Use Cancel to discard this one-item draft.")
        del updated[index]
    else:
        if re.fullmatch(MASS_ONLY, body, re.IGNORECASE):
            body += f" #{items[index].food_version_id}"
        parsed = _correction_parse(body, today)
        if len(parsed.items) != 1:
            raise MealError("Change one item at a time, or use 'replace: ...'.")
        updated[index] = (await _planned(connection, parsed, previous))[0]
    return tuple(updated)


async def correction_draft(
    connection: AsyncConnection,
    meal: MealSnapshot,
    text: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
) -> MealReply:
    current_items = tuple(
        PlannedItem(
            recipe_share=item.recipe_share,
            food_version_id=item.food_version_id,
            edible_milligrams=item.edible_milligrams,
            original_quantity=item.original_quantity,
            original_unit=item.original_unit,
            estimate_basis=item.quantity_basis
            if item.quantity_method == "approved_estimate"
            else None,
        )
        for item in meal.items
    )
    updated = await _changed_items(connection, current_items, text, reference.date(), meal)
    content = DraftContent(
        label=meal.label,
        local_date=meal.local_date,
        timezone=meal.timezone,
        consumed_at=meal.consumed_at,
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        items=updated,
        target_meal_id=meal.id,
        target_revision_id=meal.revision_id,
    )
    draft = await create_draft(connection, content, action_key=action_key, now=time.time())
    return await draft_receipt(connection, draft, "Correction draft")


async def _finalize(
    connection: AsyncConnection, draft: MealDraft, *, action_key: str, approve: bool, now: float
) -> MealReply:
    content = draft.content
    assert content is not None
    if content.review_required and not approve:
        raise MealError("Review this draft and tap its approval button before saving.")
    items = []
    for item in content.items:
        if (
            item.edible_milligrams is None
            or item.original_quantity is None
            or item.original_unit is None
        ):
            raise MealError("Enter every missing amount before approving this draft.")
        if item.estimate_basis and not approve:
            raise MealError("Use the Approve estimate button for the displayed draft.")
        estimated = item.estimate_basis is not None
        items.append(
            MealItemInput(
                recipe_share=item.recipe_share,
                food_version_id=item.food_version_id,
                edible_milligrams=item.edible_milligrams,
                original_quantity=item.original_quantity,
                original_unit=item.original_unit,
                quantity_method="approved_estimate" if estimated else "measured",
                quantity_basis=item.estimate_basis,
                approval_action_key=action_key if estimated else None,
                approved_at=now if estimated else None,
                approval_draft_id=draft.id if estimated else None,
                approval_draft_revision=draft.revision if estimated else None,
            )
        )
    if content.target_meal_id is not None and content.target_revision_id is not None:
        saved = await revise_meal(
            connection,
            content.target_meal_id,
            content.target_revision_id,
            action_key=action_key,
            items=tuple(items),
            label=content.label,
            local_date=content.local_date,
            consumed_at=content.consumed_at,
            deleted=False,
        )
    else:
        saved = await create_meal(
            connection,
            items=tuple(items),
            label=content.label,
            local_date=content.local_date,
            timezone=content.timezone,
            consumed_at=content.consumed_at,
            action_key=action_key,
            source_chat_id=content.source_chat_id,
            source_message_id=content.source_message_id,
        )
    await close_draft(
        connection,
        draft.id,
        draft.revision,
        state="saved",
        action_key=action_key,
        now=now,
        saved_meal_id=saved.id,
    )
    return receipt(
        saved,
        "Saved with approved estimates"
        if any(item.estimate_basis for item in content.items)
        else "Saved reviewed meal"
        if content.review_required
        else "Saved measured meal",
    )


async def _edit(
    connection: AsyncConnection,
    draft: MealDraft,
    text: str,
    *,
    action_key: str,
    reference: datetime,
) -> MealReply:
    assert draft.content is not None
    if text.casefold() in {"cancel", "/cancel"}:
        closed = await close_draft(
            connection,
            draft.id,
            draft.revision,
            state="cancelled",
            action_key=action_key,
            now=time.time(),
        )
        return await draft_receipt(connection, closed)
    if text.casefold() in {"yes", "ok", "okay", "approve", "approve estimate", "save"}:
        return await draft_receipt(connection, draft, "Tap the button to approve; draft unchanged")
    content = draft.content
    date_match = re.fullmatch(
        r"date\s+(today|yesterday|[0-9]{4}-[0-9]{2}-[0-9]{2})", text, re.IGNORECASE
    )
    if date_match:
        from zoneinfo import ZoneInfo

        day = parse_draft_meal(f"{date_match[1]} 1g #1", reference.date()).local_date
        anchor = _date_timestamp(day, reference.astimezone(ZoneInfo(content.timezone)))
        content = content.model_copy(update={"local_date": day, "consumed_at": anchor})
    else:
        content = content.model_copy(
            update={
                "items": await _changed_items(connection, content.items, text, reference.date())
            }
        )
    changed = await revise_draft(
        connection, draft.id, draft.revision, content, action_key=action_key, now=time.time()
    )
    if not content.review_required and all(
        item.edible_milligrams is not None and item.estimate_basis is None for item in content.items
    ):
        return await _finalize(
            connection, changed, action_key=action_key, approve=False, now=time.time()
        )
    return await draft_receipt(connection, changed, "Updated draft")


async def _referenced(
    connection: AsyncConnection, draft_id: int, revision: int | None, *, action_key: str
) -> tuple[MealDraft, bool]:
    draft = await get_draft(connection, draft_id)
    if draft is None:
        raise MealError("That draft is unavailable. Use /drafts to find an open draft.")
    await track_draft_action(connection, draft.id, action_key)
    return draft, revision is not None and revision != draft.revision


async def handle_draft_callback(
    connection: AsyncConnection, action: str, draft_id: int, revision: int, *, action_key: str
) -> MealReply:
    try:
        draft, stale = await _referenced(connection, draft_id, revision, action_key=action_key)
        if draft.state != "open":
            return await draft_receipt(connection, draft)
        if stale:
            return await draft_receipt(
                connection, draft, "Old button; nothing saved. Current draft"
            )
        now = time.time()
        draft = await touch_draft(
            connection, draft.id, draft.revision, action_key=action_key, now=now
        )
        if action == "approve":
            return await _finalize(connection, draft, action_key=action_key, approve=True, now=now)
        if action == "cancel":
            draft = await close_draft(
                connection,
                draft.id,
                draft.revision,
                state="cancelled",
                action_key=action_key,
                now=now,
            )
        elif action != "edit":
            raise MealError("Unknown draft action; nothing was saved.")
        return await draft_receipt(connection, draft, "Enter a measured amount by replying. Draft")
    except ValidationError:
        return MealReply(
            "That draft amount or date is unsupported. Enter a short amount in grams.",
            "meal_rejected",
        )
    except (DraftError, MealError, MealTextError, RecipeError) as exc:
        return MealReply(str(exc), "meal_rejected")


async def track_message_reference(
    connection: AsyncConnection,
    message: Message,
    *,
    action_key: str,
    bot_id: int,
    owner_id: int,
    retention_days: int,
    edited: bool = False,
) -> None:
    """Track retained content outside mutation savepoints, including rejected edits."""
    identifiers: set[int] = set()
    text = (message.text or "").strip()
    explicit = re.match(
        r"^/(?:draft|cancel)(?:@[A-Za-z0-9_]+)?\s+D([1-9][0-9]{0,18})(?:r[0-9]+)?(?:\s|$)",
        text,
        re.IGNORECASE,
    )
    if explicit and int(explicit[1]) <= MAX_INTEGER:
        identifiers.add(int(explicit[1]))
    if edited:
        origin = await connection.scalar(
            sa.select(meal_drafts.c.id).where(
                meal_drafts.c.source_chat_id == message.chat.id,
                meal_drafts.c.source_message_id == message.message_id,
            )
        )
        if origin is not None:
            identifiers.add(origin)
    target = message.reply_to_message
    if target and target.from_user and target.from_user.is_bot and target.from_user.id == bot_id:
        linked: sa.ScalarResult[int] = await connection.scalars(
            sa.select(draft_action_links.c.draft_id)
            .join(outbox, outbox.c.action_key == draft_action_links.c.action_key)
            .where(
                outbox.c.telegram_message_id == target.message_id,
                outbox.c.chat_id == message.chat.id,
                outbox.c.owner_user_id == owner_id,
                outbox.c.status == "sent",
            )
        )
        # Privacy association remains possible after raw receipt payload expiry.
        # This does not grant edit/approval authority to an old receipt.
        identifiers.update(linked.all())
    for draft_id in identifiers:
        if 1 <= draft_id <= MAX_INTEGER and await get_draft(connection, draft_id) is not None:
            await track_draft_action(connection, draft_id, action_key)


async def handle_draft_message(
    connection: AsyncConnection,
    message: Message,
    text: str,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
) -> MealReply | None:
    if text.casefold() == "/drafts":
        rows: Sequence[int] = (
            (
                await connection.execute(
                    sa.select(meal_drafts.c.id)
                    .where(meal_drafts.c.state == "open")
                    .order_by(meal_drafts.c.id.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return MealReply(
                "No open drafts. Send a meal with measured amounts or a rough amount "
                "such as 'about 150g rice'.",
                "draft_list",
            )
        lines = ["Open drafts (not in totals):"]
        for listed_draft_id in rows:
            draft = await get_draft(connection, listed_draft_id)
            if draft and draft.content:
                await track_draft_action(connection, draft.id, action_key)
                lines.append(
                    f"D{draft.id}r{draft.revision} · {draft.content.local_date} "
                    f"· {_short(draft.content.label, 50)}"
                )
        return MealReply("\n".join(lines) + "\nOpen one with /draft D<number>.", "draft_list")
    explicit = re.fullmatch(
        r"/(draft|cancel)\s+(" + DRAFT_REFERENCE + r")(?:\s+(.+))?", text, re.IGNORECASE | re.DOTALL
    )
    draft_id = revision = None
    body = text
    open_only = False
    if explicit:
        draft_id = int(explicit[3])
        revision = int(explicit[4]) if explicit[4] else None
        body = "cancel" if explicit[1].casefold() == "cancel" else (explicit[5] or "")
        if explicit[1].casefold() == "cancel" and explicit[5]:
            raise MealError("Cancel does not accept extra text.")
        open_only = not body
        if not open_only and revision is None:
            raise MealError(
                "Use the draft's current revision, e.g. /draft D1r2 item 1: 150g, "
                "or reply to its receipt."
            )
    elif message.reply_to_message is not None:
        target = message.reply_to_message
        if target.from_user and target.from_user.is_bot and target.from_user.id == bot_id:
            payload = await connection.scalar(
                sa.select(outbox.c.payload)
                .where(
                    outbox.c.telegram_message_id == target.message_id,
                    outbox.c.chat_id == message.chat.id,
                    outbox.c.owner_user_id == owner_id,
                    outbox.c.status == "sent",
                    outbox.c.sent_at >= time.time() - retention_days * 86400,
                )
                .order_by(outbox.c.id.desc())
                .limit(1)
            )
            if (
                isinstance(payload, dict)
                and type(payload.get("draft_id")) is int
                and type(payload.get("draft_revision")) is int
            ):
                draft_id, revision = payload["draft_id"], payload["draft_revision"]
    if draft_id is None:
        if text.casefold() in {"/draft", "/cancel"}:
            return MealReply(
                "Use /drafts, open a draft, then reply with a correction or Cancel.", "draft_help"
            )
        return None
    draft, stale = await _referenced(connection, draft_id, revision, action_key=action_key)
    if draft.state != "open":
        return await draft_receipt(connection, draft)
    if stale:
        return await draft_receipt(connection, draft, "Old receipt; nothing changed. Current draft")
    if open_only:
        draft = await touch_draft(
            connection, draft.id, draft.revision, action_key=action_key, now=time.time()
        )
        return await draft_receipt(connection, draft)
    return await _edit(connection, draft, body, action_key=action_key, reference=reference)
