import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
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
from nutrition_bot.application.meal_conversation import MealReply, handle_message
from nutrition_bot.config import BotSettings
from nutrition_bot.telegram.auth import authorized

if TYPE_CHECKING:
    from nutrition_bot.application.ai_service import AiService
    from nutrition_bot.domain.ai import AiOutcome
    from nutrition_bot.telegram.gateway import Gateway


@dataclass
class _NeedsAi(Exception):
    update_id: int
    message: Message
    reference: datetime


MAX_PENDING_UPDATES = 1000
MAX_PENDING_REPLIES = 1000


class QueueFullError(Exception):
    """Pause intake without acknowledging any part of the uncommitted batch."""


@dataclass
class _HeartbeatProgress:
    written_at: float
    state: str | None
    last_success_at: float | None


class Service:
    def __init__(self, store: Store, settings: BotSettings, ai_service: "AiService | None" = None):
        self.store = store
        self.settings = settings
        self.ai_service = ai_service
        self.gateway: Gateway | None = None
        self._heartbeat_lock = asyncio.Lock()
        self._heartbeat_progress: dict[str, _HeartbeatProgress] = {}

    async def offset(self) -> int:
        async with self.store.engine.connect() as connection:
            return int(await connection.scalar(sa.select(cursor.c.next_offset)) or 0)

    async def accept_internal(self, updates: list[Update]) -> None:
        if any(update.update_id >= 0 for update in updates):
            raise ValueError("Internal update IDs must be negative")
        await self.accept(updates, advance_cursor=False)

    async def accept(self, updates: list[Update], *, advance_cursor: bool = True) -> None:
        if not updates:
            return
        async with self.store.write() as connection:
            pending = int(
                await connection.scalar(
                    sa.select(sa.func.count()).select_from(inbox).where(inbox.c.status == "pending")
                )
                or 0
            )
            for update in sorted(updates, key=lambda value: value.update_id):
                if authorized(update, self.settings):
                    known = await connection.scalar(
                        sa.select(inbox.c.update_id).where(inbox.c.update_id == update.update_id)
                    )
                    if known is not None:
                        continue
                    if pending >= MAX_PENDING_UPDATES:
                        raise QueueFullError()
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
                    pending += 1
            if not advance_cursor:
                return
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
            "Reviewed training allocation uses /allocation. Open /home for buttons and "
            "/settings to configure optional reminders. Food suggestions are not yet available.\n"
            + (
                "AI is disabled; no paid calls are made."
                if not self.ai_service
                or not (
                    self.ai_service.enabled_for("meal_text")
                    or self.ai_service.enabled_for("meal_photo")
                )
                else "AI draft interpretation is enabled; saving still requires your review."
            )
        )

    async def process_one(self) -> bool:
        prepared: dict[int, AiOutcome] = {}
        while True:
            try:
                return await self._process_one(prepared)
            except _NeedsAi as request:
                assert self.ai_service is not None
                photo_started = time.monotonic()
                photo_created_at = time.time()
                photo = None
                if request.message.photo:
                    if self.gateway is None:
                        from nutrition_bot.domain.ai import AiOutcome

                        prepared[request.update_id] = AiOutcome(
                            status="unavailable",
                            request_key=str(request.update_id),
                            role="meal_photo",
                        )
                        await self._measure_failed_photo(
                            prepared[request.update_id], photo_created_at, photo_started
                        )
                        continue
                    try:
                        photo = await self.gateway.download_photo(request.message)
                    except Exception:
                        from nutrition_bot.domain.ai import AiOutcome

                        prepared[request.update_id] = AiOutcome(
                            status="unavailable",
                            request_key=str(request.update_id),
                            role="meal_photo",
                        )
                        await self._measure_failed_photo(
                            prepared[request.update_id], photo_created_at, photo_started
                        )
                        continue
                prepared[request.update_id] = await self.ai_service.interpret(
                    request_key=str(request.update_id),
                    text=request.message.text
                    or request.message.caption
                    or "Interpret this meal photo.",
                    local_date=request.reference.date(),
                    photo=photo,
                )

    async def _measure_failed_photo(
        self, outcome: "AiOutcome", created_at: float, started: float
    ) -> None:
        from nutrition_bot.domain.ai import AiCallMetrics

        assert self.ai_service is not None
        await self.ai_service._record_measurement(
            outcome,
            [AiCallMetrics(failure_category="download")],
            created_at=created_at,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def _process_one(self, prepared: dict[int, "AiOutcome"]) -> bool:
        processing_started = time.monotonic()
        # All processing is a local transaction. A crash leaves this row pending;
        # no in-progress inbox lease needs reclaiming and no network occurs here.
        async with self.store.write() as connection:
            queued = int(
                await connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(outbox)
                    .where(outbox.c.status.in_(["queued", "sending"]))
                )
                or 0
            )
            # A callback can produce both a message and a callback answer. Reserve both
            # before any action mutation, leaving the inbox untouched under backpressure.
            if queued > MAX_PENDING_REPLIES - 2:
                return False
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
                valid_ui = len(parts) == 3 and parts[0] == "ui" and parts[1].isdigit()
                valid_status = len(parts) == 2 and parts[0] == "status"
                valid_meal = (
                    len(parts) == 3
                    and parts[0] == "meal"
                    and parts[1] in {"edit", "repeat", "save", "delete", "undo"}
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
                    and parts[1] in {"complete", "incomplete", "add", "full", "short"}
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
                    or valid_ui
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
                if valid_ui:
                    allowed = [
                        item.get("callback_data")
                        for item in button_payload.get("buttons", [])
                        if isinstance(item, dict)
                    ]
                    requests = button_payload.get("ui_requests")
                    index = int(parts[1])
                    if (
                        callback.data not in allowed
                        or not isinstance(requests, list)
                        or index >= len(requests)
                        or not isinstance(requests[index], str)
                    ):
                        await self._finish(connection, update.update_id, "rejected", clear=True)
                        return True
                    callback_action = requests[index]
                elif (
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
                    "ui_callback"
                    if valid_ui
                    else "supplement_plan_callback"
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
                    .on_conflict_do_nothing(index_elements=[profile.c.id])
                )
                from nutrition_bot.application.navigation import home

                result = home()
                text, kind = result.text, "start"
            elif command == "/status":
                text, kind = await self._status(connection), "status"
            else:
                # Rejected user edits roll back any partially built ledger revision while
                # preserving the rejection action/response in the surrounding transaction.
                async with connection.begin_nested() as savepoint:
                    if command == "ui_callback":
                        from nutrition_bot.application.navigation import ui_action

                        timezone = await connection.scalar(
                            sa.select(profile.c.timezone).where(profile.c.id == 1)
                        )
                        result = await ui_action(
                            connection,
                            callback_action,
                            message,
                            action_key=key,
                            reference=datetime.fromtimestamp(
                                row["received_at"], ZoneInfo(timezone or self.settings.app_timezone)
                            ),
                            bot_id=self.settings.bot_id,
                            owner_id=self.settings.allowed_telegram_user_id,
                            retention_days=self.settings.raw_input_retention_days,
                        )
                    elif command == "supplement_plan_callback":
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
                        from nutrition_bot.application.navigation import meal_action

                        timezone = await connection.scalar(
                            sa.select(profile.c.timezone).where(profile.c.id == 1)
                        )
                        result = await meal_action(
                            connection,
                            callback_action,
                            button_payload["meal_id"],
                            button_payload["meal_revision_id"],
                            message,
                            action_key=key,
                            reference=datetime.fromtimestamp(
                                row["received_at"], ZoneInfo(timezone or self.settings.app_timezone)
                            ),
                            bot_id=self.settings.bot_id,
                            owner_id=self.settings.allowed_telegram_user_id,
                            retention_days=self.settings.raw_input_retention_days,
                        )
                    else:
                        timezone = await connection.scalar(
                            sa.select(profile.c.timezone).where(profile.c.id == 1)
                        )
                        reference = datetime.fromtimestamp(
                            row["received_at"], ZoneInfo(timezone or self.settings.app_timezone)
                        )
                        # A delayed Telegram update keeps the day on which the
                        # user sent it, just like deterministic meal parsing.
                        message_reference = message.date.astimezone(reference.tzinfo)
                        from nutrition_bot.application.navigation import ui_message
                        from nutrition_bot.application.settings_conversation import (
                            handle_settings_message,
                        )

                        result = None
                        if not update.edited_message:
                            result = await handle_settings_message(
                                connection,
                                message.text or "",
                                action_key=key,
                                now=row["received_at"],
                                default_timezone=self.settings.app_timezone,
                            )
                            if result is None:
                                result = await ui_message(
                                    connection,
                                    message,
                                    action_key=key,
                                    reference=reference,
                                    bot_id=self.settings.bot_id,
                                    owner_id=self.settings.allowed_telegram_user_id,
                                    retention_days=self.settings.raw_input_retention_days,
                                )
                        if result is None and row["update_id"] in prepared:
                            from nutrition_bot.application.ai_service import create_ai_draft

                            result = await create_ai_draft(
                                connection,
                                prepared[row["update_id"]],
                                message,
                                action_key=key,
                                reference=message_reference,
                            )
                        if result is None:
                            if (
                                message.photo
                                and self.ai_service
                                and self.ai_service.enabled_for("meal_photo")
                                and not update.edited_message
                                and not message.reply_to_message
                            ):
                                raise _NeedsAi(row["update_id"], message, message_reference)
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
                    if (
                        result.kind == "meal_rejected"
                        and self.ai_service
                        and self.ai_service.enabled_for("meal_text")
                        and row["update_id"] not in prepared
                        and not callback
                        and not update.edited_message
                        and not message.reply_to_message
                        and bool(message.text)
                        and not (message.text or "").lstrip().startswith("/")
                    ):
                        raise _NeedsAi(row["update_id"], message, message_reference)
                    if result.kind in {
                        "meal_rejected",
                        "allocation_rejected",
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
            if (
                not callback
                and not update.edited_message
                and not message.reply_to_message
                and row["update_id"] not in prepared
                and (message.photo or (message.text and not message.text.lstrip().startswith("/")))
            ):
                # Count only original standalone text/photo inputs. Commands,
                # callbacks and draft edits are outside this fallback denominator.
                from nutrition_bot.application.ai_metrics import record_metric

                await record_metric(
                    connection,
                    request_key=str(row["update_id"]),
                    role="meal_photo" if message.photo else "meal_text",
                    source="normal",
                    handling="local",
                    status="clarify" if kind == "meal_rejected" else "handled",
                    created_at=row["received_at"],
                    completed_at=time.time(),
                    elapsed_ms=int((time.monotonic() - processing_started) * 1000),
                    inference_sent=False,
                    attempts_sent=0,
                )
            if row["update_id"] in prepared:
                from nutrition_bot.application.ai_budget import consume_ai_outcome

                await consume_ai_outcome(connection, str(row["update_id"]))
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
                        result.ui_buttons
                        or result.meal_id
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
                    {
                        "text": {"save": "Save favorite", "repeat": "Repeat"}.get(
                            name, name.capitalize()
                        ),
                        "callback_data": f"meal:{name}:{reply_token}",
                    }
                    for name in result.buttons
                ]
            elif result and result.draft_id:
                payload.update(draft_id=result.draft_id, draft_revision=result.draft_revision)
                labels = {
                    "approve": "Approve draft" if result.review_required else "Approve estimate",
                    "edit": "Enter amount",
                    "cancel": "Cancel",
                }
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
                    "full": "Details",
                    "short": "Short version",
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
            if result and result.ui_buttons:
                payload["ui_requests"] = [request for _, request in result.ui_buttons]
                payload["buttons"] = [
                    {"text": label, "callback_data": f"ui:{i}:{reply_token}"}
                    for i, (label, _) in enumerate(result.ui_buttons)
                ]
            if result is not None and not result.buttons and not result.ui_buttons:
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
                from nutrition_bot.runtime.scheduler import guard_scheduled_reply

                if not await guard_scheduled_reply(connection, row, self.settings, now=time.time()):
                    return None
                row = (
                    (await connection.execute(sa.select(outbox).where(outbox.c.id == row["id"])))
                    .mappings()
                    .one()
                )
                await connection.execute(
                    sa.update(outbox)
                    .where(outbox.c.id == row["id"])
                    .values(status="sending", attempts=row["attempts"] + 1)
                )
            return row

    async def prepare_reply(self, row: RowMapping) -> RowMapping | None:
        from nutrition_bot.runtime.scheduler import guard_scheduled_reply

        async with self.store.write() as connection:
            current = (
                (await connection.execute(sa.select(outbox).where(outbox.c.id == row["id"])))
                .mappings()
                .one_or_none()
            )
            if (
                current is None
                or current["status"] != "sending"
                or not await guard_scheduled_reply(
                    connection, current, self.settings, now=time.time()
                )
            ):
                return None
            return (
                (await connection.execute(sa.select(outbox).where(outbox.c.id == row["id"])))
                .mappings()
                .one()
            )

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
            from nutrition_bot.adapters.database.schema_ui import ui_flows
            from nutrition_bot.application.ai_budget import purge_ai_outcomes

            await purge_ai_outcomes(connection, now=time.time())
            await connection.execute(
                sa.update(ui_flows)
                .where(ui_flows.c.updated_at < time.time() - 1800, ui_flows.c.stage != "idle")
                .values(stage="idle", payload={}, revision=ui_flows.c.revision + 1)
            )
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
            ids: list[int] = list(
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
