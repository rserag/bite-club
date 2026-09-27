import asyncio
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from nutrition_bot.adapters.database.schema import actions, inbox, nutrients
from nutrition_bot.adapters.database.schema_supplements import (
    nutrient_reference_sets,
    nutrient_reference_values,
    supplement_intake_revisions,
    supplement_intakes,
    supplement_product_components,
    supplement_product_versions,
    supplement_products,
    supplement_substances,
)
from nutrition_bot.domain.supplements import (
    load_protocol_manifest,
    supplement_amount_scaled,
)

MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


async def add_action(connection, number: int) -> str:
    key = f"update:{number}"
    await connection.execute(
        sa.insert(inbox).values(
            update_id=number,
            payload=None,
            status="done",
            received_at=float(number),
            processed_at=float(number),
        )
    )
    await connection.execute(
        sa.insert(actions).values(
            key=key,
            update_id=number,
            kind="synthetic_supplement",
            created_at=float(number),
        )
    )
    return key


def test_reviewed_creatine_manifest_preserves_optional_loading_and_exact_arithmetic():
    manifest = load_protocol_manifest()
    protocol = manifest.protocols[0]
    assert protocol.code == "creatine_monohydrate_adult"
    assert protocol.loading_optional
    assert {option.code for option in protocol.options} == {
        "steady",
        "fixed_loading",
        "weight_loading",
    }
    fixed = next(option for option in protocol.options if option.code == "fixed_loading")
    loading = fixed.phases[0]
    assert loading.duration_days_min == 5 and loading.duration_days_max == 7
    assert loading.frequency == 4 and loading.per_serving_mg == 5_000
    assert supplement_amount_scaled("5", "g", "mg") == 5_000_000_000
    assert supplement_amount_scaled(Decimal("2.5"), "mg", "mg") == 2_500_000
    with pytest.raises(ValueError, match="form-specific"):
        supplement_amount_scaled("1", "IU", "ug")


async def test_reference_scope_and_creatine_category_are_explicit(store):
    async with store.write() as connection:
        await connection.execute(
            sa.insert(supplement_substances).values(
                code="creatine_monohydrate",
                name="Creatine monohydrate",
                category="performance_compound",
                canonical_unit="mg",
                nutrient_code=None,
                definition="Synthetic test definition",
                source_reference="Synthetic test source",
                reviewed_at=1.0,
            )
        )
        await connection.execute(
            sa.insert(supplement_substances).values(
                code="supplemental_magnesium",
                name="Supplemental magnesium",
                category="nutrient",
                canonical_unit="mg",
                nutrient_code="magnesium",
                definition="Synthetic test definition",
                source_reference="Synthetic test source",
                reviewed_at=1.0,
            )
        )
        reference_id = (
            await connection.execute(
                sa.insert(nutrient_reference_sets)
                .values(
                    framework="US_DRI",
                    version="synthetic-v1",
                    name="Synthetic test references",
                    source_url="https://example.test/reference",
                    reviewed_at=1.0,
                    content_sha256="0" * 64,
                    sealed=False,
                )
                .returning(nutrient_reference_sets.c.id)
            )
        ).scalar_one()
        await connection.execute(
            sa.insert(nutrient_reference_values).values(
                reference_set_id=reference_id,
                reference_group="synthetic-adult",
                nutrient_code="magnesium",
                kind="UL",
                applicability="fortified_and_supplement",
                chemical_form="",
                amount_scaled=350_000_000,
                unit="mg",
                note="Synthetic only",
            )
        )
        await connection.execute(
            sa.update(nutrient_reference_sets)
            .where(nutrient_reference_sets.c.id == reference_id)
            .values(sealed=True)
        )
        with pytest.raises(sa.exc.IntegrityError):
            await connection.execute(
                sa.insert(supplement_substances).values(
                    code="invalid_creatine",
                    name="Invalid creatine",
                    category="performance_compound",
                    canonical_unit="mg",
                    nutrient_code="magnesium",
                    definition="Synthetic",
                    source_reference="Synthetic",
                    reviewed_at=1.0,
                )
            )

    async with store.engine.connect() as connection:
        value = (await connection.execute(sa.select(nutrient_reference_values))).mappings().one()
        assert value["kind"] == "UL"
        assert value["applicability"] == "fortified_and_supplement"
        creatine = (
            (
                await connection.execute(
                    sa.select(supplement_substances).where(
                        supplement_substances.c.code == "creatine_monohydrate"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert creatine["category"] == "performance_compound"
        assert creatine["nutrient_code"] is None


async def test_reviewed_label_and_actual_dose_are_immutable_and_unknown_stays_unknown(store):
    async with store.write() as connection:
        await connection.execute(
            sa.insert(supplement_substances).values(
                code="creatine_monohydrate",
                name="Creatine monohydrate",
                category="performance_compound",
                canonical_unit="mg",
                nutrient_code=None,
                definition="Synthetic test definition",
                source_reference="Synthetic test source",
                reviewed_at=1.0,
            )
        )
        product_id = (
            await connection.execute(
                sa.insert(supplement_products)
                .values(created_at=1.0)
                .returning(supplement_products.c.id)
            )
        ).scalar_one()
        version_id = (
            await connection.execute(
                sa.insert(supplement_product_versions)
                .values(
                    product_id=product_id,
                    version_number=1,
                    previous_version_id=None,
                    review_action_key=await add_action(connection, 1),
                    name="Synthetic creatine",
                    brand=None,
                    form="powder",
                    jurisdiction=None,
                    serving_description="one synthetic scoop",
                    source_kind="manual_label",
                    source_external_id=None,
                    source_reference="Synthetic label",
                    source_url=None,
                    source_license=None,
                    reviewed_at=1.0,
                    content_sha256="1" * 64,
                    sealed=False,
                )
                .returning(supplement_product_versions.c.id)
            )
        ).scalar_one()
        await connection.execute(
            sa.insert(supplement_product_components).values(
                product_version_id=version_id,
                component_index=1,
                substance_code="creatine_monohydrate",
                printed_name="Creatine monohydrate",
                chemical_form="monohydrate",
                comparison="exact",
                source_amount="5",
                source_unit="g",
                amount_scaled=5_000_000_000,
                conversion_version="mass-v1",
                daily_value_percent=None,
                note=None,
            )
        )
        await connection.execute(
            sa.update(supplement_product_versions)
            .where(supplement_product_versions.c.id == version_id)
            .values(sealed=True)
        )
        await connection.execute(
            sa.update(supplement_products)
            .where(supplement_products.c.id == product_id)
            .values(current_version_id=version_id)
        )
        intake_id = (
            await connection.execute(
                sa.insert(supplement_intakes)
                .values(source_chat_id=101, source_message_id=2, created_at=2.0)
                .returning(supplement_intakes.c.id)
            )
        ).scalar_one()
        revision_id = (
            await connection.execute(
                sa.insert(supplement_intake_revisions)
                .values(
                    intake_id=intake_id,
                    revision_number=1,
                    previous_revision_id=None,
                    action_key=await add_action(connection, 2),
                    local_date=date(2026, 9, 22),
                    consumed_at=2.0,
                    timezone="UTC",
                    status="taken",
                    product_version_id=None,
                    servings_scaled=None,
                    substance_code="creatine_monohydrate",
                    amount_scaled=5_000_000_000,
                    regimen_revision_id=None,
                    phase_index=None,
                    slot_index=None,
                    sealed=False,
                    deleted=False,
                    operation="create",
                    created_at=2.0,
                )
                .returning(supplement_intake_revisions.c.id)
            )
        ).scalar_one()
        await connection.execute(
            sa.update(supplement_intake_revisions)
            .where(supplement_intake_revisions.c.id == revision_id)
            .values(sealed=True)
        )
        await connection.execute(
            sa.update(supplement_intakes)
            .where(supplement_intakes.c.id == intake_id)
            .values(current_revision_id=revision_id)
        )

    async with store.write() as connection:
        with pytest.raises(sa.exc.IntegrityError, match="immutable supplement history"):
            await connection.execute(
                sa.update(supplement_product_versions)
                .where(supplement_product_versions.c.id == version_id)
                .values(name="Mutated")
            )
    async with store.write() as connection:
        with pytest.raises(sa.exc.IntegrityError, match="immutable supplement history"):
            await connection.execute(
                sa.insert(supplement_product_components).values(
                    product_version_id=version_id,
                    component_index=2,
                    substance_code="creatine_monohydrate",
                    printed_name="Late component",
                    comparison="unknown",
                )
            )
    async with store.write() as connection:
        with pytest.raises(sa.exc.IntegrityError, match="immutable supplement history"):
            await connection.execute(
                sa.update(supplement_intake_revisions)
                .where(supplement_intake_revisions.c.id == revision_id)
                .values(amount_scaled=1)
            )


async def test_unknown_component_cannot_smuggle_a_numeric_amount(store):
    async with store.write() as connection:
        await connection.execute(
            sa.insert(supplement_substances).values(
                code="synthetic_blend",
                name="Synthetic blend",
                category="other",
                canonical_unit="mg",
                nutrient_code=None,
                definition="Synthetic test definition",
                source_reference="Synthetic test source",
                reviewed_at=1.0,
            )
        )
        product_id = (
            await connection.execute(
                sa.insert(supplement_products)
                .values(created_at=1.0)
                .returning(supplement_products.c.id)
            )
        ).scalar_one()
        version_id = (
            await connection.execute(
                sa.insert(supplement_product_versions)
                .values(
                    product_id=product_id,
                    version_number=1,
                    review_action_key=await add_action(connection, 1),
                    name="Synthetic unknown blend",
                    form="powder",
                    serving_description="one serving",
                    source_kind="manual_label",
                    source_reference="Synthetic label",
                    reviewed_at=1.0,
                    content_sha256="2" * 64,
                    sealed=False,
                )
                .returning(supplement_product_versions.c.id)
            )
        ).scalar_one()
        await connection.execute(
            sa.insert(supplement_product_components).values(
                product_version_id=version_id,
                component_index=1,
                substance_code="synthetic_blend",
                printed_name="Synthetic proprietary blend",
                comparison="unknown",
                source_amount=None,
                source_unit=None,
                amount_scaled=None,
                conversion_version=None,
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            await connection.execute(
                sa.insert(supplement_product_components).values(
                    product_version_id=version_id,
                    component_index=2,
                    substance_code="synthetic_blend",
                    printed_name="Invalid invented amount",
                    comparison="unknown",
                    source_amount=None,
                    source_unit=None,
                    amount_scaled=1,
                    conversion_version="invented",
                )
            )


async def test_downgrade_refuses_to_discard_supplement_reference_data(store, settings):
    async with store.write() as connection:
        await connection.execute(
            sa.insert(supplement_substances).values(
                code="creatine_monohydrate",
                name="Creatine monohydrate",
                category="performance_compound",
                canonical_unit="mg",
                nutrient_code=None,
                definition="Synthetic test definition",
                source_reference="Synthetic test source",
                reviewed_at=1.0,
            )
        )
    await store.close()
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    with pytest.raises(RuntimeError, match="Supplement reference or personal history exists"):
        await asyncio.to_thread(command.downgrade, config, "0018_recovery_checkins")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0019_supplement_foundation",
        )
        assert connection.execute("SELECT count(*) FROM supplement_substances").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_nutrient_registry_remains_shared_with_reference_values(store):
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(nutrients)
                .where(nutrients.c.code == "magnesium")
            )
            == 1
        )
