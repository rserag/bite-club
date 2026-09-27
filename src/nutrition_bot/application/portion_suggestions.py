"""Local portion proposals; every result remains an unapproved estimate."""

import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import (
    food_portions,
    food_versions,
    meal_items,
    meal_revisions,
    meals,
)
from nutrition_bot.domain.food import MAX_INTEGER

_MAX_MILLIGRAMS = 50_000_000


@dataclass(frozen=True, slots=True)
class PortionSuggestion:
    edible_milligrams: int
    basis: str


async def suggest_portion(
    connection: AsyncConnection, food_version_id: int
) -> PortionSuggestion | None:
    """Prefer the median of the latest 3–10 qualifying meals for this exact version.

    Duplicate occurrences within one current meal are summed. A meal is excluded
    if any occurrence of this food is estimated or a recipe equivalent, rather than
    treating its remaining measured subtotal as a complete portion. Deleted meals
    and superseded revisions never participate. Recency is consumption time, then
    meal ID for stable ties.
    The median is rounded half-up to a whole gram, with a minimum of one milligram.

    Fallback requires exactly one documented catalog portion. Its recorded mass is
    preserved, but selecting it implicitly is still an estimate, regardless of that
    catalog portion's own ``is_estimate`` flag. This function never writes data.
    """
    if type(food_version_id) is not int or not 1 <= food_version_id <= MAX_INTEGER:
        return None
    if (
        await connection.scalar(
            sa.select(food_versions.c.id).where(
                food_versions.c.id == food_version_id, food_versions.c.sealed.is_(True)
            )
        )
        is None
    ):
        return None
    portions = (
        (
            await connection.execute(
                sa.select(sa.func.sum(meal_items.c.edible_milligrams).label("milligrams"))
                .select_from(
                    meals.join(
                        meal_revisions, meals.c.current_revision_id == meal_revisions.c.id
                    ).join(meal_items, meal_items.c.revision_id == meal_revisions.c.id)
                )
                .where(
                    meal_items.c.food_version_id == food_version_id,
                    meal_revisions.c.sealed.is_(True),
                    meal_revisions.c.deleted.is_(False),
                )
                .group_by(meals.c.id, meal_revisions.c.consumed_at)
                .having(
                    sa.func.sum(
                        sa.case(
                            (
                                sa.and_(
                                    meal_items.c.quantity_method == "measured",
                                    meal_items.c.recipe_version_id.is_(None),
                                ),
                                0,
                            ),
                            else_=1,
                        )
                    )
                    == 0
                )
                .order_by(meal_revisions.c.consumed_at.desc(), meals.c.id.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    if len(portions) >= 3:
        ordered = sorted(int(amount) for amount in portions)
        middle = len(ordered) // 2
        with localcontext() as context:
            context.prec = 50
            median = (
                Decimal(ordered[middle])
                if len(ordered) % 2
                else (Decimal(ordered[middle - 1]) + ordered[middle]) / 2
            )
            milligrams = max(
                1, int((median / 1000).to_integral_value(rounding=ROUND_HALF_UP)) * 1000
            )
        if milligrams > _MAX_MILLIGRAMS:
            return None
        return PortionSuggestion(
            milligrams,
            f"History estimate: median of {len(portions)} measured meals for this food version",
        )
    standards = (
        (
            await connection.execute(
                sa.select(food_portions)
                .where(food_portions.c.food_version_id == food_version_id)
                .limit(2)
            )
        )
        .mappings()
        .all()
    )
    if len(standards) != 1:
        return None
    standard = standards[0]
    label, source = _basis_text(standard["label"]), _basis_text(standard["source"])
    milligrams = int(standard["edible_milligrams"])
    if not label or not source or not 1 <= milligrams <= _MAX_MILLIGRAMS:
        return None
    return PortionSuggestion(milligrams, f"Catalog estimate: {label}; source: {source}")


def _basis_text(value: str) -> str:
    """Keep source attribution readable within the ledger's 300-character limit."""
    text = " ".join(
        "".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in value
        ).split()
    )
    return text if len(text) <= 120 else text[:119] + "…"
