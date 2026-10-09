"""Bounded relevant food context; retrieval never selects a consumed food."""

import re
import unicodedata

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import food_versions, foods
from nutrition_bot.adapters.database.schema_aliases import food_aliases
from nutrition_bot.domain.ai import AiCatalogItem

MAX_CONTEXT_FOODS = 40
MAX_QUERY_WORDS = 64
_NOISE = frozenset(
    "i ate had have today yesterday about roughly around approximately estimated "
    "meal breakfast lunch dinner snack and with of the a an for grams gram g kg mg "
    "raw cooked uncooked as sold prepared please log save my this that".split()
)


def _query_words(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    words = re.findall(r"[^\W_]+", normalized)
    return tuple(
        dict.fromkeys(word for word in words if word not in _NOISE and not word.isdecimal())
    )[:MAX_QUERY_WORDS]


def _context_name(name: str, aliases: str | None, text: str) -> str:
    if not aliases:
        return name[:120]
    query = set(_query_words(text))
    selected = min(
        aliases.split("; "),
        key=lambda value: (-len(query.intersection(_query_words(value))), value.casefold()),
    )
    return name[:28] + " [alias: " + selected[:80] + "]"


async def ai_food_context(connection: AsyncConnection, text: str = "") -> tuple[AiCatalogItem, ...]:
    """Current identities plus explicitly pinned aliases, ranked by query overlap.

    A full meal can contain several foods and connecting words. Unlike a catalog
    search, context retrieval uses a union of matching words rather than requiring
    every word to occur in each food. AI still has to account for the whole input
    and can only propose identifiers from this exact bounded context.
    """
    latest = (
        sa.select(food_versions.c.food_id, sa.func.max(food_versions.c.version_number).label("v"))
        .where(food_versions.c.sealed.is_(True))
        .group_by(food_versions.c.food_id)
        .subquery()
    )
    aliases = (
        sa.select(
            food_aliases.c.food_version_id,
            sa.func.substr(sa.func.group_concat(food_aliases.c.name, "; "), 1, 1000).label("names"),
        )
        .where(food_aliases.c.active.is_(True))
        .group_by(food_aliases.c.food_version_id)
        .subquery()
    )
    searchable = sa.func.unicode_casefold(
        food_versions.c.name
        + " "
        + sa.func.coalesce(food_versions.c.brand, "")
        + " "
        + sa.func.coalesce(aliases.c.names, "")
    )
    score: sa.ColumnElement[int] = sa.literal(0)
    for word in _query_words(text):
        score = score + sa.case((searchable.contains(word, autoescape=True), 1), else_=0)
    statement = (
        sa.select(
            food_versions.c.id,
            food_versions.c.name,
            foods.c.preparation,
            aliases.c.names.label("aliases"),
        )
        .join(foods, food_versions.c.food_id == foods.c.id)
        .join(latest, food_versions.c.food_id == latest.c.food_id)
        .outerjoin(aliases, aliases.c.food_version_id == food_versions.c.id)
        .where(
            food_versions.c.sealed.is_(True),
            foods.c.preparation != "unspecified",
            sa.or_(
                food_versions.c.version_number == latest.c.v,
                aliases.c.food_version_id.is_not(None),
            ),
        )
        .order_by(score.desc(), food_versions.c.id.desc())
        .limit(MAX_CONTEXT_FOODS)
    )
    rows = (await connection.execute(statement)).mappings().all()
    return tuple(
        AiCatalogItem(
            food_version_id=row["id"],
            name=_context_name(row["name"], row["aliases"], text),
            preparation=row["preparation"],
        )
        for row in rows
    )
