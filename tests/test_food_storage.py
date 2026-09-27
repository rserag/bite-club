import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from nutrition_bot.adapters.database.foods import get_food_version, publish_reviewed_food
from nutrition_bot.adapters.database.schema import (
    SCHEMA_REVISION,
    food_nutrients,
    food_portions,
    food_versions,
    foods,
)
from nutrition_bot.cli import migrate
from nutrition_bot.domain.food import ReviewedFoodInput


def reviewed_food(**overrides):
    return ReviewedFoodInput.model_validate(
        {
            "name": "Synthetic reviewed food",
            "preparation": "raw",
            "source_reference": "Synthetic storage-test record, not dietary data",
            "source_license": "Synthetic test fixture",
            "basis_grams": "50",
            "nutrients": [
                {"code": "energy", "amount": "100", "unit": "kcal"},
                {"code": "protein", "amount": "5", "unit": "g"},
                {"code": "sodium", "amount": "0", "unit": "mg"},
                {"code": "vitamin_d", "amount": None, "unit": "ug"},
            ],
            "portions": [
                {
                    "label": "Synthetic portion",
                    "grams": "75.125",
                    "original_measure": "One synthetic portion",
                    "source": "Synthetic weighing record",
                    "is_estimate": False,
                }
            ],
            **overrides,
        }
    )


async def insert_unsealed_version(connection, food_id, *, version_number=2):
    result = await connection.execute(
        sa.insert(food_versions).values(
            food_id=food_id,
            version_number=version_number,
            name="Synthetic incomplete version",
            source_kind="manual_reviewed",
            source_reference="Synthetic pending record",
            source_license="Synthetic test fixture",
            source_basis_milligrams=100_000,
            reviewed_at=0,
            calculation_version="synthetic-test",
            content_sha256="0" * 64,
            sealed=False,
        )
    )
    return result.inserted_primary_key[0]


def test_upgrade_preserves_populated_foundation_and_foreign_keys(settings):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.attributes["database_url"] = settings.resolved_database_url
    command.upgrade(config, "0001_foundation")
    foundation_tables = (
        "profile",
        "telegram_cursor",
        "telegram_inbox",
        "actions",
        "outbox",
        "runtime_heartbeat",
    )
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO profile VALUES (1, 'UTC', 1700000000)")
        connection.execute("INSERT INTO telegram_cursor VALUES (1, 102)")
        connection.execute(
            "INSERT INTO telegram_inbox "
            "(update_id,payload,status,received_at,processed_at) "
            "VALUES (101, '{\"synthetic\":true}', 'done', 1700000000, 1700000001)"
        )
        connection.execute("INSERT INTO actions VALUES ('update:101', 101, 'status', 1700000001)")
        connection.execute(
            "INSERT INTO outbox "
            "(action_key,kind,chat_id,owner_user_id,payload,status,attempts,"
            "next_attempt_at,created_at,sent_at,telegram_message_id,button_token) "
            "VALUES ('update:101','message',101,101,'{\"text\":\"synthetic\"}',"
            "'sent',1,0,1700000001,1700000002,99,'synthetic-button')"
        )
        connection.execute(
            "INSERT INTO runtime_heartbeat VALUES ('receiver',1700000002,'ok',1700000002)"
        )
        connection.commit()
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in foundation_tables
        }
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            SCHEMA_REVISION,
        )
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
        assert connection.execute("SELECT count(*) FROM nutrients").fetchone()[0] >= 13
        assert connection.execute("SELECT count(*) FROM food_versions").fetchone()[0] == 0


async def test_versions_preserve_original_values_and_known_missingness(store):
    original = reviewed_food()
    revised = reviewed_food(
        name="Synthetic revised food",
        nutrients=[{"code": "energy", "amount": "110", "unit": "kcal"}],
    )
    async with store.write() as connection:
        first = await publish_reviewed_food(connection, original)
        repeated = await publish_reviewed_food(connection, original, food_id=first.food_id)
        second = await publish_reviewed_food(connection, revised, food_id=first.food_id)
    async with store.engine.connect() as connection:
        old = await get_food_version(connection, first.version_id)
        new = await get_food_version(connection, second.version_id)
        assert old == first == repeated
        assert old.record == original
        assert old.food_id == new.food_id
        assert (old.version_number, new.version_number) == (1, 2)
        assert old.version_id != new.version_id
        assert old.content_sha256 != new.content_sha256
        assert old.amount_for("energy", 150_000) == Decimal("300")
        assert new.amount_for("energy", 150_000) == Decimal("330")
        assert old.amount_for("sodium", 150_000) == Decimal(0)
        assert old.amount_for("vitamin_d", 150_000) is None
        assert old.amount_for("magnesium", 150_000) is None
        assert old.portions[0].edible_milligrams == 75_125
        assert old.portions[0].is_estimate is False
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 2


async def test_raw_and_cooked_require_separate_food_identities(store):
    async with store.write() as connection:
        raw = await publish_reviewed_food(connection, reviewed_food())
    with pytest.raises(ValueError, match="different preparation"):
        async with store.write() as connection:
            await publish_reviewed_food(
                connection, reviewed_food(preparation="cooked"), food_id=raw.food_id
            )
    async with store.write() as connection:
        cooked = await publish_reviewed_food(connection, reviewed_food(preparation="cooked"))
    assert raw.food_id != cooked.food_id
    assert raw.record.preparation == "raw"
    assert cooked.record.preparation == "cooked"
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == 2


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE food_versions SET name='mutated' WHERE id=:version_id",
        "UPDATE food_versions SET sealed=0 WHERE id=:version_id",
        "UPDATE nutrients SET unit='mg' WHERE code='protein'",
        "UPDATE nutrients SET definition='mutated' WHERE code='protein'",
        "UPDATE nutrients SET code='mutated' WHERE code='protein'",
        "UPDATE foods SET preparation='cooked' WHERE id=:food_id",
        "INSERT INTO food_nutrients "
        "(food_version_id,nutrient_code,amount_scaled,source_amount,source_unit,quality) "
        "VALUES (:version_id,'vitamin_c',1,'0.000001','mg','manual_reviewed')",
        "UPDATE food_nutrients SET amount_scaled=1,source_amount='0.000001' "
        "WHERE food_version_id=:version_id AND nutrient_code='protein'",
        "DELETE FROM food_nutrients WHERE food_version_id=:version_id",
        "INSERT INTO food_portions "
        "(food_version_id,label,edible_milligrams,original_measure,source,is_estimate) "
        "VALUES (:version_id,'Another portion',1,'Synthetic','Synthetic',1)",
        "UPDATE food_portions SET edible_milligrams=1 WHERE food_version_id=:version_id",
        "DELETE FROM food_portions WHERE food_version_id=:version_id",
    ],
)
async def test_sealed_snapshots_and_definitions_reject_direct_sql_mutations(store, statement):
    async with store.write() as connection:
        snapshot = await publish_reviewed_food(connection, reviewed_food())
    with pytest.raises(sa.exc.IntegrityError, match="immutable food"):
        async with store.write() as connection:
            await connection.execute(
                sa.text(statement),
                {"version_id": snapshot.version_id, "food_id": snapshot.food_id},
            )
    async with store.engine.connect() as connection:
        assert await get_food_version(connection, snapshot.version_id) == snapshot


async def test_referenced_registry_definition_cannot_be_deleted(store):
    async with store.write() as connection:
        snapshot = await publish_reviewed_food(connection, reviewed_food())
    with pytest.raises(sa.exc.IntegrityError, match="FOREIGN KEY"):
        async with store.write() as connection:
            await connection.execute(sa.text("DELETE FROM nutrients WHERE code='protein'"))
    async with store.engine.connect() as connection:
        assert await get_food_version(connection, snapshot.version_id) == snapshot


@pytest.mark.parametrize("table", ["food_nutrients", "food_portions"])
@pytest.mark.parametrize("direction", ["from_sealed", "into_sealed"])
async def test_child_cannot_move_out_of_or_into_sealed_version(store, table, direction):
    async with store.write() as connection:
        snapshot = await publish_reviewed_food(connection, reviewed_food())
        unsealed_id = await insert_unsealed_version(connection, snapshot.food_id)
        await connection.execute(
            sa.insert(food_nutrients).values(
                food_version_id=unsealed_id,
                nutrient_code="vitamin_c",
                amount_scaled=1,
                source_amount="0.000001",
                source_unit="mg",
                quality="manual_reviewed",
            )
        )
        await connection.execute(
            sa.insert(food_portions).values(
                food_version_id=unsealed_id,
                label="Different synthetic portion",
                edible_milligrams=1000,
                original_measure="Synthetic",
                source="Synthetic",
                is_estimate=True,
            )
        )
    source_id, target_id = (
        (snapshot.version_id, unsealed_id)
        if direction == "from_sealed"
        else (unsealed_id, snapshot.version_id)
    )
    # Distinct nutrient codes/portion labels avoid uniqueness errors masking the trigger.
    with pytest.raises(sa.exc.IntegrityError, match="immutable food snapshot"):
        async with store.write() as connection:
            await connection.execute(
                sa.text(
                    f"UPDATE {table} SET food_version_id=:target WHERE food_version_id=:source"
                ),
                {"source": source_id, "target": target_id},
            )
    async with store.engine.connect() as connection:
        assert await get_food_version(connection, snapshot.version_id) == snapshot


async def test_whole_version_erasure_cascades_without_mutating_other_history(store):
    async with store.write() as connection:
        first = await publish_reviewed_food(connection, reviewed_food())
        second = await publish_reviewed_food(
            connection, reviewed_food(name="Synthetic revision"), food_id=first.food_id
        )
        await connection.execute(
            sa.delete(food_versions).where(food_versions.c.id == first.version_id)
        )
    async with store.engine.connect() as connection:
        with pytest.raises(ValueError, match="does not exist"):
            await get_food_version(connection, first.version_id)
        assert await get_food_version(connection, second.version_id) == second
        for child in (food_nutrients, food_portions):
            assert (
                await connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(child)
                    .where(child.c.food_version_id == first.version_id)
                )
                == 0
            )
        assert (await connection.execute(sa.text("PRAGMA foreign_key_check"))).all() == []


async def test_unsealed_versions_are_not_readable(store):
    async with store.write() as connection:
        published = await publish_reviewed_food(connection, reviewed_food())
        unsealed_id = await insert_unsealed_version(connection, published.food_id)
    async with store.engine.connect() as connection:
        with pytest.raises(ValueError, match="does not exist"):
            await get_food_version(connection, unsealed_id)
        assert await get_food_version(connection, published.version_id) == published


async def test_late_storage_failure_rolls_back_entire_publication(store):
    async with store.write() as connection:
        await connection.exec_driver_sql(
            "CREATE TRIGGER synthetic_reject_portion BEFORE INSERT ON food_portions "
            "BEGIN SELECT RAISE(ABORT,'synthetic late failure'); END"
        )
    with pytest.raises(sa.exc.IntegrityError, match="synthetic late failure"):
        async with store.write() as connection:
            await publish_reviewed_food(connection, reviewed_food())
    async with store.engine.connect() as connection:
        for table in (foods, food_versions, food_nutrients, food_portions):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0


async def test_caller_failure_rolls_back_food_after_successful_publication(store):
    with pytest.raises(RuntimeError, match="synthetic action failed"):
        async with store.write() as connection:
            await publish_reviewed_food(connection, reviewed_food())
            raise RuntimeError("synthetic action failed")
    async with store.engine.connect() as connection:
        for table in (foods, food_versions, food_nutrients, food_portions):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
