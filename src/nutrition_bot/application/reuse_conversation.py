"""Explicit personal shortcuts, with a new consent boundary for every estimate."""

import re
import time
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from aiogram.types import Message
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.aliases import (
    create_alias,
    get_alias,
    list_aliases,
    resolve_alias,
    update_alias,
)
from nutrition_bot.adapters.database.drafts import create_draft
from nutrition_bot.adapters.database.favorites import (
    create_favorite,
    find_favorite,
    get_favorite,
    list_favorites,
    revise_favorite,
)
from nutrition_bot.adapters.database.foods import get_food_version
from nutrition_bot.adapters.database.meals import MealError, MealItemInput, create_meal, get_meal
from nutrition_bot.adapters.database.schema import meal_revisions, meals
from nutrition_bot.application.meal_conversation import (
    REFERENCE,
    MealReply,
    _catalog_rows,
    _date_timestamp,
    _explicit_target,
    _reply_target,
    _short,
    receipt,
)
from nutrition_bot.application.recipe_display import portion_mass, share_lines
from nutrition_bot.domain.aliases import Alias
from nutrition_bot.domain.drafts import DraftContent, DraftError, PlannedItem
from nutrition_bot.domain.meal_draft_text import parse_draft_meal
from nutrition_bot.domain.meal_text import MealTextError, _parse_date
from nutrition_bot.domain.recipe_portions import RecipeError
from nutrition_bot.domain.reuse import (
    FavoriteSnapshot,
    ReuseError,
    planned_from_meal,
    scale_items,
)

FAVORITE_REF = r"F([1-9][0-9]*)(?:v([1-9][0-9]*))?"
ALIAS_REF = r"A([1-9][0-9]*)(?:r([1-9][0-9]*))?"


async def _alias_view(connection: AsyncConnection, alias: Alias) -> MealReply:
    food = await get_food_version(connection, alias.food_version_id)
    ref = f"A{alias.id}r{alias.revision}"
    return MealReply(
        f"Alias {ref} · {alias.name} · {'active' if alias.active else 'disabled'}\n"
        f"#{alias.food_version_id} · {_short(food.record.name)} · {food.record.preparation}\n"
        f"Use: 150g {alias.name}\n"
        f"Change: /alias update {ref} = #<food version>\n"
        f"{'Disable' if alias.active else 'Enable'}: "
        f"/alias {'off' if alias.active else 'on'} {ref}\nNothing was logged.",
        "alias_view",
    )


async def _aliases(connection: AsyncConnection, text: str, *, action_key: str) -> MealReply | None:
    if re.fullmatch(r"/aliases(?:\s+.*)?", text, re.IGNORECASE):
        query = text[len("/aliases") :].strip()
        values = await list_aliases(connection, query=query, limit=11)
        return MealReply(
            "Personal aliases (up to 10 shown):\n"
            + (
                "\n".join(
                    f"A{v.id}r{v.revision} · {v.name} → #{v.food_version_id}"
                    + (" · disabled" if not v.active else "")
                    for v in values[:10]
                )
                or "No matching aliases."
            )
            + ("\nMore matches: narrow the /aliases search." if len(values) > 10 else "")
            + "\nCreate: /alias my rice = #12\nView: /alias A1\nNothing was logged.",
            "alias_list",
        )
    if not re.match(r"/alias(?:\s|$)", text, re.IGNORECASE):
        return None
    body = text[len("/alias") :].strip()
    updated = re.fullmatch(
        r"update\s+A([1-9][0-9]*)r([1-9][0-9]*)\s*=\s*#([1-9][0-9]*)", body, re.IGNORECASE
    )
    toggle = re.fullmatch(r"(on|off)\s+A([1-9][0-9]*)r([1-9][0-9]*)", body, re.IGNORECASE)
    view = re.fullmatch(ALIAS_REF, body, re.IGNORECASE)
    create = re.fullmatch(r"(.+?)\s*=\s*#([1-9][0-9]*)", body)
    if updated:
        alias = await update_alias(
            connection,
            int(updated[1]),
            int(updated[2]),
            food_version_id=int(updated[3]),
            action_key=action_key,
        )
    elif toggle:
        alias = await update_alias(
            connection,
            int(toggle[2]),
            int(toggle[3]),
            active=toggle[1].casefold() == "on",
            action_key=action_key,
        )
    elif view:
        found = await get_alias(connection, int(view[1]))
        if found is None:
            raise ReuseError("That alias does not exist. Use /aliases.")
        if view[2] is not None and int(view[2]) != found.revision:
            raise ReuseError(f"That alias changed. Open /alias A{found.id}.")
        alias = found
    elif create and not re.match(r"(?:update|on|off)(?:\s|$)", body, re.IGNORECASE):
        alias = await create_alias(connection, create[1], int(create[2]), action_key=action_key)
    else:
        raise ReuseError(
            "Use /alias my rice = #12, /alias A1, /alias update A1r1 = #13, or /alias off A1r1."
        )
    return await _alias_view(connection, alias)


async def favorite_view(
    connection: AsyncConnection, favorite: FavoriteSnapshot, lead: str = "Favorite"
) -> MealReply:
    lines = [
        f"{lead} F{favorite.id}v{favorite.version_number} · {favorite.name}"
        + (" · archived" if favorite.archived else "")
    ]
    lines.extend(share_lines(item.recipe_share for item in favorite.items))
    for index, item in enumerate(favorite.items, 1):
        food = await get_food_version(connection, item.food_version_id)
        assert item.edible_milligrams is not None
        lines.append(
            f"{index}. {portion_mass(item.edible_milligrams, item.recipe_share)} "
            f"{_short(food.record.name, 35 if item.recipe_share else 55)} "
            f"({food.record.preparation}; #{item.food_version_id})"
            + (" · estimate: fresh approval required" if item.estimate_basis else "")
        )
        if item.estimate_basis:
            lines.append("   Basis: " + _short(" ".join(item.estimate_basis.split()), 60))
    ref = f"F{favorite.id}v{favorite.version_number}"
    lines.append(
        "Nothing was logged. "
        + (
            f"Restore: /favorite restore {ref}."
            if favorite.archived
            else f"Use /eat {ref}, optionally x0.5; update: /favorite update {ref} = M1r1."
        )
    )
    return MealReply(
        "\n".join(lines),
        "favorite_view",
        buttons=("restore",) if favorite.archived else ("log", "archive"),
        favorite_id=favorite.id,
        favorite_version_id=favorite.version_id,
    )


async def _favorite_target(
    connection: AsyncConnection, text: str, *, require_version: bool, allow_archived: bool = False
) -> FavoriteSnapshot:
    matched = re.fullmatch(FAVORITE_REF, text, re.IGNORECASE)
    if matched:
        favorite = await get_favorite(connection, int(matched[1]))
        if favorite is not None and (
            (require_version and matched[2] is None)
            or (matched[2] is not None and int(matched[2]) != favorite.version_number)
        ):
            raise ReuseError(
                f"Use the current favorite: /favorite F{favorite.id}. "
                "The supplied version is missing or stale."
            )
    else:
        favorite = await find_favorite(connection, text)
    if favorite is None:
        raise ReuseError("That favorite does not exist. Use /favorites.")
    if favorite.archived and not allow_archived:
        raise ReuseError(f"That favorite is archived. Open /favorite F{favorite.id} to restore it.")
    return favorite


def _reuse_request(body: str, reference: datetime) -> tuple[str, date, Decimal]:
    day, body = _parse_date(body, reference.date())
    factor = Decimal(1)
    scaling = re.search(r"\s+x([^\s]+)$", body, re.IGNORECASE)
    if scaling:
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,3})?", scaling[1]):
            raise ReuseError("Use x0.5 or x2, with at most three decimals.")
        factor = Decimal(scaling[1])
        body = body[: scaling.start()].strip()
    if not body:
        raise ReuseError("Choose a favorite or meal reference first.")
    return body, day, factor


async def _consume(
    connection: AsyncConnection,
    items: tuple[PlannedItem, ...],
    *,
    label: str,
    factor: Decimal,
    day: date,
    reference: datetime,
    message: Message,
    action_key: str,
    lead: str,
) -> MealReply:
    from nutrition_bot.application.draft_conversation import draft_receipt

    portions = scale_items(items, factor)
    timezone = str(reference.tzinfo)
    consumed_at = _date_timestamp(day, reference)
    if any(item.estimate_basis for item in portions):
        draft = await create_draft(
            connection,
            DraftContent(
                label=label,
                local_date=day,
                timezone=timezone,
                consumed_at=consumed_at,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                items=portions,
            ),
            action_key=action_key,
            now=time.time(),
        )
        return await draft_receipt(connection, draft, lead + "; fresh approval required. Draft")
    exact = []
    for item in portions:
        assert item.edible_milligrams is not None
        assert item.original_quantity is not None and item.original_unit is not None
        exact.append(
            MealItemInput(
                recipe_share=item.recipe_share,
                food_version_id=item.food_version_id,
                edible_milligrams=item.edible_milligrams,
                original_quantity=item.original_quantity,
                original_unit=item.original_unit,
            )
        )
    meal = await create_meal(
        connection,
        items=tuple(exact),
        label=label,
        local_date=day,
        timezone=timezone,
        consumed_at=consumed_at,
        action_key=action_key,
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
    )
    return receipt(meal, "Saved from " + lead)


async def handle_reuse_message(
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
    alias_reply = await _aliases(connection, text, action_key=action_key)
    if alias_reply is not None:
        return alias_reply
    if re.fullmatch(r"/favorites(?:\s+all)?", text, re.IGNORECASE):
        values = await list_favorites(
            connection, limit=11, include_archived=text.casefold().endswith(" all")
        )
        return MealReply(
            "Favorites (up to 10 shown):\n"
            + (
                "\n".join(
                    f"F{v.id}v{v.version_number} · {v.name}" + (" · archived" if v.archived else "")
                    for v in values[:10]
                )
                or "No saved favorites."
            )
            + (
                "\nMore favorites exist; open one by its exact name or F number."
                if len(values) > 10
                else ""
            )
            + "\nOpen: /favorite F1; log: /eat usual breakfast.\n"
            "Reply to a meal receipt: save as usual breakfast. Nothing was logged.",
            "favorite_list",
        )
    saving = re.fullmatch(
        r"/favorite\s+save\s+(.+?)\s*=\s*(" + REFERENCE + ")", text, re.IGNORECASE
    )
    if saving:
        meal = await _explicit_target(connection, saving[2], mutation=True)
        favorite = await create_favorite(connection, saving[1], meal, action_key=action_key)
        return await favorite_view(connection, favorite, "Saved favorite")
    updating = re.fullmatch(
        r"/favorite\s+update\s+(F[1-9][0-9]*v[1-9][0-9]*)\s*=\s*(" + REFERENCE + ")",
        text,
        re.IGNORECASE,
    )
    toggle = re.fullmatch(
        r"/favorite\s+(archive|restore)\s+(F[1-9][0-9]*v[1-9][0-9]*)", text, re.IGNORECASE
    )
    if updating or toggle:
        if updating:
            selected = updating[1]
        else:
            assert toggle is not None
            selected = toggle[2]
        favorite = await _favorite_target(
            connection, selected, require_version=True, allow_archived=True
        )
        replacement = (
            await _explicit_target(connection, updating[2], mutation=True) if updating else None
        )
        favorite = await revise_favorite(
            connection,
            favorite.id,
            favorite.version_id,
            action_key=action_key,
            meal=replacement,
            archived=(toggle[1].casefold() == "archive") if toggle else None,
        )
        return await favorite_view(connection, favorite, "Updated favorite")
    viewing = re.fullmatch(r"/favorite\s+(.+)", text, re.IGNORECASE)
    if viewing:
        favorite = await _favorite_target(
            connection, viewing[1], require_version=False, allow_archived=True
        )
        return await favorite_view(connection, favorite)
    if text.casefold() == "/favorite":
        return MealReply(
            "Reply to a meal receipt: save as usual breakfast. "
            "Or /favorite save usual breakfast = M1r1. "
            "Use /favorites to see saved meals."
        )
    reply_save = re.fullmatch(r"save as\s+(.+)", text, re.IGNORECASE)
    reply_repeat = text.casefold() == "same again"
    if reply_save or reply_repeat:
        replied_meal = await _reply_target(connection, message, bot_id, owner_id, retention_days)
        if replied_meal is None:
            raise ReuseError("Reply to a current meal receipt, or choose one with /meals.")
        meal = replied_meal
        if reply_save:
            favorite = await create_favorite(connection, reply_save[1], meal, action_key=action_key)
            return await favorite_view(connection, favorite, "Saved favorite")
        return await _consume(
            connection,
            planned_from_meal(meal),
            label=meal.label,
            factor=Decimal(1),
            day=reference.date(),
            reference=reference,
            message=message,
            action_key=action_key,
            lead=f"M{meal.id}r{meal.revision_number}",
        )
    repeat = re.fullmatch(r"/repeat\s+(.+)", text, re.IGNORECASE)
    if repeat:
        source, day, factor = _reuse_request(repeat[1], reference)
        meal = await _explicit_target(connection, source, mutation=True)
        return await _consume(
            connection,
            planned_from_meal(meal),
            label=meal.label,
            factor=factor,
            day=day,
            reference=reference,
            message=message,
            action_key=action_key,
            lead=f"M{meal.id}r{meal.revision_number}",
        )
    yesterday = re.fullmatch(r"same\s+(.+?)\s+as yesterday", text, re.IGNORECASE)
    if yesterday:
        day = reference.date() - timedelta(days=1)
        ids: Sequence[int] = (
            (
                await connection.execute(
                    sa.select(meals.c.id)
                    .join(meal_revisions, meal_revisions.c.id == meals.c.current_revision_id)
                    .where(
                        meal_revisions.c.local_date == day,
                        meal_revisions.c.deleted.is_(False),
                        sa.func.unicode_casefold(meal_revisions.c.label) == yesterday[1].casefold(),
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(ids) != 1:
            raise ReuseError(
                "Choose the exact meal with /meals, then /repeat M1r1. "
                "Yesterday's label has no unique match."
            )
        meal = await get_meal(connection, ids[0])
        return await _consume(
            connection,
            planned_from_meal(meal),
            label=meal.label,
            factor=Decimal(1),
            day=reference.date(),
            reference=reference,
            message=message,
            action_key=action_key,
            lead=f"M{meal.id}r{meal.revision_number}",
        )
    eating = re.fullmatch(r"/eat\s+(.+)", text, re.IGNORECASE)
    if eating:
        source, day, factor = _reuse_request(eating[1], reference)
        favorite = await _favorite_target(connection, source, require_version=True)
    else:
        # A reply already has correction context. Do not consume a favorite implicitly.
        if message.reply_to_message is not None or text.startswith("/"):
            return None
        source = re.sub(r"^I\s+ate\s+", "", text, flags=re.IGNORECASE)
        # Explicit food amounts and qualifiers retain their normal parsing/consent
        # semantics even if a favorite happens to have that exact name.
        try:
            parsed = parse_draft_meal(source, reference.date())
            if (
                source.startswith("#")
                or len(parsed.items) != 1
                or parsed.items[0].query != source
                or parsed.items[0].grams is not None
                or parsed.items[0].estimate_basis is not None
                or parsed.label != "Meal"
                or parsed.local_date != reference.date()
            ):
                return None
        except MealTextError:
            return None
        try:
            found = await find_favorite(connection, source)
        except ReuseError:
            return None
        if found is None:
            return None
        favorite = found
        if await resolve_alias(connection, source) or await _catalog_rows(
            connection, source, exact=True
        ):
            raise ReuseError(
                f"That name also identifies a food. To log the favorite use "
                f"/eat F{favorite.id}v{favorite.version_number}."
            )
        day, factor = reference.date(), Decimal(1)
    return await _consume(
        connection,
        favorite.items,
        label=favorite.name,
        factor=factor,
        day=day,
        reference=reference,
        message=message,
        action_key=action_key,
        lead=f"F{favorite.id}v{favorite.version_number}",
    )


async def handle_favorite_callback(
    connection: AsyncConnection,
    action: str,
    favorite_id: int,
    version_id: int,
    *,
    message: Message,
    reference: datetime,
    action_key: str,
) -> MealReply:
    try:
        favorite = await get_favorite(connection, favorite_id)
        if favorite is None:
            raise ReuseError("This favorite no longer exists.")
        if favorite.version_id != version_id:
            raise ReuseError(f"This button is stale. Open /favorite F{favorite.id} again.")
        if action == "log":
            if favorite.archived:
                raise ReuseError("Restore this favorite before logging it.")
            return await _consume(
                connection,
                favorite.items,
                label=favorite.name,
                factor=Decimal(1),
                day=reference.date(),
                reference=reference,
                message=message,
                action_key=action_key,
                lead=f"F{favorite.id}v{favorite.version_number}",
            )
        if action not in {"archive", "restore"}:
            raise ReuseError("Unknown favorite action.")
        favorite = await revise_favorite(
            connection, favorite.id, version_id, action_key=action_key, archived=action == "archive"
        )
        return await favorite_view(connection, favorite, "Updated favorite")
    except (ReuseError, MealError, DraftError, RecipeError) as exc:
        return MealReply(_short(str(exc), 2000) + "\nNothing was changed.", "meal_rejected")
    except ValidationError:
        return MealReply(
            "This favorite contains unsupported portions. Nothing was changed.", "meal_rejected"
        )
