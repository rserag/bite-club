import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from aiogram.types import Message, Update
from pydantic import ValidationError
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.drafts import expire_drafts
from nutrition_bot.adapters.database.schema import (
    actions,
    cursor,
    food_source_cache,
    heartbeat,
    inbox,
    outbox,
    profile,
)
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.meal_conversation import MealReply, handle_callback, handle_message
from nutrition_bot.config import BotSettings
from nutrition_bot.telegram.auth import authorized


@dataclass
class _HeartbeatProgress:
    written_at: float
    state: str | None
    last_success_at: float | None


class Service:
    def __init__(self, store: Store, settings: BotSettings):
        self.store = store
        self.settings = settings
        self._heartbeat_lock = asyncio.Lock()
        self._heartbeat_progress: dict[str, _HeartbeatProgress] = {}

    async def offset(self) -> int:
        async with self.store.engine.connect() as connection:
            return int(await connection.scalar(sa.select(cursor.c.next_offset)) or 0)

    async def accept(self, updates: list[Update]) -> None:
        if not updates:
            return
        async with self.store.write() as connection:
            for update in sorted(updates, key=lambda value: value.update_id):
                if authorized(update, self.settings):
                    await connection.execute(
                        insert(inbox)
                        .values(
                            update_id=update.update_id,
                            payload=update.model_dump(mode="json", exclude_none=True),
                            status="pending",
                            received_at=time.time(),
                        )
                        .on_conflict_do_nothing(index_elements=["update_id"])
                    )
            next_offset = max(update.update_id for update in updates) + 1
            await connection.execute(
                insert(cursor)
                .values(id=1, next_offset=next_offset)
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={"next_offset": sa.func.max(cursor.c.next_offset, next_offset)},
                )
            )

    async def _status(self, connection: AsyncConnection) -> str:
        pending = await connection.scalar(
            sa.select(sa.func.count()).select_from(inbox).where(inbox.c.status == "pending")
        )
        queued = await connection.scalar(
            sa.select(sa.func.count())
            .select_from(outbox)
            .where(outbox.c.status.in_(["queued", "sending"]))
        )
        failed = await connection.scalar(
            sa.select(sa.func.count()).select_from(outbox).where(outbox.c.status == "failed")
        )
        pulse = (await connection.execute(sa.select(heartbeat))).mappings().all()
        degraded = [row["component"] for row in pulse if row["state"] == "degraded"]
        network = "Degraded: " + ", ".join(degraded) if degraded else "No recorded API interruption"
        return (
            "Nutrition diary is running.\n"
            f"Pending updates: {pending}; queued replies: {queued}; failed replies: {failed}.\n"
            f"{network}.\n"
            "Measured meals, explicit estimate approvals, corrections and /today totals "
            "are available; reviewed goals use /goal, body-weight trends use /weight, and "
            "weekly reports use /week; conservative calorie reviews use /adjust; "
            "quick gym/BJJ logs use /gym and /bjj, with optional session details; "
            "BJJ plans, recovery check-ins and /load workload guidance are available. "
            "Training nutrition and reminders are not yet.\n"
            "AI is disabled; no paid calls are made."
        )

    async def process_one(self) -> bool:
        # All processing is a local transaction. A crash leaves this row pending;
        # no in-progress inbox lease needs reclaiming and no network occurs here.
        async with self.store.write() as connection:
            # Check inactivity before callback validation, outside rejection savepoints.
            await expire_drafts(connection, now=time.time())
            row = (
                (
                    await connection.execute(
                        sa.select(inbox)
                        .where(inbox.c.status == "pending")
                        .order_by(inbox.c.update_id)
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            try:
                update = Update.model_validate(row["payload"])
            except ValidationError:
                await self._finish(connection, row["update_id"], "failed", clear=True)
                return True
            if not authorized(update, self.settings):
                await self._finish(connection, update.update_id, "rejected", clear=True)
                return True

            callback = update.callback_query
            button_payload: dict[str, Any] = {}
            callback_action = ""
            if callback:
                message = callback.message
                assert isinstance(message, Message)
                parts = (callback.data or "").split(":")
                valid_status = len(parts) == 2 and parts[0] == "status"
                valid_meal = (
                    len(parts) == 3
                    and parts[0] == "meal"
                    and parts[1] in {"edit", "delete", "undo"}
                )
                valid_draft = (
                    len(parts) == 3
                    and parts[0] == "draft"
                    and parts[1] in {"approve", "edit", "cancel"}
                )
                valid_favorite = (
                    len(parts) == 3
                    and parts[0] == "favorite"
                    and parts[1] in {"log", "archive", "restore"}
                )
                valid_recipe = (
                    len(parts) == 3
                    and parts[0] == "recipe"
                    and parts[1] in {"portion", "archive", "restore"}
                )
                valid_goal = (
                    len(parts) == 3 and parts[0] == "goal" and parts[1] in {"apply", "cancel"}
                )
                valid_weight = (
                    len(parts) == 3
                    and parts[0] == "weight"
                    and parts[1] in {"edit", "delete", "undo"}
                )
                valid_daily = (
                    len(parts) == 3
                    and parts[0] == "daily"
                    and parts[1] in {"complete", "incomplete", "add"}
                )
                valid_weekly = (
                    len(parts) == 3 and parts[0] == "weekly" and parts[1] in {"short", "full"}
                )
                valid_adaptive = (
                    len(parts) == 3
                    and parts[0] == "adaptive"
                    and parts[1] in {"apply", "keep", "review"}
                )
                valid_training = (
                    len(parts) == 3
                    and parts[0] == "training"
                    and parts[1] in {"edit", "delete", "undo"}
                )
                valid_recovery = (
                    len(parts) == 3
                    and parts[0] == "recovery"
                    and parts[1] in {"edit", "delete", "undo"}
                )
                valid_supplement_plan = (
                    len(parts) == 3
                    and parts[0] == "supplementplan"
                    and parts[1] in {"approve", "cancel"}
                )
                valid_supplement = (
                    len(parts) == 3
                    and parts[0] == "supplement"
                    and parts[1] in {"edit", "delete", "undo"}
                )
                if not (
                    valid_status
                    or valid_meal
                    or valid_draft
                    or valid_favorite
                    or valid_recipe
                    or valid_goal
                    or valid_weight
                    or valid_daily
                    or valid_weekly
                    or valid_adaptive
                    or valid_training
                    or valid_recovery
                    or valid_supplement
                    or valid_supplement_plan
                ):
                    await self._finish(connection, update.update_id, "rejected", clear=True)
                    return True
                token = parts[-1]
                button = (
                    (
                        await connection.execute(
                            sa.select(outbox).where(
                                outbox.c.button_token == token,
                                outbox.c.telegram_message_id == message.message_id,
                                outbox.c.chat_id == self.settings.allowed_telegram_chat_id,
                                outbox.c.owner_user_id == self.settings.allowed_telegram_user_id,
                                outbox.c.status == "sent",
                                outbox.c.sent_at
                                >= time.time() - self.settings.raw_input_retention_days * 86400,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if button is None or not isinstance(button["payload"], dict):
                    await self._finish(connection, update.update_id, "rejected", clear=True)
                    return True
                button_payload = button["payload"]
                if (
                    valid_meal
                    or valid_draft
                    or valid_favorite
                    or valid_recipe
                    or valid_goal
                    or valid_weight
                    or valid_daily
                    or valid_weekly
                    or valid_adaptive
                    or valid_training
                    or valid_recovery
                    or valid_supplement
                    or valid_supplement_plan
                ):
                    allowed = [
                        item.get("callback_data")
                        for item in button_payload.get("buttons", [])
                        if isinstance(item, dict)
                    ]
                    identity_key = (
                        "supplement_plan_proposal_id"
                        if valid_supplement_plan
                        else "supplement_intake_id"
                        if valid_supplement
                        else "training_session_id"
                        if valid_training
                        else "recovery_id"
                        if valid_recovery
                        else "adaptive_proposal_id"
                        if valid_adaptive
                        else "weekly_end"
                        if valid_weekly
                        else "daily_date"
                        if valid_daily
                        else "weight_id"
                        if valid_weight
                        else "goal_proposal_id"
                        if valid_goal
                        else "recipe_id"
                        if valid_recipe
                        else "favorite_id"
                        if valid_favorite
                        else "draft_id"
                        if valid_draft
                        else "meal_id"
                    )
                    revision_key = (
                        "supplement_intake_revision_id"
                        if valid_supplement
                        else "training_revision_id"
                        if valid_training
                        else "recovery_revision_id"
                        if valid_recovery
                        else "weight_revision_id"
                        if valid_weight
                        else "recipe_version_id"
                        if valid_recipe
                        else "favorite_version_id"
                        if valid_favorite
                        else "draft_revision"
                        if valid_draft
                        else "meal_revision_id"
                    )
                    if (
                        callback.data not in allowed
                        or (
                            type(button_payload.get(identity_key)) is not str
                            if valid_daily or valid_weekly
                            else type(button_payload.get(identity_key)) is not int
                        )
                        or (
                            not (
                                valid_goal
                                or valid_daily
                                or valid_weekly
                                or valid_adaptive
                                or valid_supplement_plan
                            )
                            and type(button_payload.get(revision_key)) is not int
                        )
                    ):
                        await self._finish(connection, update.update_id, "rejected", clear=True)
                        return True
                    callback_action = parts[1]
                elif any(
                    field in button_payload
                    for field in (
                        "meal_id",
                        "draft_id",
                        "favorite_id",
                        "recipe_id",
                        "goal_proposal_id",
                        "weight_id",
                        "daily_date",
                        "weekly_end",
                        "adaptive_proposal_id",
                        "training_session_id",
                        "recovery_id",
                        "supplement_intake_id",
                        "supplement_plan_proposal_id",
                    )
                ):
                    await self._finish(connection, update.update_id, "rejected", clear=True)
                    return True
                key = f"callback:{callback.id}"
                command = (
                    "supplement_plan_callback"
                    if valid_supplement_plan
                    else "supplement_callback"
                    if valid_supplement
                    else "training_callback"
                    if valid_training
                    else "recovery_callback"
                    if valid_recovery
                    else "adaptive_callback"
                    if valid_adaptive
                    else "weekly_callback"
                    if valid_weekly
                    else "daily_callback"
                    if valid_daily
                    else "weight_callback"
                    if valid_weight
                    else "goal_callback"
                    if valid_goal
                    else "recipe_callback"
                    if valid_recipe
                    else "favorite_callback"
                    if valid_favorite
                    else "draft_callback"
                    if valid_draft
                    else "meal_callback"
                    if valid_meal
                    else "/status"
                )
            else:
                key = f"update:{update.update_id}"
                message = update.message or update.edited_message
                assert message is not None
                parts = (message.text or "").split(maxsplit=1)
                command = (parts[0] if parts else "").split("@", 1)[0]
                if update.edited_message:
                    command = "edited"

            if await connection.scalar(sa.select(actions.c.key).where(actions.c.key == key)):
                await self._finish(connection, update.update_id, "done")
                return True
            # Actions exist before their ledger FK references; all commit with the receipt.
            await connection.execute(
                sa.insert(actions).values(
                    key=key, update_id=update.update_id, kind="pending", created_at=time.time()
                )
            )
            # Rejected edits still carry retained draft text; preserve their cleanup
            # association even when the nested mutation must roll back.
            from nutrition_bot.adapters.database.drafts import get_draft, track_draft_action
            from nutrition_bot.application.draft_conversation import track_message_reference

            if command == "draft_callback":
                draft_id = button_payload["draft_id"]
                if await get_draft(connection, draft_id) is not None:
                    await track_draft_action(connection, draft_id, key)
            elif not callback:
                await track_message_reference(
                    connection,
                    message,
                    action_key=key,
                    bot_id=self.settings.bot_id,
                    owner_id=self.settings.allowed_telegram_user_id,
                    retention_days=self.settings.raw_input_retention_days,
                    edited=update.edited_message is not None,
                )
            result: MealReply | None = None
            if command == "/start":
                await connection.execute(
                    insert(profile)
                    .values(id=1, timezone=self.settings.app_timezone, created_at=time.time())
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                text = (
                    "Your private nutrition diary is ready.\n"
                    "Use /foods to find a saved food, then log measured amounts: "
                    "'I ate 150g rice'.\n"
                    "Reply to meal receipts to correct, delete or undo them. /meal shows help; "
                    "/meals shows recent meals; /today shows daily totals "
                    "(/today short for a quick view); /status checks the connection.\n"
                    "Rough amounts create drafts: use /drafts to resume or approve an estimate. "
                    "Save meals with 'save as usual breakfast' as a receipt reply; "
                    "use /favorites, /eat and /aliases for shortcuts, or /recipe for batches. "
                    "Use /goal setup for a reviewed calorie and macro starting plan. "
                    "Send 'weight 79.4' or use /weight for trend coverage. "
                    "Use /week or /week short for the last seven calendar dates. "
                    "Use /adjust for a conservative evidence-based calorie review. "
                    "Quick gym/BJJ logs use /gym and /bjj; optional sets use /gym details. "
                    "Optional BJJ details, plans and recovery check-ins are available; /load "
                    "shows conservative workload guidance. Log an exact creatine dose such as "
                    "'creatine 5 g'; /supplement shows supplement help. Reminders and AI are not "
                    "available yet."
                )
                kind = "start"
            elif command == "/status":
                text, kind = await self._status(connection), "status"
            else:
                # Rejected user edits roll back any partially built ledger revision while
                # preserving the rejection action/response in the surrounding transaction.
                async with connection.begin_nested() as savepoint:
                    if command == "supplement_plan_callback":
                        from nutrition_bot.application.supplement_plan_conversation import (
                            handle_plan_callback,
                        )

                        timezone = await connection.scalar(
                            sa.select(profile.c.timezone).where(profile.c.id == 1)
                        )
                        result = await handle_plan_callback(
                            connection,
                            callback_action,
                            button_payload["supplement_plan_proposal_id"],
                            action_key=key,
                            reference=datetime.fromtimestamp(
                                row["received_at"], ZoneInfo(timezone or self.settings.app_timezone)
                            ),
                        )
                    elif command == "supplement_callback":
                        from nutrition_bot.application.supplement_conversation import (
                            handle_supplement_callback,
                        )

                        result = await handle_supplement_callback(
                            connection,
                            callback_action,
                            button_payload["supplement_intake_id"],
                            button_payload["supplement_intake_revision_id"],
                            action_key=key,
                        )
                    elif command == "recovery_callback":
                        from nutrition_bot.application.recovery_conversation import (
                            handle_recovery_callback,
                        )

                        result = await handle_recovery_callback(
                            connection,
                            callback_action,
                            button_payload["recovery_id"],
                            button_payload["recovery_revision_id"],
                            action_key=key,
                        )
                    elif command == "training_callback":
                        from nutrition_bot.application.training_conversation import (
                            handle_training_callback,
                        )

                        result = await handle_training_callback(
                            connection,
                            callback_action,
                            button_payload["training_session_id"],
                            button_payload["training_revision_id"],
                            action_key=key,
                        )
                    elif command == "adaptive_callback":
                        from nutrition_bot.application.adaptive_conversation import (
                            handle_adaptive_callback,
                        )

                        timezone = await connection.scalar(
                            sa.select(profile.c.timezone).where(profile.c.id == 1)
                        )
                        result = await handle_adaptive_callback(
                            connection,
                            callback_action,
                            button_payload["adaptive_proposal_id"],
                            action_key=key,
                            today=datetime.fromtimestamp(
                                row["received_at"], ZoneInfo(timezone or self.settings.app_timezone)
                            ).date(),
                        )
                    elif command == "weekly_callback":
                        from nutrition_bot.application.weekly_conversation import (
                            handle_weekly_callback,
                        )

                        result = await handle_weekly_callback(
                            connection,
                            callback_action,
                            button_payload["weekly_end"],
                        )
                    elif command == "daily_callback":
                        from nutrition_bot.application.daily_conversation import (
                            handle_daily_callback,
                        )

                        timezone = await connection.scalar(
                            sa.select(profile.c.timezone).where(profile.c.id == 1)
                        )
                        result = await handle_daily_callback(
                            connection,
                            callback_action,
                            button_payload["daily_date"],
                            action_key=key,
                            timezone=timezone or self.settings.app_timezone,
                        )
                    elif command == "weight_callback":
                        from nutrition_bot.application.weight_conversation import (
                            handle_weight_callback,
                        )

                        result = await handle_weight_callback(
                            connection,
                            callback_action,
                            button_payload["weight_id"],
                            button_payload["weight_revision_id"],
                            action_key=key,
                        )
                    elif command == "recipe_callback":
                        from nutrition_bot.application.recipe_conversation import (
                            handle_recipe_callback,
                        )

                        result = await handle_recipe_callback(
                            connection,
                            callback_action,
                            button_payload["recipe_id"],
                            button_payload["recipe_version_id"],
                            action_key=key,
                        )
                    elif command == "goal_callback":
                        from nutrition_bot.application.goal_conversation import (
                            handle_goal_callback,
                        )

                        timezone = await connection.scalar(
                            sa.select(profile.c.timezone).where(profile.c.id == 1)
                        )
                        result = await handle_goal_callback(
                            connection,
                            callback_action,
                            button_payload["goal_proposal_id"],
                            action_key=key,
                            today=datetime.fromtimestamp(
                                row["received_at"], ZoneInfo(timezone or self.settings.app_timezone)
                            ).date(),
                        )
                    elif command == "favorite_callback":
                        from nutrition_bot.application.reuse_conversation import (
                            handle_favorite_callback,
                        )

                        timezone = await connection.scalar(
                            sa.select(profile.c.timezone).where(profile.c.id == 1)
                        )
                        result = await handle_favorite_callback(
                            connection,
                            callback_action,
                            button_payload["favorite_id"],
                            button_payload["favorite_version_id"],
                            message=message,
                            reference=datetime.fromtimestamp(
                                row["received_at"], ZoneInfo(timezone or self.settings.app_timezone)
                            ),
                            action_key=key,
                        )
                    elif command == "draft_callback":
                        from nutrition_bot.application.draft_conversation import (
                            handle_draft_callback,
                        )

                        result = await handle_draft_callback(
                            connection,
                            callback_action,
                            button_payload["draft_id"],
                            button_payload["draft_revision"],
                            action_key=key,
                        )
                    elif command == "meal_callback":
                        result = await handle_callback(
                            connection,
                            callback_action,
                            button_payload["meal_id"],
                            button_payload["meal_revision_id"],
                            action_key=key,
                        )
                    else:
                        timezone = await connection.scalar(
                            sa.select(profile.c.timezone).where(profile.c.id == 1)
                        )
                        result = await handle_message(
                            connection,
                            message,
                            action_key=key,
                            timezone=timezone or self.settings.app_timezone,
                            bot_id=self.settings.bot_id,
                            owner_id=self.settings.allowed_telegram_user_id,
                            edited=update.edited_message is not None,
                            retention_days=self.settings.raw_input_retention_days,
                        )
                    if result.kind in {
                        "meal_rejected",
                        "weight_rejected",
                        "goal_rejected",
                        "daily_rejected",
                        "adaptive_rejected",
                        "training_rejected",
                        "recovery_rejected",
                        "supplement_rejected",
                    }:
                        await savepoint.rollback()
                text, kind = result.text, result.kind
            # Newly associated expired references may contain quoted raw copies.
            # Purge before inserting this response so an expiry explanation can be sent.
            await expire_drafts(connection, now=time.time())
            await connection.execute(
                sa.update(actions).where(actions.c.key == key).values(kind=kind)
            )
            payload: dict[str, Any] = {"text": text}
            reply_token = (
                uuid.uuid4().hex
                if kind in {"start", "status"}
                or (
                    result
                    and (
                        result.meal_id
                        or result.draft_id
                        or result.favorite_id
                        or result.recipe_id
                        or result.goal_proposal_id
                        or result.weight_id
                        or result.daily_date
                        or result.weekly_end
                        or result.adaptive_proposal_id
                        or result.training_session_id
                        or result.recovery_id
                        or result.supplement_plan_proposal_id
                        or result.supplement_intake_id
                    )
                )
                else None
            )
            if result and result.meal_id:
                payload.update(meal_id=result.meal_id, meal_revision_id=result.revision_id)
                payload["buttons"] = [
                    {"text": name.capitalize(), "callback_data": f"meal:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.draft_id:
                payload.update(draft_id=result.draft_id, draft_revision=result.draft_revision)
                labels = {"approve": "Approve estimate", "edit": "Enter amount", "cancel": "Cancel"}
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"draft:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.favorite_id:
                payload.update(
                    favorite_id=result.favorite_id, favorite_version_id=result.favorite_version_id
                )
                labels = {"log": "Log this", "archive": "Archive", "restore": "Restore"}
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"favorite:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.recipe_id:
                payload.update(
                    recipe_id=result.recipe_id, recipe_version_id=result.recipe_version_id
                )
                labels = {"portion": "Enter portion", "archive": "Archive", "restore": "Restore"}
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"recipe:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.goal_proposal_id:
                payload.update(goal_proposal_id=result.goal_proposal_id)
                labels = {"apply": "Apply tomorrow", "cancel": "Cancel"}
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"goal:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.weight_id:
                payload.update(
                    weight_id=result.weight_id,
                    weight_revision_id=result.weight_revision_id,
                )
                labels = {"edit": "Edit", "delete": "Delete", "undo": "Undo"}
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"weight:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.daily_date:
                payload.update(daily_date=result.daily_date)
                labels = {
                    "complete": "All food logged",
                    "incomplete": "Incomplete",
                    "add": "Add something",
                }
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"daily:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.weekly_end:
                payload.update(weekly_end=result.weekly_end)
                labels = {"short": "Short version", "full": "Full report"}
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"weekly:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.adaptive_proposal_id:
                payload.update(adaptive_proposal_id=result.adaptive_proposal_id)
                labels = {
                    "apply": "Apply tomorrow",
                    "keep": "Keep target",
                    "review": "Review evidence",
                }
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"adaptive:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.training_session_id:
                payload.update(
                    training_session_id=result.training_session_id,
                    training_revision_id=result.training_revision_id,
                )
                labels = {"edit": "Edit", "delete": "Delete", "undo": "Undo"}
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"training:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.recovery_id:
                payload.update(
                    recovery_id=result.recovery_id,
                    recovery_revision_id=result.recovery_revision_id,
                )
                labels = {"edit": "Edit", "delete": "Delete", "undo": "Undo"}
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"recovery:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.supplement_plan_proposal_id:
                payload.update(supplement_plan_proposal_id=result.supplement_plan_proposal_id)
                labels = {"approve": "Approve plan — no listed concerns", "cancel": "Cancel"}
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"supplementplan:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            elif result and result.supplement_intake_id:
                payload.update(
                    supplement_intake_id=result.supplement_intake_id,
                    supplement_intake_revision_id=result.supplement_intake_revision_id,
                )
                labels = {"edit": "Edit", "delete": "Delete", "undo": "Undo"}
                payload["buttons"] = [
                    {"text": labels[name], "callback_data": f"supplement:{name}:{reply_token}"}
                    for name in result.buttons
                ]
            if result is not None and not result.buttons:
                payload.pop("buttons", None)
                reply_token = None
            await self._reply(connection, key, "message", payload, reply_token)
            if callback:
                await self._reply(
                    connection,
                    key,
                    "callback_answer",
                    {
                        "callback_id": callback.id,
                        "text": "Status requested" if command == "/status" else "Request processed",
                    },
                )
            await self._finish(connection, update.update_id, "done")
        return True

    @staticmethod
    async def _finish(
        connection: AsyncConnection, update_id: int, status: str, *, clear: bool = False
    ) -> None:
        values: dict[str, Any] = {"status": status, "processed_at": time.time()}
        if clear:
            values["payload"] = None
        await connection.execute(
            sa.update(inbox).where(inbox.c.update_id == update_id).values(values)
        )

    async def _reply(
        self,
        connection: AsyncConnection,
        key: str,
        kind: str,
        payload: dict[str, Any],
        token: str | None = None,
    ) -> None:
        await connection.execute(
            sa.insert(outbox).values(
                action_key=key,
                kind=kind,
                chat_id=self.settings.allowed_telegram_chat_id,
                owner_user_id=self.settings.allowed_telegram_user_id,
                payload=payload,
                status="queued",
                next_attempt_at=time.time(),
                created_at=time.time(),
                button_token=token,
            )
        )

    async def pulse(
        self, component: str, state: str | None = None, *, success: bool = False
    ) -> None:
        # Idle loops run several times per second; their liveness needs only a
        # periodic commit. State transitions and the first success remain immediate.
        async with self._heartbeat_lock:
            now, monotonic_now = time.time(), time.monotonic()
            previous = self._heartbeat_progress.get(component)
            next_state: str | None
            if state is not None or success:
                next_state = state or "healthy"
            else:
                next_state = previous.state if previous else None
            last_success_at = now if success else previous.last_success_at if previous else None
            if (
                previous is not None
                and monotonic_now - previous.written_at < 5
                and next_state == previous.state
                and not (success and previous.last_success_at is None)
            ):
                # Carry a throttled success into the next ordinary heartbeat;
                # never substitute that later heartbeat's time for the event time.
                previous.last_success_at = last_success_at
                return
            values: dict[str, Any] = {"touched_at": now}
            if next_state is not None:
                values["state"] = next_state
            if last_success_at is not None:
                values["last_success_at"] = last_success_at
            async with self.store.write() as connection:
                await connection.execute(
                    insert(heartbeat)
                    .values(component=component, **({"state": "healthy"} | values))
                    .on_conflict_do_update(index_elements=["component"], set_=values)
                )
            self._heartbeat_progress[component] = _HeartbeatProgress(
                monotonic_now, next_state, last_success_at
            )

    async def recover_outbox(self) -> None:
        # Caller holds the process-wide file lock; no other sender can still own these rows.
        async with self.store.write() as connection:
            await connection.execute(
                sa.update(outbox)
                .where(outbox.c.status == "sending")
                .values(status="queued", next_attempt_at=time.time(), error_type="InterruptedSend")
            )

    async def claim_reply(self) -> RowMapping | None:
        async with self.store.write() as connection:
            await expire_drafts(connection, now=time.time())
            row = (
                (
                    await connection.execute(
                        sa.select(outbox)
                        .where(
                            outbox.c.status == "queued",
                            outbox.c.next_attempt_at <= time.time(),
                        )
                        .order_by(
                            sa.case((outbox.c.kind == "callback_answer", 0), else_=1), outbox.c.id
                        )
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if row:
                await connection.execute(
                    sa.update(outbox)
                    .where(outbox.c.id == row["id"])
                    .values(status="sending", attempts=row["attempts"] + 1)
                )
            return row

    async def finish_reply(
        self,
        reply_id: int,
        status: str,
        *,
        message_id: int | None = None,
        delay: float = 0,
        error_type: str | None = None,
    ) -> None:
        async with self.store.write() as connection:
            await connection.execute(
                sa.update(outbox)
                .where(outbox.c.id == reply_id)
                .values(
                    status=status,
                    telegram_message_id=message_id,
                    sent_at=time.time() if status == "sent" else None,
                    next_attempt_at=time.time() + delay,
                    error_type=error_type,
                )
            )

    async def cleanup(self) -> None:
        cutoff = time.time() - self.settings.raw_input_retention_days * 86400
        async with self.store.write() as connection:
            await expire_drafts(connection, now=time.time())
            await connection.execute(
                sa.delete(food_source_cache).where(food_source_cache.c.expires_at <= time.time())
            )
            await connection.execute(
                sa.update(inbox)
                .where(
                    inbox.c.status != "pending",
                    inbox.c.processed_at < cutoff,
                )
                .values(payload=None)
            )
            await connection.execute(
                sa.update(outbox)
                .where(
                    outbox.c.status.in_(["sent", "failed"]),
                    outbox.c.created_at < cutoff,
                )
                .values(payload=None, button_token=None)
            )

    async def retry_failed_replies(self) -> int:
        """Operator-requested recovery; never revive expired content or old ownership."""
        async with self.store.write() as connection:
            ids = list(
                (
                    await connection.execute(
                        sa.select(outbox.c.id).where(
                            outbox.c.status == "failed",
                            outbox.c.chat_id == self.settings.allowed_telegram_chat_id,
                            outbox.c.owner_user_id == self.settings.allowed_telegram_user_id,
                            outbox.c.created_at
                            >= time.time() - self.settings.raw_input_retention_days * 86400,
                            outbox.c.kind == "message",
                        )
                    )
                ).scalars()
            )
            if ids:
                await connection.execute(
                    sa.update(outbox)
                    .where(outbox.c.id.in_(ids))
                    .values(
                        status="queued",
                        attempts=0,
                        next_attempt_at=time.time(),
                        error_type=None,
                    )
                )
            return len(ids)
