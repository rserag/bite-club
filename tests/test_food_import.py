import json
import sqlite3
from contextlib import closing

import pytest

from nutrition_bot.cli import import_food, main


def synthetic_file(tmp_path):
    path = tmp_path / "reviewed.json"
    path.write_text("""{
        "name": "Synthetic test food", "preparation": "as_sold",
        "source_reference": "Synthetic label fixture", "source_license": "Synthetic",
        "basis_grams": 40.125,
        "nutrients": [
            {"code": "protein", "amount": 2.675, "unit": "g"},
            {"code": "calcium", "amount": null, "unit": "mg"},
            {"code": "sodium", "amount": 0, "unit": "mg"}
        ]
    }""")
    return path


async def test_preview_writes_nothing_and_reviewed_import_keeps_exact_source(
    migrated, tmp_path, capsys
):
    path = synthetic_file(tmp_path)
    await import_food(migrated, path, reviewed=False, food_id=None)
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "preview_only"
    assert preview["record"]["nutrients"][0]["amount"] == "2.675"
    with closing(sqlite3.connect(migrated.database_path)) as db:
        assert db.execute("SELECT count(*) FROM foods").fetchone()[0] == 0
    await import_food(migrated, path, reviewed=True, food_id=None)
    saved = json.loads(capsys.readouterr().out)
    assert saved["status"] == "catalog_saved"
    assert saved["record"]["basis_grams"] == "40.125"
    # Re-importing the same record for its identity reuses the exact version.
    await import_food(migrated, path, reviewed=True, food_id=saved["food_id"])
    assert json.loads(capsys.readouterr().out)["version_id"] == saved["version_id"]
    with closing(sqlite3.connect(migrated.database_path)) as db:
        assert db.execute("SELECT count(*) FROM foods").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM actions").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT amount_scaled FROM food_nutrients WHERE nutrient_code='protein'"
            ).fetchone()[0]
            == 6_666_667
        )


async def test_oversized_input_rejected_before_database_work(migrated, tmp_path):
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="size limit"):
        await import_food(migrated, path, reviewed=True, food_id=None)


def test_cli_invalid_food_input_does_not_disclose_private_values(
    migrated, tmp_path, monkeypatch, capsys
):
    path = tmp_path / "invalid.json"
    path.write_text('{"private-food-and-secret": "private-food-and-secret"}')
    monkeypatch.setenv("DATABASE_URL", migrated.database_url)
    monkeypatch.setattr("sys.argv", ["nutrition-bot", "food-import", "--input-file", str(path)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "private-food-and-secret" not in captured.out + captured.err
    assert "invalid_food_input_or_configuration" in captured.out


async def test_register_explicit_nutrient_definition_without_reinterpreting_old_code(store):
    import sqlalchemy as sa

    from nutrition_bot.adapters.database.foods import publish_reviewed_food, register_nutrient
    from nutrition_bot.adapters.database.schema import nutrients
    from nutrition_bot.domain.food import NutrientDefinition, ReviewedFoodInput

    definition = NutrientDefinition(
        code="available_carbohydrate",
        name="Available carbohydrate",
        unit="g",
        definition="Carbohydrate excluding dietary fiber; synthetic extension test",
    )
    async with store.write() as connection:
        await register_nutrient(connection, definition)
        await register_nutrient(connection, definition)
        snapshot = await publish_reviewed_food(
            connection,
            ReviewedFoodInput.model_validate(
                {
                    "name": "Synthetic extension",
                    "preparation": "as_sold",
                    "source_reference": "Synthetic fixture",
                    "source_license": "Synthetic",
                    "nutrients": [{"code": definition.code, "amount": "7.25", "unit": "g"}],
                }
            ),
        )
        assert snapshot.amount_for(definition.code, 100_000) == 7.25
        assert snapshot.amount_for("carbohydrate", 100_000) is None
    with pytest.raises(ValueError, match="definition differs"):
        async with store.write() as connection:
            await register_nutrient(connection, definition.model_copy(update={"unit": "mg"}))
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(nutrients.c.unit).where(nutrients.c.code == definition.code)
            )
            == "g"
        )


async def test_erased_food_and_version_ids_are_never_reused(store):
    import sqlalchemy as sa

    from nutrition_bot.adapters.database.foods import publish_reviewed_food
    from nutrition_bot.adapters.database.schema import food_versions, foods
    from nutrition_bot.domain.food import ReviewedFoodInput

    record = ReviewedFoodInput.model_validate(
        {
            "name": "Synthetic identity",
            "preparation": "raw",
            "source_reference": "Synthetic",
            "source_license": "Synthetic",
            "nutrients": [{"code": "protein", "amount": "1", "unit": "g"}],
        }
    )
    async with store.write() as connection:
        original = await publish_reviewed_food(connection, record)
        await connection.execute(sa.delete(food_versions))
        await connection.execute(sa.delete(foods))
        replacement = await publish_reviewed_food(connection, record)
        assert replacement.food_id > original.food_id
        assert replacement.version_id > original.version_id


@pytest.mark.parametrize("target", ["nutrient", "food", "version", "version_unique", "child_rowid"])
async def test_sql_replace_cannot_reinterpret_or_replace_sealed_history(store, target):
    import sqlalchemy as sa

    from nutrition_bot.adapters.database.foods import get_food_version, publish_reviewed_food
    from tests.test_food_storage import insert_unsealed_version, reviewed_food

    async with store.write() as connection:
        snapshot = await publish_reviewed_food(connection, reviewed_food())
        draft = await insert_unsealed_version(connection, snapshot.food_id)
    statements = {
        "nutrient": "INSERT OR REPLACE INTO nutrients (code,name,unit,definition) "
        "VALUES ('protein','Changed','mg','Changed')",
        "food": "INSERT OR REPLACE INTO foods (id,preparation,created_at) "
        "VALUES (:food_id,'cooked',0)",
        "version": "INSERT OR REPLACE INTO food_versions SELECT * FROM food_versions "
        "WHERE id=:version_id",
        "version_unique": "INSERT OR REPLACE INTO food_versions "
        "(food_id,version_number,name,source_kind,source_reference,source_license,"
        "source_basis_milligrams,reviewed_at,calculation_version,content_sha256,sealed) "
        "SELECT food_id,version_number,name,source_kind,source_reference,source_license,"
        "source_basis_milligrams,reviewed_at,calculation_version,content_sha256,sealed "
        "FROM food_versions WHERE id=:version_id",
        "child_rowid": "INSERT OR REPLACE INTO food_nutrients "
        "(rowid,food_version_id,nutrient_code,amount_scaled,source_amount,source_unit,quality) "
        "SELECT rowid,:draft,nutrient_code,amount_scaled,source_amount,source_unit,quality "
        "FROM food_nutrients WHERE food_version_id=:version_id AND nutrient_code='protein'",
    }
    with pytest.raises(sa.exc.IntegrityError, match="immutable food"):
        async with store.write() as connection:
            await connection.execute(
                sa.text(statements[target]),
                {"food_id": snapshot.food_id, "version_id": snapshot.version_id, "draft": draft},
            )
    async with store.engine.connect() as connection:
        assert await get_food_version(connection, snapshot.version_id) == snapshot
