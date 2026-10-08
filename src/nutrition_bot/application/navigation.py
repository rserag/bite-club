"""Small Telegram menus and guided entry using the existing application commands."""

import re
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

import sqlalchemy as sa
from aiogram.types import Message
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.meals import get_meal
from nutrition_bot.adapters.database.schema import food_versions, meals
from nutrition_bot.adapters.database.schema_favorites import favorite_versions, favorites
from nutrition_bot.adapters.database.schema_ui import ui_flows
from nutrition_bot.application.meal_conversation import MealReply, handle_message
from nutrition_bot.domain.food import exact_decimal, grams_to_milligrams

HOME = (
    ("Log meal", "log"),
    ("Today", "today"),
    ("Favorites", "favorites"),
    ("Training", "training"),
    ("Weight", "weight"),
    ("Recovery", "recovery"),
    ("Recent meals", "recent"),
    ("Settings", "settings"),
    ("Help & setup", "help"),
)


def menu(text: str, buttons: tuple[tuple[str, str], ...] = HOME) -> MealReply:
    return MealReply(text, "navigation", ui_buttons=buttons)


def home() -> MealReply:
    return menu(
        "Bite Club\nWhat would you like to do?\nYou can also send a measured meal directly."
    )


def _label(value: str | None, limit: int = 80) -> str:
    text = " ".join((value or "Saved food").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _review_buttons(revision: int, item_count: int) -> tuple[tuple[str, str], ...]:
    return (
        (("Save / review draft", f"flow:{revision}:save"),)
        + ((("Add another food", f"flow:{revision}:more"),) if item_count < 10 else ())
        + (("Cancel", "cancel"),)
    )


async def _flow(connection: AsyncConnection, owner: int) -> Any:
    row = (
        (await connection.execute(sa.select(ui_flows).where(ui_flows.c.owner_id == owner)))
        .mappings()
        .one_or_none()
    )
    if row and row["updated_at"] < time.time() - 1800:
        await cancel(connection, owner)
        return None
    return row if row and row["stage"] != "idle" else None


async def begin(
    connection: AsyncConnection, owner: int, stage: str, payload: dict[str, Any]
) -> int:
    previous = await connection.scalar(
        sa.select(ui_flows.c.revision).where(ui_flows.c.owner_id == owner)
    )
    revision = (previous or 0) + 1
    await connection.execute(
        insert(ui_flows)
        .values(
            owner_id=owner, revision=revision, stage=stage, payload=payload, updated_at=time.time()
        )
        .on_conflict_do_update(
            index_elements=[ui_flows.c.owner_id],
            set_={
                "revision": revision,
                "stage": stage,
                "payload": payload,
                "updated_at": time.time(),
            },
        )
    )
    return revision


async def cancel(connection: AsyncConnection, owner: int) -> None:
    await connection.execute(
        sa.update(ui_flows)
        .where(ui_flows.c.owner_id == owner)
        .values(stage="idle", payload={}, revision=ui_flows.c.revision + 1, updated_at=time.time())
    )


async def _command(
    connection: AsyncConnection,
    text: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
) -> MealReply:
    virtual = message.model_copy(update={"text": text, "reply_to_message": None, "date": reference})
    return await handle_message(
        connection,
        virtual,
        action_key=action_key,
        timezone=str(reference.tzinfo),
        bot_id=bot_id,
        owner_id=owner_id,
        retention_days=retention_days,
    )


async def ui_action(
    connection: AsyncConnection,
    action: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
) -> MealReply:
    context: dict[str, Any] = dict(
        action_key=action_key,
        reference=reference,
        bot_id=bot_id,
        owner_id=owner_id,
        retention_days=retention_days,
    )
    if action.startswith("flow:"):
        _, rev_text, operation = action.split(":", 2)
        row = await _flow(connection, owner_id)
        if row is None or row["revision"] != int(rev_text):
            return menu("That step has changed or expired. Start again from the menu.")
        payload = row["payload"]
        if operation.startswith("goal:") and row["stage"].startswith("goal_"):
            from nutrition_bot.application.guided_goals import goal_action

            return await goal_action(connection, row, operation, message, **context)
        if operation.startswith("food=") and row["stage"] == "food_search":
            food_id = int(operation[5:])
            allowed = payload.get("choices", [])
            if food_id not in allowed:
                return menu("Choose a food from the latest search.")
            name = await connection.scalar(
                sa.select(food_versions.c.name).where(food_versions.c.id == food_id)
            )
            await begin(
                connection,
                owner_id,
                "amount",
                {"food_id": food_id, "name": name, "items": payload.get("items", [])},
            )
            return menu(
                f"How much {_label(name)} did you eat?\n"
                "Send measured grams, e.g. 150 g. For a rough portion, send about 150 g.",
                (("Cancel", "cancel"),),
            )
        if operation == "more" and row["stage"] == "meal_review":
            if len(payload["items"]) >= 10:
                return menu(
                    "This meal already contains ten foods. Save it before adding another meal.",
                    _review_buttons(row["revision"], len(payload["items"])),
                )
            await begin(connection, owner_id, "food_search", {"items": payload["items"]})
            return menu("Which food would you like to add? Type its name.", (("Cancel", "cancel"),))
        if operation == "save" and row["stage"] == "meal_review":
            text = "/meal " + "; ".join(
                item["quantity"] + " #" + str(item["food_id"]) for item in payload["items"]
            )
            await cancel(connection, owner_id)
            return await _command(connection, text, message, **context)
        return menu("That step is no longer available. Start again.")
    if action.startswith("command:"):
        await cancel(connection, owner_id)
        return await _command(connection, action[8:], message, **context)
    await cancel(connection, owner_id)
    if action in {"home", "cancel"}:
        return home()
    if action == "log":
        await begin(connection, owner_id, "food_search", {"items": []})
        return menu(
            "Which food did you eat? Type its name to search your reviewed catalog.",
            (("Favorites", "favorites"), ("Recent meals", "recent"), ("Cancel", "cancel")),
        )
    if action in {"weight", "recovery", "gym", "bjj"}:
        await begin(connection, owner_id, action, {})
        prompts = {
            "weight": "What is your weight in kg? Send a number, e.g. 79.4.",
            "recovery": "How was your recovery? Send any available fields, e.g. sleep 7h, "
            "fatigue 2, soreness 3, readiness 4. Missing fields can stay "
            "unknown.",
            "gym": "What did you train, and for how long? E.g. chest and triceps for 40 mins.",
            "bjj": "How long was your BJJ session? E.g. 60 mins effort 5. You can add"
            " round details later.",
        }
        return menu(prompts[action], (("Cancel", "cancel"),))
    if action == "training":
        return menu(
            "Training\nLog a completed session or review your plans.",
            (
                ("Gym session", "gym"),
                ("BJJ session", "bjj"),
                ("Plans", "command:/plan"),
                ("Workload", "command:/load"),
                ("Home", "home"),
            ),
        )
    if action == "help":
        return menu(
            "Choose a topic. For regular use, start with Log meal or a saved favorite.",
            (
                ("Food & portions", "command:/meal"),
                ("Targets & setup", "setup"),
                ("Training", "training"),
                ("Supplements", "command:/supplement"),
                ("Settings", "settings"),
                ("Home", "home"),
            ),
        )
    if action == "setup":
        return menu(
            "Set up your diary\n1. Review foods and save your frequent meals.\n"
            "2. Set calorie/macro targets.\n3. Configure your timezone and "
            "optional reminders.\nEach target proposal needs your review before"
            " applying.",
            (
                ("Find foods", "log"),
                ("Set targets", "setupgoal"),
                ("Reminders & timezone", "settings"),
                ("Home", "home"),
            ),
        )
    if action == "setupgoal":
        from nutrition_bot.application.guided_goals import start_goal_setup

        return await start_goal_setup(connection, owner_id)
    if action == "favorites":
        rows = (
            await connection.execute(
                sa.select(favorites.c.id, favorites.c.name)
                .join(favorite_versions, favorite_versions.c.id == favorites.c.current_version_id)
                .where(favorite_versions.c.archived.is_(False))
                .order_by(favorites.c.id.desc())
                .limit(8)
            )
        ).all()
        return menu(
            "Choose a saved favorite to review and log."
            if rows
            else "No favorites yet. Open a recent meal and tap Save favorite.",
            tuple((str(name)[:40], f"command:/favorite F{fid}") for fid, name in rows)
            + (("Recent meals", "recent"), ("Home", "home")),
        )
    if action == "recent":
        ids: Sequence[int] = (
            (await connection.execute(sa.select(meals.c.id).order_by(meals.c.id.desc()).limit(8)))
            .scalars()
            .all()
        )
        snapshots = [await get_meal(connection, mid) for mid in ids]
        return menu(
            "Choose a meal to view, repeat or edit." if snapshots else "No meals logged yet.",
            tuple((f"{m.local_date} · {m.label}"[:45], f"command:/meal M{m.id}") for m in snapshots)
            + (("Log meal", "log"), ("Home", "home")),
        )
    if action == "settings":
        from nutrition_bot.application.settings_conversation import handle_settings_message

        result = await handle_settings_message(
            connection,
            "/settings",
            action_key=action_key,
            now=reference.timestamp(),
            default_timezone=str(reference.tzinfo),
        )
        assert result is not None
        return replace(result, ui_buttons=(("Setup guide", "setup"), ("Home", "home")))
    if action == "today":
        return await _command(connection, "/today short", message, **context)
    return home()


async def ui_message(
    connection: AsyncConnection,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
) -> MealReply | None:
    text = (message.text or "").strip()
    context: dict[str, Any] = dict(
        action_key=action_key,
        reference=reference,
        bot_id=bot_id,
        owner_id=owner_id,
        retention_days=retention_days,
    )
    if text in {"/help", "/home", "/menu", "/setup", "/cancel"}:
        return await ui_action(
            connection,
            {"/help": "help", "/setup": "setup", "/cancel": "cancel"}.get(text, "home"),
            message,
            **context,
        )
    if text.casefold() == "/goal setup":
        from nutrition_bot.application.guided_goals import start_goal_setup

        return await start_goal_setup(connection, owner_id)
    row = await _flow(connection, owner_id)
    if row is None or message.reply_to_message is not None:
        return None
    if text.startswith("/"):
        await cancel(connection, owner_id)
        return None
    if len(text) > 2000:
        return menu("Please send a shorter answer, or cancel this step.", (("Cancel", "cancel"),))
    stage, payload = row["stage"], row["payload"]
    if stage.startswith("goal_"):
        from nutrition_bot.application.guided_goals import goal_message

        return await goal_message(connection, row, text, owner_id=owner_id)
    if stage == "food_search":
        rows = (
            await connection.execute(
                sa.select(food_versions.c.id, food_versions.c.name)
                .where(
                    food_versions.c.sealed.is_(True),
                    sa.func.instr(sa.func.unicode_casefold(food_versions.c.name), text.casefold())
                    > 0,
                )
                .order_by(food_versions.c.id.desc())
                .limit(8)
            )
        ).all()
        if not rows:
            return menu(
                "No reviewed food matches that name. Try another name. You can "
                "import a reviewed food source before logging it.",
                (("Cancel", "cancel"),),
            )
        rev = await begin(
            connection,
            owner_id,
            "food_search",
            {"items": payload.get("items", []), "choices": [r.id for r in rows]},
        )
        return menu(
            "Choose the exact reviewed food/version:",
            tuple((f"{r.name} · #{r.id}"[:48], f"flow:{rev}:food={r.id}") for r in rows)
            + (("Cancel", "cancel"),),
        )
    if stage == "amount":
        match = re.fullmatch(
            r"(?:(about|around|roughly)\s+)?([0-9]+(?:\.[0-9]{1,3})?)\s*(g|kg|mg)?",
            text,
            re.IGNORECASE,
        )
        if not match:
            return menu(
                "Send a measured amount in grams, e.g. 150 g, or an explicit rough"
                " amount such as about 150 g.",
                (("Cancel", "cancel"),),
            )
        try:
            number = exact_decimal(match[2])
            unit = (match[3] or "g").casefold()
            with localcontext() as decimal_context:
                decimal_context.prec = 50
                grams = number * {"g": Decimal(1), "kg": Decimal(1000), "mg": Decimal(".001")}[unit]
            if not 0 < grams_to_milligrams(grams) <= 50_000_000:
                raise ValueError
        except ValueError:
            return menu(
                "Use a positive measured amount from 0.001 to 50000 grams, with whole "
                "milligrams. Rough portions still need approval.",
                (("Cancel", "cancel"),),
            )
        quantity = ("about " if match[1] else "") + format(number, "f") + unit
        items = payload.get("items", []) + [
            {"food_id": payload["food_id"], "name": payload["name"], "quantity": quantity}
        ]
        if len(items) > 10:
            return menu(
                "A meal can contain up to ten foods. Save this meal before adding more.",
                (("Cancel", "cancel"),),
            )
        rev = await begin(connection, owner_id, "meal_review", {"items": items})
        return menu(
            "Review this meal:\n"
            + "\n".join(f"• {i['quantity']} {_label(i['name'])} · #{i['food_id']}" for i in items)
            + "\nRough portions will open an approval draft.",
            _review_buttons(rev, len(items)),
        )
    commands = {
        "weight": "weight ",
        "recovery": "recovery ",
        "gym": "/gym ",
        "bjj": "/bjj ",
        "save_favorite": "/favorite save ",
        "edit_meal": f"/edit {payload.get('reference', '')} ",
    }
    if stage in commands:
        request = commands[stage] + text
        if stage == "save_favorite":
            if len(text) > 60 or any(c in text for c in "\n="):
                return menu("Choose a short favorite name without '='.", (("Cancel", "cancel"),))
            request += " = " + payload["reference"]
        result = await _command(connection, request, message, **context)
        if result.kind.endswith("rejected") or result.kind.endswith("help"):
            return replace(result, ui_buttons=(("Cancel", "cancel"),))
        await cancel(connection, owner_id)
        return result
    return None


async def meal_action(
    connection: AsyncConnection,
    action: str,
    meal_id: int,
    revision_id: int,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
) -> MealReply:
    from nutrition_bot.application.meal_conversation import handle_callback, receipt

    meal = await get_meal(connection, meal_id)
    if meal.revision_id != revision_id:
        return receipt(meal, "This receipt has changed (older receipt). Review the current meal:")
    ref = f"M{meal.id}r{meal.revision_number}"
    if action in {"save", "edit"}:
        await begin(
            connection,
            owner_id,
            "save_favorite" if action == "save" else "edit_meal",
            {"reference": ref},
        )
        prompt = menu(
            "What name should this favorite have?"
            if action == "save"
            else "What would you like to change? E.g. item 1: 120g, date yesterday,"
            " or replace: 120g rice.",
            (("Cancel", "cancel"),),
        )
        return (
            replace(prompt, meal_id=meal.id, revision_id=meal.revision_id)
            if action == "edit"
            else prompt
        )
    if action == "repeat":
        return await _command(
            connection,
            f"/repeat {ref}",
            message,
            action_key=action_key,
            reference=reference,
            bot_id=bot_id,
            owner_id=owner_id,
            retention_days=retention_days,
        )
    return await handle_callback(connection, action, meal_id, revision_id, action_key=action_key)
