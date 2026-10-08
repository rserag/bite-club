"""Durable monthly spending reservations. Network operations never occur here."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import profile
from nutrition_bot.adapters.database.schema_ai import (
    ai_attempts,
    ai_budget_periods,
    ai_disabled_routes,
    ai_requests,
)
from nutrition_bot.domain.ai import MONTHLY_MICRO_USD_LIMIT, AiOutcome
from nutrition_bot.domain.ai_policy import ReviewedAiRoute


async def current_budget_period(connection: AsyncConnection, *, now: float) -> str:
    """Freeze boundaries at first activation; timezone changes never reset an active month."""
    existing = await connection.scalar(
        sa.select(ai_budget_periods.c.id).where(
            ai_budget_periods.c.starts_at <= now, ai_budget_periods.c.ends_at > now
        )
    )
    if existing is not None:
        return str(existing)
    timezone = str(await connection.scalar(sa.select(profile.c.timezone)) or "UTC")
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    start = datetime(local.year, local.month, 1, tzinfo=local.tzinfo).timestamp()
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    end = datetime(year, month, 1, tzinfo=local.tzinfo).timestamp()
    previous_end = await connection.scalar(
        sa.select(sa.func.max(ai_budget_periods.c.ends_at)).where(
            ai_budget_periods.c.ends_at <= now
        )
    )
    if previous_end is not None:
        start = max(start, float(previous_end))
    period = f"{local.year:04}-{local.month:02}@{int(start)}"
    await connection.execute(
        sa.insert(ai_budget_periods).values(
            id=period,
            timezone=timezone,
            starts_at=start,
            ends_at=end,
            limit_micro_usd=MONTHLY_MICRO_USD_LIMIT,
        )
    )
    return period


async def used_micro_usd(connection: AsyncConnection, period: str) -> int:
    value = await connection.scalar(
        sa.select(
            sa.func.coalesce(
                sa.func.sum(
                    sa.case(
                        (ai_attempts.c.state == "settled", ai_attempts.c.charged_micro_usd),
                        else_=ai_attempts.c.reserved_micro_usd,
                    )
                ),
                0,
            )
        ).where(ai_attempts.c.period == period)
    )
    return int(value or 0)


async def reserve_attempt(
    connection: AsyncConnection,
    *,
    request_key: str,
    attempt: int,
    route: ReviewedAiRoute,
    now: float,
    evaluation_prefix: str | None = None,
) -> int | None:
    """Caller owns the write lock. Unknown/reserved attempts still consume capacity."""
    if await connection.scalar(
        sa.select(ai_disabled_routes.c.route_id).where(ai_disabled_routes.c.route_id == route.id)
    ):
        return None
    period = await current_budget_period(connection, now=now)
    used = await used_micro_usd(connection, period)
    if used + route.reservation_micro_usd > MONTHLY_MICRO_USD_LIMIT:
        return None
    if evaluation_prefix is not None:
        evaluation_used = int(
            await connection.scalar(
                sa.select(
                    sa.func.coalesce(
                        sa.func.sum(
                            sa.case(
                                (ai_attempts.c.state == "settled", ai_attempts.c.charged_micro_usd),
                                else_=ai_attempts.c.reserved_micro_usd,
                            )
                        ),
                        0,
                    )
                ).where(ai_attempts.c.request_key.startswith(evaluation_prefix, autoescape=True))
            )
            or 0
        )
        if evaluation_used + route.reservation_micro_usd > 1_000_000:
            return None
    result = await connection.execute(
        sa.insert(ai_attempts).values(
            request_key=request_key,
            attempt=attempt,
            period=period,
            route_id=route.id,
            reserved_micro_usd=route.reservation_micro_usd,
            charged_micro_usd=None,
            state="reserved",
            created_at=now,
        )
    )
    assert result.inserted_primary_key is not None
    return int(result.inserted_primary_key[0])


async def settle_attempt(
    connection: AsyncConnection,
    attempt_id: int,
    *,
    charge: int | None,
    generation_id: str | None,
    fault: str | None = None,
    route_id: str,
    now: float,
) -> None:
    await connection.execute(
        sa.update(ai_attempts)
        .where(ai_attempts.c.id == attempt_id, ai_attempts.c.state == "reserved")
        .values(
            charged_micro_usd=charge,
            generation_id=generation_id,
            state="settled" if charge is not None else "unknown",
            completed_at=now,
        )
    )
    if fault in {"policy", "pricing", "usage"}:
        await connection.execute(
            insert(ai_disabled_routes)
            .values(route_id=route_id, reason=fault, disabled_at=now)
            .on_conflict_do_nothing(index_elements=["route_id"])
        )


async def save_outcome(connection: AsyncConnection, outcome: AiOutcome, now: float) -> None:
    await connection.execute(
        sa.update(ai_requests)
        .where(ai_requests.c.request_key == outcome.request_key)
        .values(
            state="unknown" if outcome.status == "unknown" else "done",
            outcome=outcome.model_dump(mode="json"),
            completed_at=now,
        )
    )


async def consume_ai_outcome(connection: AsyncConnection, request_key: str) -> None:
    """Clear structured proposals with the application action, preserving only billing state."""
    await connection.execute(
        sa.update(ai_requests).where(ai_requests.c.request_key == request_key).values(outcome=None)
    )


async def purge_ai_outcomes(connection: AsyncConnection, *, now: float | None = None) -> None:
    """Crash-left proposals expire after seven days; billed attempt metadata contains no inputs."""
    cutoff = (time.time() if now is None else now) - 7 * 24 * 60 * 60
    await connection.execute(
        sa.update(ai_requests)
        .where(ai_requests.c.created_at <= cutoff, ai_requests.c.outcome.is_not(None))
        .values(outcome=None)
    )
