"""Food lookup and explicit selection. Network work never holds the SQLite writer."""

import hashlib
import json
import re
import time

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.foods import FoodSnapshot, publish_reviewed_food
from nutrition_bot.adapters.database.schema import (
    food_source_cache as cache,
)
from nutrition_bot.adapters.database.schema import (
    food_source_links as links,
)
from nutrition_bot.adapters.database.schema import (
    food_versions,
    foods,
)
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.domain.food import FrozenModel, Preparation
from nutrition_bot.domain.food_source import (
    FoodCandidate,
    FoodProvider,
    ProviderError,
    SourceDocument,
    SourceProvenance,
)

CACHE_FRESH_SECONDS = 86400
CACHE_RETENTION_SECONDS = 7 * 86400
MAX_CACHED_SOURCES = 200


class LocalFood(FrozenModel):
    food_id: int
    version_id: int
    name: str
    brand: str | None
    preparation: Preparation
    source_kind: str


class SearchResult(FrozenModel):
    local: tuple[LocalFood, ...]
    remote: tuple[FoodCandidate, ...] = ()
    source_status: str
    retry_after_seconds: int | None = None
    fallback_options: tuple[str, ...] = ("Choose a saved food", "Import a reviewed label manually")


class SourcePreview(FrozenModel):
    document: SourceDocument | None = None
    content_sha256: str | None = None
    fetched_at: float | None = None
    expires_at: float | None = None
    source_status: str
    retry_after_seconds: int | None = None
    fallback_options: tuple[str, ...] = ("Choose a saved food", "Import a reviewed label manually")


def validate_source_id(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,11}", value):
        raise ValueError("Invalid USDA food identifier")
    return value


def document_hash(document: SourceDocument) -> str:
    return hashlib.sha256(
        json.dumps(document.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def read_cached_source(
    connection: AsyncConnection, source_id: str, now: float
) -> SourcePreview | None:
    row = (
        (
            await connection.execute(
                sa.select(cache).where(
                    cache.c.provider == "usda",
                    cache.c.external_id == source_id,
                    cache.c.expires_at > now,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    document = SourceDocument.model_validate(row["document"])
    if document.source_id != source_id or document_hash(document) != row["content_sha256"]:
        raise ValueError("Cached source integrity mismatch")
    return SourcePreview(
        document=document,
        content_sha256=row["content_sha256"],
        fetched_at=row["fetched_at"],
        expires_at=row["expires_at"],
        source_status="cached",
    )


async def accept_cached_source(
    connection: AsyncConnection,
    source_id: str,
    expected_hash: str,
    preparation: Preparation,
    *,
    now: float | None = None,
) -> FoodSnapshot:
    """Select exactly the displayed source revision, within the caller's action transaction."""
    validate_source_id(source_id)
    preview = await read_cached_source(connection, source_id, time.time() if now is None else now)
    if preview is None or preview.content_sha256 != expected_hash:
        raise ValueError("Source preview expired or changed; review it again")
    document = preview.document
    assert document is not None and preview.fetched_at is not None
    if preparation == "unspecified":
        raise ValueError("Choose the preparation before selecting this food")
    if document.record.preparation not in ("unspecified", preparation):
        raise ValueError("Preparation differs from this source; choose another food")
    # Validate changes normally rather than using Pydantic's unchecked model_copy.
    record = type(document.record).model_validate(
        {**document.record.model_dump(), "preparation": preparation}
    )
    food_id = await connection.scalar(
        sa.select(links.c.food_id).where(
            links.c.provider == "usda",
            links.c.external_id == source_id,
            links.c.preparation == preparation,
        )
    )
    snapshot = await publish_reviewed_food(
        connection,
        record,
        food_id=food_id,
        provenance=SourceProvenance(
            external_id=source_id,
            fetched_at=preview.fetched_at,
            published_date=document.source_published_date,
            adapter_version=document.adapter_version,
            data_type=document.data_type,
            warnings=document.warnings,
        ),
    )
    if food_id is None:
        await connection.execute(
            sa.insert(links).values(
                provider="usda",
                external_id=source_id,
                preparation=preparation,
                food_id=snapshot.food_id,
            )
        )
    return snapshot


class FoodCatalog:
    def __init__(self, store: Store, provider: FoodProvider | None):
        self.store = store
        self.provider = provider

    async def search(
        self, query: str, *, remote: bool = False, offline: bool = False
    ) -> SearchResult:
        query = " ".join(query.split())
        if not 1 <= len(query) <= 120:
            raise ValueError("Search must contain 1 to 120 characters")
        latest = (
            sa.select(
                food_versions.c.food_id, sa.func.max(food_versions.c.version_number).label("number")
            )
            .where(food_versions.c.sealed.is_(True))
            .group_by(food_versions.c.food_id)
            .subquery()
        )
        statement = (
            sa.select(food_versions, foods.c.preparation)
            .join(foods)
            .join(
                latest,
                sa.and_(
                    food_versions.c.food_id == latest.c.food_id,
                    food_versions.c.version_number == latest.c.number,
                ),
            )
        )
        searchable = sa.func.unicode_casefold(
            food_versions.c.name + " " + sa.func.coalesce(food_versions.c.brand, "")
        )
        for word in query.casefold().split():
            escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(searchable.like(f"%{escaped}%", escape="\\"))
        async with self.store.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        statement.order_by(food_versions.c.name, food_versions.c.id).limit(10)
                    )
                )
                .mappings()
                .all()
            )
        local = tuple(
            LocalFood(
                food_id=row["food_id"],
                version_id=row["id"],
                name=row["name"],
                brand=row["brand"],
                preparation=row["preparation"],
                source_kind=row["source_kind"],
            )
            for row in rows
        )
        if offline or (local and not remote):
            return SearchResult(local=local, source_status="offline" if offline else "local")
        if self.provider is None:
            return SearchResult(local=local, source_status="not_configured")
        try:
            candidates = await self.provider.search(query)
        except ProviderError as exc:
            return SearchResult(
                local=local, source_status=exc.code, retry_after_seconds=exc.retry_after_seconds
            )
        return SearchResult(local=local, remote=candidates, source_status="remote")

    async def lookup(
        self, source_id: str, *, refresh: bool = False, offline: bool = False
    ) -> SourcePreview:
        validate_source_id(source_id)
        now = time.time()
        async with self.store.engine.connect() as connection:
            saved = await read_cached_source(connection, source_id, now)
        if saved and (
            offline or (not refresh and now - (saved.fetched_at or 0) < CACHE_FRESH_SECONDS)
        ):
            return saved.model_copy(
                update={"source_status": "cached_offline" if offline else "cached"}
            )
        if offline or self.provider is None:
            reason = "offline" if offline else "not_configured"
            return (
                saved.model_copy(update={"source_status": f"cached_{reason}"})
                if saved
                else SourcePreview(source_status=reason)
            )
        try:
            document = await self.provider.fetch(source_id)
            if document.source_id != source_id:
                raise ProviderError("invalid_response")
        except ProviderError as exc:
            return (saved or SourcePreview(source_status=exc.code)).model_copy(
                update={
                    "source_status": f"cached_{exc.code}" if saved else exc.code,
                    "retry_after_seconds": exc.retry_after_seconds,
                }
            )
        fetched_at = time.time()
        digest = document_hash(document)
        async with self.store.write() as connection:
            await connection.execute(sa.delete(cache).where(cache.c.expires_at <= fetched_at))
            statement = insert(cache).values(
                provider="usda",
                external_id=source_id,
                content_sha256=digest,
                document=document.model_dump(mode="json"),
                fetched_at=fetched_at,
                expires_at=fetched_at + CACHE_RETENTION_SECONDS,
            )
            await connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[cache.c.provider, cache.c.external_id],
                    set_={
                        key: getattr(statement.excluded, key)
                        for key in ("content_sha256", "document", "fetched_at", "expires_at")
                    },
                )
            )
            # This bounded, disposable cache is separate from indefinitely saved food versions.
            overflow = (
                (
                    await connection.execute(
                        sa.select(cache.c.external_id)
                        .where(cache.c.provider == "usda")
                        .order_by(cache.c.fetched_at.desc(), cache.c.external_id)
                        .offset(MAX_CACHED_SOURCES)
                    )
                )
                .scalars()
                .all()
            )
            if overflow:
                await connection.execute(
                    sa.delete(cache).where(
                        cache.c.provider == "usda", cache.c.external_id.in_(overflow)
                    )
                )
        return SourcePreview(
            document=document,
            content_sha256=digest,
            fetched_at=fetched_at,
            expires_at=fetched_at + CACHE_RETENTION_SECONDS,
            source_status="remote",
        )
