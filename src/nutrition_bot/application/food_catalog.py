"""Food lookup and explicit selection. Network work never holds the SQLite writer."""

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.aliases import resolve_alias
from nutrition_bot.adapters.database.foods import (
    FoodSnapshot,
    get_food_version,
    publish_reviewed_food,
)
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


def infer_preparation(query: str) -> Preparation | None:
    """Recognize explicit preparation words, without guessing from a food name."""
    tokens = set(re.findall(r"[a-z]+", query.casefold()))
    raw = bool(tokens & {"raw", "uncooked"})
    cooked = bool(tokens & {"cooked", "boiled", "baked", "roasted", "fried", "grilled", "steamed"})
    if raw == cooked:
        return None
    return "raw" if raw else "cooked"


async def local_food_search(
    connection: AsyncConnection,
    query: str,
    *,
    preparation: Preparation | None = None,
    limit: int = 10,
) -> tuple[LocalFood, ...]:
    """Shared current-version search. Preparation is a constraint, never a substitution."""
    query = " ".join(query.split())
    if not 1 <= len(query) <= 120 or not 1 <= limit <= 40:
        raise ValueError("Search must contain 1 to 120 characters")
    preparation = preparation or infer_preparation(query)
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
    words = query.casefold().split()
    if preparation:
        statement = statement.where(foods.c.preparation == preparation)
        words = [word for word in words if word not in {"raw", "uncooked", "cooked"}]
    for word in words:
        escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(searchable.like(f"%{escaped}%", escape="\\"))
    rows = (
        (
            await connection.execute(
                statement.order_by(food_versions.c.name, food_versions.c.id).limit(limit)
            )
        )
        .mappings()
        .all()
    )
    result = tuple(
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
    alias = await resolve_alias(connection, query)
    if alias:
        food = await get_food_version(connection, alias.food_version_id)
        if preparation is None or food.record.preparation == preparation:
            pinned = LocalFood(
                food_id=food.food_id,
                version_id=food.version_id,
                name=food.record.name,
                brand=food.record.brand,
                preparation=food.record.preparation,
                source_kind=food.source_kind,
            )
            result = (pinned,) + tuple(
                item for item in result if item.version_id != pinned.version_id
            )
    return result[:limit]


def validate_source_id(value: str, provider: str = "usda") -> str:
    if provider == "openfoodfacts":
        from nutrition_bot.domain.barcodes import normalize_barcode

        return normalize_barcode(value)
    if provider != "usda":
        raise ValueError("Unsupported food provider")
    if not re.fullmatch(r"[1-9][0-9]{0,11}", value):
        raise ValueError("Invalid USDA food identifier")
    return value


def document_hash(document: SourceDocument) -> str:
    # Keep persisted USDA preview hashes compatible with the original adapter.
    value = document.model_dump(mode="json")
    if document.provider == "usda":
        value.pop("provider", None)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def read_cached_source(
    connection: AsyncConnection, source_id: str, now: float, *, provider: str = "usda"
) -> SourcePreview | None:
    row = (
        (
            await connection.execute(
                sa.select(cache).where(
                    cache.c.provider == provider,
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
    if (
        document.source_id != source_id
        or document.provider != provider
        or document_hash(document) != row["content_sha256"]
    ):
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
    provider: Literal["usda", "openfoodfacts"] = "usda",
) -> FoodSnapshot:
    """Select exactly the displayed source revision, within the caller's action transaction."""
    source_id = validate_source_id(source_id, provider)
    preview = await read_cached_source(
        connection, source_id, time.time() if now is None else now, provider=provider
    )
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
            links.c.provider == provider,
            links.c.external_id == source_id,
            links.c.preparation == preparation,
        )
    )
    snapshot = await publish_reviewed_food(
        connection,
        record,
        food_id=food_id,
        provenance=SourceProvenance(
            source_kind=provider,
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
                provider=provider,
                external_id=source_id,
                preparation=preparation,
                food_id=snapshot.food_id,
            )
        )
    return snapshot


class FoodCatalog:
    def __init__(
        self,
        store: Store,
        provider: FoodProvider | None,
        *,
        providers: Mapping[str, FoodProvider] | None = None,
    ):
        self.store = store
        self.provider = provider
        self.providers = {**(providers or {}), **({"usda": provider} if provider else {})}

    async def search(
        self,
        query: str,
        *,
        remote: bool = False,
        offline: bool = False,
        preparation: Preparation | None = None,
        provider: str = "usda",
    ) -> SearchResult:
        query = " ".join(query.split())
        if not 1 <= len(query) <= 120:
            raise ValueError("Search must contain 1 to 120 characters")
        async with self.store.engine.connect() as connection:
            local = await local_food_search(connection, query, preparation=preparation)
        if offline or (local and not remote):
            return SearchResult(local=local, source_status="offline" if offline else "local")
        source_provider = self.providers.get(provider)
        if source_provider is None:
            return SearchResult(local=local, source_status="not_configured")
        try:
            candidates = await source_provider.search(query)
        except ProviderError as exc:
            return SearchResult(
                local=local, source_status=exc.code, retry_after_seconds=exc.retry_after_seconds
            )
        required = preparation or infer_preparation(query)
        if required:
            candidates = tuple(
                item for item in candidates if item.preparation_hint in (required, "unspecified")
            )
        return SearchResult(local=local, remote=candidates, source_status="remote")

    async def lookup(
        self,
        source_id: str,
        *,
        refresh: bool = False,
        offline: bool = False,
        provider: str = "usda",
    ) -> SourcePreview:
        source_id = validate_source_id(source_id, provider)
        now = time.time()
        async with self.store.engine.connect() as connection:
            saved = await read_cached_source(connection, source_id, now, provider=provider)
        if saved and (
            offline or (not refresh and now - (saved.fetched_at or 0) < CACHE_FRESH_SECONDS)
        ):
            return saved.model_copy(
                update={"source_status": "cached_offline" if offline else "cached"}
            )
        source_provider = self.providers.get(provider)
        if offline or source_provider is None:
            reason = "offline" if offline else "not_configured"
            return (
                saved.model_copy(update={"source_status": f"cached_{reason}"})
                if saved
                else SourcePreview(source_status=reason)
            )
        try:
            document = await source_provider.fetch(source_id)
            if document.source_id != source_id or document.provider != provider:
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
                provider=provider,
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
            overflow: Sequence[str] = (
                (
                    await connection.execute(
                        sa.select(cache.c.external_id)
                        .where(cache.c.provider == provider)
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
                        cache.c.provider == provider, cache.c.external_id.in_(overflow)
                    )
                )
        return SourcePreview(
            document=document,
            content_sha256=digest,
            fetched_at=fetched_at,
            expires_at=fetched_at + CACHE_RETENTION_SECONDS,
            source_status="remote",
        )
