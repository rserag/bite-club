import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from nutrition_bot.adapters.database.foods import get_food_version, publish_reviewed_food
from nutrition_bot.adapters.database.schema import (
    SCHEMA_REVISION,
    actions,
    food_nutrients,
    food_portions,
    food_source_cache,
    food_source_links,
    food_versions,
    foods,
    outbox,
)
from nutrition_bot.application import food_catalog as catalog_module
from nutrition_bot.application.food_catalog import (
    CACHE_FRESH_SECONDS,
    CACHE_RETENTION_SECONDS,
    FoodCatalog,
    accept_cached_source,
    document_hash,
)
from nutrition_bot.cli import migrate
from nutrition_bot.domain.food_source import FoodCandidate, ProviderError, SourceDocument
from tests.test_food_storage import insert_unsealed_version, reviewed_food


def source_document(source_id="111", **record_overrides):
    return SourceDocument(
        source_id=source_id,
        record=reviewed_food(**record_overrides),
        data_type="Foundation",
        source_published_date="2024-01-01",
        adapter_version="synthetic-adapter-v1",
        warnings=("Synthetic fixture; no dietary information.",),
    )


@dataclass
class FakeProvider:
    documents: dict[str, SourceDocument] = field(default_factory=dict)
    candidates: tuple[FoodCandidate, ...] = ()
    search_error: ProviderError | None = None
    fetch_error: ProviderError | None = None
    search_calls: list[str] = field(default_factory=list)
    fetch_calls: list[str] = field(default_factory=list)

    async def search(self, query):
        self.search_calls.append(query)
        if self.search_error:
            raise self.search_error
        return self.candidates

    async def fetch(self, source_id):
        self.fetch_calls.append(source_id)
        if self.fetch_error:
            raise self.fetch_error
        return self.documents[source_id]


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=1_700_000_000.0)
    monkeypatch.setattr(catalog_module, "time", SimpleNamespace(time=lambda: value.now))
    return value


@pytest.fixture
def provider():
    return FakeProvider(documents={"111": source_document()})


async def test_saved_food_search_is_local_first_and_returns_only_latest_sealed_version(
    store, provider
):
    async with store.write() as connection:
        original = await publish_reviewed_food(
            connection, reviewed_food(name="Synthetic rice", brand="Synthetic brand")
        )
        latest = await publish_reviewed_food(
            connection,
            reviewed_food(name="Synthetic brown rice", brand="Synthetic brand"),
            food_id=original.food_id,
        )
        await insert_unsealed_version(connection, original.food_id, version_number=3)
    result = await FoodCatalog(store, provider).search("  BRAND   rice ")
    assert result.source_status == "local"
    assert len(result.local) == 1
    assert result.local[0].version_id == latest.version_id
    assert result.local[0].preparation == "raw"
    assert result.remote == ()
    assert provider.search_calls == []


async def test_local_search_treats_sql_wildcards_as_literal_text(store, provider):
    async with store.write() as connection:
        target = await publish_reviewed_food(connection, reviewed_food(name="Synthetic 5%_food"))
        await publish_reviewed_food(connection, reviewed_food(name="Synthetic 500 food"))
    catalog = FoodCatalog(store, provider)
    result = await catalog.search("5%_", offline=True)
    assert [food.food_id for food in result.local] == [target.food_id]
    assert provider.search_calls == []


async def test_explicit_remote_search_keeps_local_matches_and_returns_candidates_only(
    store, provider
):
    async with store.write() as connection:
        local = await publish_reviewed_food(connection, reviewed_food())
    provider.candidates = (
        FoodCandidate(
            source_id="111",
            name="Synthetic candidate",
            data_type="Foundation",
            preparation_hint="raw",
        ),
    )
    result = await FoodCatalog(store, provider).search(" synthetic ", remote=True)
    assert [food.food_id for food in result.local] == [local.food_id]
    assert result.remote == provider.candidates
    assert result.source_status == "remote"
    assert provider.search_calls == ["synthetic"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(foods)) == 1
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(food_source_cache)) == 0
        )


@pytest.mark.parametrize("error", ["timeout", "rate_limited", "unavailable", "not_configured"])
async def test_search_provider_failure_preserves_saved_matches_and_fallbacks(
    store, provider, error
):
    async with store.write() as connection:
        local = await publish_reviewed_food(connection, reviewed_food())
    provider.search_error = ProviderError(error, 37 if error == "rate_limited" else None)
    result = await FoodCatalog(store, provider).search("synthetic", remote=True)
    assert result.source_status == error
    assert [food.food_id for food in result.local] == [local.food_id]
    assert result.remote == ()
    assert result.retry_after_seconds == (37 if error == "rate_limited" else None)
    assert result.fallback_options


async def test_search_without_key_or_when_offline_never_calls_provider(store, provider):
    unconfigured = await FoodCatalog(store, None).search("synthetic")
    offline = await FoodCatalog(store, provider).search("synthetic", remote=True, offline=True)
    assert unconfigured.source_status == "not_configured"
    assert offline.source_status == "offline"
    assert unconfigured.local == offline.local == ()
    assert provider.search_calls == []


@pytest.mark.parametrize("query", ["", " \n ", "x" * 121])
async def test_invalid_search_is_rejected_before_provider_call(store, provider, query):
    with pytest.raises(ValueError, match="Search"):
        await FoodCatalog(store, provider).search(query)
    assert provider.search_calls == []


async def test_source_lookup_is_preview_only_and_fresh_cache_avoids_network(store, provider, clock):
    catalog = FoodCatalog(store, provider)
    preview = await catalog.lookup("111")
    assert preview.document == provider.documents["111"]
    assert preview.content_sha256 == document_hash(preview.document)
    assert preview.fetched_at == clock.now
    assert preview.expires_at == clock.now + CACHE_RETENTION_SECONDS
    assert preview.source_status == "remote"
    cached = await catalog.lookup("111")
    assert cached.source_status == "cached"
    assert cached.document == preview.document
    assert provider.fetch_calls == ["111"]
    async with store.engine.connect() as connection:
        for table in (foods, food_versions, food_source_links, actions, outbox):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(food_source_cache)) == 1
        )


async def test_waiting_for_provider_does_not_block_other_local_writes(store):
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowProvider(FakeProvider):
        async def fetch(self, source_id):
            entered.set()
            await release.wait()
            return source_document(source_id)

    task = asyncio.create_task(FoodCatalog(store, SlowProvider()).lookup("111"))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            async with store.write() as connection:
                saved = await publish_reviewed_food(connection, reviewed_food())
    finally:
        release.set()
        await task
    async with store.engine.connect() as connection:
        assert await get_food_version(connection, saved.version_id) == saved


@pytest.mark.parametrize("error", ["timeout", "rate_limited", "unavailable", "not_configured"])
async def test_stale_but_unexpired_preview_survives_provider_failure(store, provider, clock, error):
    catalog = FoodCatalog(store, provider)
    original = await catalog.lookup("111")
    clock.now += CACHE_FRESH_SECONDS + 1
    provider.fetch_error = ProviderError(error, 15 if error == "rate_limited" else None)
    cached = await catalog.lookup("111")
    assert cached.source_status == f"cached_{error}"
    assert cached.document == original.document
    assert cached.content_sha256 == original.content_sha256
    assert cached.expires_at == original.expires_at
    assert cached.retry_after_seconds == (15 if error == "rate_limited" else None)
    assert provider.fetch_calls == ["111", "111"]


async def test_lookup_offline_and_missing_key_keep_unexpired_cache(store, provider, clock):
    catalog = FoodCatalog(store, provider)
    original = await catalog.lookup("111")
    clock.now += CACHE_FRESH_SECONDS + 1
    assert (await catalog.lookup("111", offline=True)).source_status == "cached_offline"
    unconfigured = await FoodCatalog(store, None).lookup("111")
    assert unconfigured.source_status == "cached_not_configured"
    assert unconfigured.document == original.document
    assert (await catalog.lookup("222", offline=True)).source_status == "offline"
    assert (await FoodCatalog(store, None).lookup("222")).source_status == "not_configured"
    assert provider.fetch_calls == ["111"]


async def test_expired_cache_is_unavailable_and_cannot_be_selected(store, provider, clock):
    catalog = FoodCatalog(store, provider)
    original = await catalog.lookup("111")
    clock.now += CACHE_RETENTION_SECONDS
    provider.fetch_error = ProviderError("timeout")
    expired = await catalog.lookup("111")
    assert expired.source_status == "timeout"
    assert expired.document is None
    assert (await catalog.lookup("111", offline=True)).document is None
    with pytest.raises(ValueError, match="expired or changed"):
        async with store.write() as connection:
            await accept_cached_source(
                connection, "111", original.content_sha256, "raw", now=clock.now
            )


async def test_source_identifier_mismatch_does_not_pollute_cache(store, provider):
    provider.documents["111"] = source_document("222")
    result = await FoodCatalog(store, provider).lookup("111")
    assert result.source_status == "invalid_response"
    assert result.document is None
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(food_source_cache)) == 0
        )


@pytest.mark.parametrize("source_id", ["0", "-1", "01", "1/2", "1?key=x", "x", "1" * 13])
async def test_invalid_source_identifiers_are_rejected_before_network(store, provider, source_id):
    with pytest.raises(ValueError, match="identifier"):
        await FoodCatalog(store, provider).lookup(source_id)
    assert provider.fetch_calls == []


async def test_selection_requires_exact_displayed_hash_and_explicit_matching_preparation(
    store, provider, clock
):
    preview = await FoodCatalog(store, provider).lookup("111")
    for expected_hash, preparation, message in (
        ("0" * 64, "raw", "expired or changed"),
        (preview.content_sha256, "unspecified", "Choose the preparation"),
        (preview.content_sha256, "cooked", "Preparation differs"),
    ):
        with pytest.raises(ValueError, match=message):
            async with store.write() as connection:
                await accept_cached_source(
                    connection, "111", expected_hash, preparation, now=clock.now
                )
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(foods)) == 0


async def test_refresh_rejects_selection_of_old_preview_revision(store, provider, clock):
    catalog = FoodCatalog(store, provider)
    original = await catalog.lookup("111")
    provider.documents["111"] = source_document(name="Synthetic corrected source")
    clock.now += 1
    revised = await catalog.lookup("111", refresh=True)
    assert original.content_sha256 != revised.content_sha256
    with pytest.raises(ValueError, match="expired or changed"):
        async with store.write() as connection:
            await accept_cached_source(
                connection, "111", original.content_sha256, "raw", now=clock.now
            )
    async with store.write() as connection:
        saved = await accept_cached_source(
            connection, "111", revised.content_sha256, "raw", now=clock.now
        )
    assert saved.record.name == "Synthetic corrected source"


async def test_selected_source_keeps_provenance_known_zero_and_missing_nutrients(
    store, provider, clock
):
    preview = await FoodCatalog(store, provider).lookup("111")
    async with store.write() as connection:
        snapshot = await accept_cached_source(
            connection, "111", preview.content_sha256, "raw", now=clock.now
        )
    assert snapshot.source_kind == "usda"
    assert snapshot.provenance.external_id == "111"
    assert snapshot.provenance.fetched_at == clock.now
    assert snapshot.provenance.published_date == "2024-01-01"
    assert snapshot.provenance.adapter_version == "synthetic-adapter-v1"
    assert snapshot.record.source_reference == preview.document.record.source_reference
    assert snapshot.record.source_license == preview.document.record.source_license
    assert snapshot.amount_for("protein", 150_000) == Decimal("15")
    assert snapshot.amount_for("sodium", 150_000) == Decimal(0)
    assert snapshot.amount_for("vitamin_d", 150_000) is None
    assert snapshot.amount_for("magnesium", 150_000) is None
    assert all(nutrient.quality == "source_reported" for nutrient in snapshot.nutrients)
    assert snapshot.portions[0].edible_milligrams == 75_125


async def test_reselection_deduplicates_unchanged_source_and_appends_changed_version(
    store, provider, clock
):
    catalog = FoodCatalog(store, provider)
    preview = await catalog.lookup("111")
    async with store.write() as connection:
        first = await accept_cached_source(
            connection, "111", preview.content_sha256, "raw", now=clock.now
        )
    clock.now += 5
    refreshed = await catalog.lookup("111", refresh=True)
    assert refreshed.fetched_at != preview.fetched_at
    async with store.write() as connection:
        repeated = await accept_cached_source(
            connection, "111", refreshed.content_sha256, "raw", now=clock.now
        )
    assert repeated == first
    provider.documents["111"] = source_document(
        nutrients=[{"code": "protein", "amount": "7", "unit": "g"}]
    )
    clock.now += 5
    revision = await catalog.lookup("111", refresh=True)
    async with store.write() as connection:
        second = await accept_cached_source(
            connection, "111", revision.content_sha256, "raw", now=clock.now
        )
    assert second.food_id == first.food_id
    assert second.version_id != first.version_id
    assert second.version_number == 2
    assert second.amount_for("protein", 100_000) == Decimal("14")
    async with store.engine.connect() as connection:
        assert await get_food_version(connection, first.version_id) == first
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 2
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(food_source_links)) == 1
        )


async def test_unspecified_source_selection_separates_preparation_identities(
    store, provider, clock
):
    provider.documents["111"] = source_document(preparation="unspecified")
    preview = await FoodCatalog(store, provider).lookup("111")
    async with store.write() as connection:
        raw = await accept_cached_source(
            connection, "111", preview.content_sha256, "raw", now=clock.now
        )
        cooked = await accept_cached_source(
            connection, "111", preview.content_sha256, "cooked", now=clock.now
        )
    assert raw.food_id != cooked.food_id
    assert raw.record.preparation == "raw"
    assert cooked.record.preparation == "cooked"
    assert raw.amount_for("protein", 100_000) == cooked.amount_for("protein", 100_000)


async def test_caller_action_failure_rolls_back_selected_food_and_identity_link(
    store, provider, clock
):
    preview = await FoodCatalog(store, provider).lookup("111")
    with pytest.raises(RuntimeError, match="Synthetic action failed"):
        async with store.write() as connection:
            await accept_cached_source(
                connection, "111", preview.content_sha256, "raw", now=clock.now
            )
            raise RuntimeError("Synthetic action failed")
    async with store.engine.connect() as connection:
        for table in (foods, food_versions, food_nutrients, food_portions, food_source_links):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(food_source_cache)) == 1
        )


async def test_cache_expiry_and_eviction_preserve_selected_foods(
    store, provider, clock, monkeypatch
):
    monkeypatch.setattr(catalog_module, "MAX_CACHED_SOURCES", 2)
    catalog = FoodCatalog(store, provider)
    preview = await catalog.lookup("111")
    async with store.write() as connection:
        selected = await accept_cached_source(
            connection, "111", preview.content_sha256, "raw", now=clock.now
        )
    for source_id in ("222", "333"):
        provider.documents[source_id] = source_document(source_id)
        clock.now += 1
        await catalog.lookup(source_id)
    async with store.engine.connect() as connection:
        retained = (await connection.execute(sa.select(food_source_cache.c.external_id))).scalars()
        assert set(retained) == {"222", "333"}
        assert await get_food_version(connection, selected.version_id) == selected
    clock.now += CACHE_RETENTION_SECONDS
    provider.documents["444"] = source_document("444")
    await catalog.lookup("444")
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(food_source_cache)) == 1
        )
        assert await get_food_version(connection, selected.version_id) == selected
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(food_source_links)) == 1
        )
    result = await catalog.search("synthetic", offline=True)
    assert [food.version_id for food in result.local] == [selected.version_id]


async def test_tampered_cache_document_is_not_displayed_or_accepted(store, provider, clock):
    preview = await FoodCatalog(store, provider).lookup("111")
    async with store.write() as connection:
        await connection.execute(
            sa.update(food_source_cache).values(
                document=source_document(name="Synthetic tampered value").model_dump(mode="json")
            )
        )
    with pytest.raises(ValueError, match="integrity mismatch"):
        await FoodCatalog(store, provider).lookup("111", offline=True)
    with pytest.raises(ValueError, match="integrity mismatch"):
        async with store.write() as connection:
            await accept_cached_source(
                connection, "111", preview.content_sha256, "raw", now=clock.now
            )


def test_0003_upgrade_preserves_reviewed_manual_records_and_snapshot_triggers(settings):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.attributes["database_url"] = settings.resolved_database_url
    command.upgrade(config, "0002_food_data")
    tables = ("foods", "food_versions", "food_nutrients", "food_portions", "nutrients")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO foods VALUES (1,'raw',1700000000)")
        connection.execute(
            "INSERT INTO food_versions "
            "(id,food_id,version_number,name,source_kind,source_reference,source_license,"
            "source_basis_milligrams,reviewed_at,calculation_version,content_sha256,sealed) "
            "VALUES (1,1,1,'Synthetic legacy record','manual_reviewed','Synthetic label',"
            "'Synthetic fixture',100000,1700000000,'synthetic-v1',?,0)",
            ("0" * 64,),
        )
        connection.executemany(
            "INSERT INTO food_nutrients "
            "(food_version_id,nutrient_code,amount_scaled,source_amount,source_unit,quality) "
            "VALUES (1,?,?,?,?, 'manual_reviewed')",
            [
                ("protein", 1_250_000, "1.25", "g"),
                ("sodium", 0, "0", "mg"),
                ("vitamin_d", None, None, "ug"),
            ],
        )
        connection.execute(
            "INSERT INTO food_portions VALUES "
            "(1,1,'Synthetic portion',50000,'Synthetic measure','Synthetic label',1)"
        )
        connection.execute("UPDATE food_versions SET sealed=1 WHERE id=1")
        connection.commit()
        columns = {
            table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for table in tables
        }
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        }
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).fetchall()
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            SCHEMA_REVISION,
        )
        for table in tables:
            projection = ",".join(columns[table])
            assert (
                connection.execute(f"SELECT {projection} FROM {table}").fetchall() == before[table]
            )
        assert connection.execute(
            "SELECT source_external_id,source_fetched_at,source_published_date,"
            "source_adapter_version FROM food_versions"
        ).fetchall() == [(None, None, None, None)]
        current_triggers = dict(
            connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            ).fetchall()
        )
        # Later migrations may add guards; original food guards must remain identical.
        assert {name: current_triggers.get(name) for name, _ in triggers} == dict(triggers)
        assert connection.execute("SELECT count(*) FROM food_source_cache").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM food_source_links").fetchone() == (0,)
        for statement in (
            "UPDATE food_versions SET name='mutation' WHERE id=1",
            "UPDATE food_versions SET source_external_id='111' WHERE id=1",
            "UPDATE food_nutrients SET amount_scaled=1 WHERE nutrient_code='protein'",
            "DELETE FROM food_portions WHERE food_version_id=1",
            "INSERT OR REPLACE INTO foods VALUES (1,'cooked',0)",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable food"):
                connection.execute(statement)
            connection.rollback()
