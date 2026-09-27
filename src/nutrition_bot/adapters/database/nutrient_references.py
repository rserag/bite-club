"""Install the bundled immutable reference version without choosing a user group."""

from dataclasses import asdict
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema_supplements import nutrient_reference_sets as sets
from nutrition_bot.adapters.database.schema_supplements import nutrient_reference_values as values
from nutrition_bot.domain.nutrient_references import SOURCE, VERSION, bundled_values, content_hash


async def install_bundled(connection: AsyncConnection) -> int:
    rows = bundled_values()
    digest = content_hash(rows)
    existing = (
        (
            await connection.execute(
                sa.select(sets).where(
                    sets.c.framework == "US_DRI",
                    sets.c.version == VERSION,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing:
        stored = (
            (
                await connection.execute(
                    sa.select(values).where(
                        values.c.reference_set_id == existing["id"],
                    )
                )
            )
            .mappings()
            .all()
        )
        expected = {tuple(sorted(asdict(r).items())) for r in rows}
        actual = {
            tuple(sorted((k, v) for k, v in r.items() if k != "reference_set_id")) for r in stored
        }
        if not existing["sealed"] or existing["content_sha256"] != digest or actual != expected:
            raise ValueError(
                "Bundled nutrient reference integrity check failed; operator review required."
            )
        return int(existing["id"])
    rid = int(
        (
            await connection.execute(
                sa.insert(sets)
                .values(
                    framework="US_DRI",
                    version=VERSION,
                    name="US/Canadian adult DRIs: supported nutrients",
                    source_url=SOURCE,
                    reviewed_at=datetime(2026, 9, 27, tzinfo=UTC).timestamp(),
                    content_sha256=digest,
                    sealed=False,
                )
                .returning(sets.c.id)
            )
        ).scalar_one()
    )
    await connection.execute(
        sa.insert(values), [dict(reference_set_id=rid, **asdict(r)) for r in rows]
    )
    await connection.execute(sa.update(sets).where(sets.c.id == rid).values(sealed=True))
    return rid
