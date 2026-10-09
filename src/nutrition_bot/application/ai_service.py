"""Optional meal interpretation outside write transactions and exact reviewed drafts."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import cast

import sqlalchemy as sa
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.ai.chatgpt_plan import ChatGPTPlanAdapter
from nutrition_bot.adapters.ai.openrouter import (
    AiTransportError,
    OpenRouterAdapter,
)
from nutrition_bot.adapters.database.drafts import create_draft
from nutrition_bot.adapters.database.foods import get_food_version
from nutrition_bot.adapters.database.schema_ai import (
    ai_attempts,
    ai_disabled_routes,
    ai_plan_invocations,
    ai_requests,
)
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.ai_budget import (
    consume_ai_outcome,
    purge_ai_outcomes,
    reserve_attempt,
    save_outcome,
    settle_attempt,
)
from nutrition_bot.application.draft_conversation import draft_receipt
from nutrition_bot.application.food_retrieval import ai_food_context
from nutrition_bot.application.meal_conversation import MealReply, _date_timestamp
from nutrition_bot.domain.ai import AiCallMetrics, AiCatalogItem, AiOutcome, AiRole, AiUnavailable
from nutrition_bot.domain.ai_policy import AiEndpointManifest
from nutrition_bot.domain.drafts import DraftContent, PlannedItem
from nutrition_bot.domain.food import exact_decimal, grams_to_milligrams

logger = logging.getLogger("nutrition_bot.ai_metrics")


@dataclass
class _InterpretationMetrics:
    calls: list[AiCallMetrics] = field(default_factory=list)
    replayed: bool = False


class AiService:
    def __init__(
        self,
        store: Store,
        manifest: AiEndpointManifest | None = None,
        adapter: OpenRouterAdapter | None = None,
        *,
        evaluation_prefix: str | None = None,
        plan_adapter: ChatGPTPlanAdapter | None = None,
        evaluation_catalog: tuple[AiCatalogItem, ...] | None = None,
    ):
        if evaluation_prefix is not None and not (
            evaluation_prefix.startswith("eval:")
            and 8 <= len(evaluation_prefix) <= 80
            and evaluation_prefix.endswith(":")
        ):
            raise ValueError("Use a bounded evaluation run prefix.")
        self.store = store
        self.manifest = manifest
        self.adapter = adapter
        self.evaluation_prefix = evaluation_prefix
        if evaluation_catalog is not None and evaluation_prefix is None:
            raise ValueError("Synthetic evaluation catalogs require an evaluation run scope.")
        self.plan_adapter = plan_adapter
        self.evaluation_catalog = evaluation_catalog
        self._disabled_route_ids: set[str] = set()
        self._lock = asyncio.Lock()

    def enabled_for(self, role: AiRole) -> bool:
        if self.plan_adapter is not None:
            return self.plan_adapter.enabled_for(role)
        if self.adapter is None or self.manifest is None:
            return False
        try:
            # Endpoint policy dates use UTC; spending month boundaries are frozen locally.
            from datetime import UTC

            route = self.manifest.route(role, datetime.now(UTC).date())
            if route.id in self._disabled_route_ids:
                return False
        except AiUnavailable:
            return False
        return True

    async def close(self) -> None:
        if self.plan_adapter is not None:
            await self.plan_adapter.close()
        if self.adapter is not None:
            await self.adapter.close()

    async def _catalog(self, text: str = "") -> tuple[AiCatalogItem, ...]:
        if self.evaluation_catalog is not None:
            return self.evaluation_catalog
        async with self.store.engine.connect() as connection:
            return await ai_food_context(connection, text)

    async def interpret(
        self,
        *,
        request_key: str,
        text: str,
        local_date: date,
        photo: bytes | None = None,
    ) -> AiOutcome:
        created_at = time.time()
        started = time.monotonic()
        measurement = _InterpretationMetrics()
        calls = measurement.calls
        try:
            outcome = await self._interpret(
                request_key=request_key,
                text=text,
                local_date=local_date,
                photo=photo,
                measurement=measurement,
            )
        except asyncio.CancelledError:

            async def abandon() -> None:
                await self.recover_abandoned(request_key=request_key)
                if calls:
                    calls[-1].failure_category = "interrupted"
                await self._record_measurement(
                    AiOutcome(
                        request_key=request_key,
                        role="meal_photo" if photo is not None else "meal_text",
                        status="unknown",
                    ),
                    calls,
                    created_at=created_at,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            cleanup = asyncio.create_task(abandon())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise
        if not measurement.replayed:
            await self._record_measurement(
                outcome,
                calls,
                created_at=created_at,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        return outcome

    async def _record_measurement(
        self, outcome: AiOutcome, calls: list[AiCallMetrics], *, created_at: float, elapsed_ms: int
    ) -> None:
        """Optional measurements cannot turn a successful interpretation into a failure."""
        from nutrition_bot.application.ai_metrics import record_metric

        sent = [call for call in calls if call.inference_sent]

        def stage(field: str) -> int | None:
            values = [cast(int | None, getattr(call, field)) for call in calls]
            known = [value for value in values if value is not None]
            return sum(known) if known else None

        def usage(field: str) -> int | None:
            values = [cast(int | None, getattr(call, field)) for call in sent]
            if not values or any(value is None for value in values):
                return None
            return sum(value for value in values if value is not None)

        latest = calls[-1] if calls else AiCallMetrics()
        failure = latest.failure_category
        if outcome.status == "unknown" and failure is None:
            failure = "interrupted"
        try:
            async with self.store.write() as connection:
                await record_metric(
                    connection,
                    request_key=outcome.request_key,
                    role=outcome.role,
                    source="evaluation" if self.evaluation_prefix else "normal",
                    handling="ai",
                    status=outcome.status,
                    created_at=created_at,
                    completed_at=time.time(),
                    elapsed_ms=elapsed_ms,
                    model=latest.model,
                    prompt_version=latest.prompt_version,
                    schema_version=latest.schema_version,
                    reasoning_effort=latest.reasoning_effort,
                    auth_ms=stage("auth_ms"),
                    model_catalog_ms=stage("model_catalog_ms"),
                    inference_ms=stage("inference_ms"),
                    input_tokens=usage("input_tokens"),
                    output_tokens=usage("output_tokens"),
                    reasoning_tokens=usage("reasoning_tokens"),
                    cached_tokens=usage("cached_tokens"),
                    charged_micro_usd=usage("charged_micro_usd"),
                    inference_sent=bool(sent),
                    attempts_sent=len(sent),
                    failure_category=failure,
                )
        except Exception:
            logger.warning("ai_measurement_unavailable")

    async def recover_abandoned(self, *, request_key: str | None = None) -> None:
        """Run once before a sole worker starts; preserve uncertain cost and never replay."""
        async with self.store.write() as connection:
            self._disabled_route_ids.update(
                (await connection.scalars(sa.select(ai_disabled_routes.c.route_id))).all()
            )
            matching = ai_requests.c.state == "running"
            if request_key is not None:
                matching = sa.and_(matching, ai_requests.c.request_key == request_key)
            keys: tuple[str, ...] = tuple(
                (
                    await connection.scalars(sa.select(ai_requests.c.request_key).where(matching))
                ).all()
            )
            if not keys:
                return
            now = time.time()
            await connection.execute(
                sa.update(ai_attempts)
                .where(ai_attempts.c.request_key.in_(keys), ai_attempts.c.state == "reserved")
                .values(state="unknown", completed_at=now)
            )
            await connection.execute(
                sa.update(ai_plan_invocations)
                .where(
                    ai_plan_invocations.c.request_key.in_(keys),
                    ai_plan_invocations.c.state == "started",
                )
                .values(state="unknown", completed_at=now)
            )
            await connection.execute(
                sa.update(ai_requests)
                .where(ai_requests.c.request_key.in_(keys))
                .values(state="unknown", completed_at=now)
            )

    async def _interpret(
        self,
        *,
        request_key: str,
        text: str,
        local_date: date,
        photo: bytes | None = None,
        measurement: _InterpretationMetrics | None = None,
    ) -> AiOutcome:
        role: AiRole = "meal_photo" if photo is not None else "meal_text"
        if (
            not 1 <= len(request_key) <= 128
            or self.evaluation_prefix is not None
            and not request_key.startswith(self.evaluation_prefix)
            or self.evaluation_prefix is None
            and request_key.startswith("eval:")
        ):
            raise ValueError("The AI request key is invalid.")
        if not self.enabled_for(role):
            return AiOutcome(request_key=request_key, role=role, status="disabled")
        if self.plan_adapter is not None:
            return await self._interpret_plan(
                request_key=request_key,
                text=text,
                local_date=local_date,
                photo=photo,
                role=role,
                measurement=measurement,
            )
        assert self.manifest is not None and self.adapter is not None
        # One paid request at a time. Persistent claims also prevent a second
        # worker/CLI from replaying a sent attempt after a process interruption.
        async with self._lock:
            async with self.store.write() as connection:
                await purge_ai_outcomes(connection)
                existing = (
                    (
                        await connection.execute(
                            sa.select(ai_requests).where(ai_requests.c.request_key == request_key)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    if measurement is not None:
                        measurement.replayed = True
                    if existing["outcome"] is not None:
                        return AiOutcome.model_validate(existing["outcome"])
                    # Never restart a claimed request whose result was lost or consumed.
                    outcome = AiOutcome(request_key=request_key, role=role, status="unknown")
                    if existing["state"] == "running":
                        await connection.execute(
                            sa.update(ai_requests)
                            .where(ai_requests.c.request_key == request_key)
                            .values(state="unknown", completed_at=time.time())
                        )
                    return outcome
                # Global lock across process instances: a reserved attempt cannot
                # start another paid request. Crash-left reservations require reconciliation.
                if await connection.scalar(
                    sa.select(ai_attempts.c.id).where(ai_attempts.c.state == "reserved").limit(1)
                ) or await connection.scalar(
                    sa.select(ai_requests.c.request_key)
                    .where(ai_requests.c.state == "running")
                    .limit(1)
                ):
                    return AiOutcome(request_key=request_key, role=role, status="unknown")
                await connection.execute(
                    sa.insert(ai_requests).values(
                        request_key=request_key,
                        role=role,
                        state="running",
                        outcome=None,
                        created_at=time.time(),
                    )
                )
            from datetime import UTC

            route = self.manifest.route(role, datetime.now(UTC).date())
            catalog = await self._catalog(text)
            if not catalog:
                return await self._finish(request_key, role, "clarify")
            try:
                body = self.adapter.request_body(
                    route, text=text, catalog=catalog, local_date=local_date, photo=photo
                )
            except AiUnavailable:
                return await self._finish(request_key, role, "unavailable")
            for attempt in (1, 2):
                async with self.store.write() as connection:
                    attempt_id = await reserve_attempt(
                        connection,
                        request_key=request_key,
                        attempt=attempt,
                        route=route,
                        now=time.time(),
                        evaluation_prefix=self.evaluation_prefix,
                    )
                if attempt_id is None:
                    return await self._finish(request_key, role, "budget")
                try:
                    metrics = AiCallMetrics()
                    if measurement is not None:
                        measurement.calls.append(metrics)
                    # Every attempt is bounded, even injected transport clients.
                    async with asyncio.timeout(30):
                        completion = await self.adapter.complete(route, body, metrics=metrics)
                except (AiTransportError, TimeoutError):
                    if metrics.failure_category is None:
                        metrics.failure_category = "timeout"
                    async with self.store.write() as connection:
                        await settle_attempt(
                            connection,
                            attempt_id,
                            charge=None,
                            generation_id=None,
                            route_id=route.id,
                            now=time.time(),
                        )
                    return await self._finish(request_key, role, "unknown")
                # Cancellation deliberately leaves its durable reservation intact.
                async with self.store.write() as connection:
                    await settle_attempt(
                        connection,
                        attempt_id,
                        charge=completion.charged_micro_usd,
                        generation_id=completion.generation_id,
                        fault=completion.fault,
                        route_id=route.id,
                        now=time.time(),
                    )
                if completion.charged_micro_usd is None:
                    return await self._finish(request_key, role, "unknown")
                if completion.fault in {"policy", "usage", "pricing"}:
                    self._disabled_route_ids.add(route.id)
                    return await self._finish(request_key, role, "unavailable")
                if completion.proposal is not None:
                    known = {food.food_version_id for food in catalog}
                    proposal = completion.proposal
                    if proposal.local_date > local_date or any(
                        item.food_version_id not in known for item in proposal.items
                    ):
                        metrics.failure_category = "schema"
                        return await self._finish(request_key, role, "unavailable")
                    outcome = AiOutcome(
                        request_key=request_key,
                        role=role,
                        proposal=proposal,
                        status="ready" if proposal.intent == "meal" else "clarify",
                    )
                    async with self.store.write() as connection:
                        await save_outcome(connection, outcome, time.time())
                    return outcome
                # A schema-only error with returned billing can use one fully
                # reserved retry. Clarification and unknown billing never retry.
            return await self._finish(request_key, role, "unavailable")
        raise AssertionError("The bounded AI attempt loop must return.")

    async def _interpret_plan(
        self,
        *,
        request_key: str,
        text: str,
        local_date: date,
        photo: bytes | None,
        role: AiRole,
        measurement: _InterpretationMetrics | None = None,
    ) -> AiOutcome:
        from datetime import UTC

        assert self.plan_adapter is not None
        async with self._lock:
            async with self.store.write() as connection:
                await purge_ai_outcomes(connection)
                existing = (
                    (
                        await connection.execute(
                            sa.select(ai_requests).where(ai_requests.c.request_key == request_key)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    if measurement is not None:
                        measurement.replayed = True
                    if existing["outcome"] is not None:
                        return AiOutcome.model_validate(existing["outcome"])
                    return AiOutcome(request_key=request_key, role=role, status="unknown")
                if await connection.scalar(
                    sa.select(ai_requests.c.request_key)
                    .where(ai_requests.c.state == "running")
                    .limit(1)
                ):
                    return AiOutcome(request_key=request_key, role=role, status="unknown")
                day = datetime.now(UTC).date().isoformat()
                evaluation_pool = self.evaluation_prefix is not None
                is_evaluation = sa.func.substr(ai_plan_invocations.c.request_key, 1, 5) == "eval:"
                count = int(
                    await connection.scalar(
                        sa.select(sa.func.count())
                        .select_from(ai_plan_invocations)
                        .where(
                            ai_plan_invocations.c.day == day,
                            is_evaluation if evaluation_pool else ~is_evaluation,
                        )
                    )
                    or 0
                )
                limit = (
                    self.plan_adapter.policy.daily_evaluation_invocation_limit
                    if evaluation_pool
                    else self.plan_adapter.policy.daily_invocation_limit
                )
                if count >= limit:
                    return AiOutcome(request_key=request_key, role=role, status="quota")
                await connection.execute(
                    sa.insert(ai_requests).values(
                        request_key=request_key, role=role, state="running", created_at=time.time()
                    )
                )
            catalog = await self._catalog(text)
            if not catalog:
                return await self._finish(request_key, role, "unavailable")
            try:
                body = self.plan_adapter.request_body(
                    role=role, text=text, catalog=catalog, local_date=local_date, photo=photo
                )
            except AiUnavailable:
                return await self._finish(request_key, role, "unavailable")
            async with self.store.write() as connection:
                await connection.execute(
                    sa.insert(ai_plan_invocations).values(
                        request_key=request_key, day=day, state="started", created_at=time.time()
                    )
                )
            try:
                metrics = AiCallMetrics()
                if measurement is not None:
                    measurement.calls.append(metrics)
                async with asyncio.timeout(30):
                    proposal = await self.plan_adapter.complete(
                        role=role, body=body, metrics=metrics
                    )
            except (AiUnavailable, TimeoutError):
                if metrics.failure_category is None:
                    metrics.failure_category = "timeout"
                async with self.store.write() as connection:
                    await connection.execute(
                        sa.update(ai_plan_invocations)
                        .where(ai_plan_invocations.c.request_key == request_key)
                        .values(state="unknown", completed_at=time.time())
                    )
                return await self._finish(request_key, role, "unavailable")
            known = {food.food_version_id for food in catalog}
            valid = proposal.local_date <= local_date and all(
                item.food_version_id in known for item in proposal.items
            )
            if not valid:
                metrics.failure_category = "schema"
            outcome = AiOutcome(
                request_key=request_key,
                role=role,
                proposal=proposal if valid else None,
                status=("ready" if proposal.intent == "meal" else "clarify")
                if valid
                else "unavailable",
            )
            async with self.store.write() as connection:
                await connection.execute(
                    sa.update(ai_plan_invocations)
                    .where(ai_plan_invocations.c.request_key == request_key)
                    .values(state="done", completed_at=time.time())
                )
                await save_outcome(connection, outcome, time.time())
            return outcome

    async def _finish(self, request_key: str, role: AiRole, status: str) -> AiOutcome:
        if status == "clarify":
            # No catalog is a safe manual-entry fallback, not a model proposal.
            status = "unavailable"
        outcome = AiOutcome.model_validate(
            {"request_key": request_key, "role": role, "status": status}
        )
        async with self.store.write() as connection:
            await save_outcome(connection, outcome, time.time())
        return outcome


async def create_ai_draft(
    connection: AsyncConnection,
    outcome: AiOutcome,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
) -> MealReply:
    """Root's authorized action creates an ordinary revision-bound reviewed draft."""
    if outcome.status != "ready" or outcome.proposal is None:
        await consume_ai_outcome(connection, outcome.request_key)
        return MealReply(outcome.manual_message, "ai_status")
    proposal = outcome.proposal
    planned = []
    origin = "photo estimate" if outcome.role == "meal_photo" else "text interpretation"
    for item in proposal.items:
        food = await get_food_version(connection, item.food_version_id)
        if food.record.preparation == "unspecified":
            raise AiUnavailable("Choose a reviewed food with a known preparation.")
        mass = grams_to_milligrams(exact_decimal(item.grams)) if item.grams is not None else None
        planned.append(
            PlannedItem(
                food_version_id=food.version_id,
                edible_milligrams=mass,
                original_quantity=item.grams,
                original_unit="g" if item.grams is not None else None,
                # Model prose can conceal nutrient claims. Persist only the
                # locally defined origin alongside the explicitly reviewed mass.
                estimate_basis=f"AI {origin}; review food, preparation and amount."
                if mass is not None
                else None,
            )
        )
    content = DraftContent(
        label={
            "breakfast": "Breakfast",
            "lunch": "Lunch",
            "dinner": "Dinner",
            "snack": "Snack",
            "meal": "Meal",
        }.get(proposal.label.strip().casefold(), "Meal"),
        local_date=proposal.local_date,
        timezone=str(reference.tzinfo),
        consumed_at=_date_timestamp(proposal.local_date, reference),
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        items=tuple(planned),
        review_required=True,
    )
    draft = await create_draft(connection, content, action_key=action_key, now=time.time())
    from nutrition_bot.application.ai_metrics import link_draft

    await link_draft(connection, outcome.request_key, draft.id, draft.revision)
    await consume_ai_outcome(connection, outcome.request_key)
    return await draft_receipt(connection, draft, "AI proposal — review every item")
