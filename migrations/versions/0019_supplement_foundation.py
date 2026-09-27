"""Reviewed supplement references, products, regimens and actual-dose history."""

import sqlalchemy as sa
from alembic import op

from nutrition_bot.adapters.database.schema_supplements import (
    health_context_events,
    nutrient_reference_sets,
    nutrient_reference_values,
    supplement_certifications,
    supplement_intake_components,
    supplement_intake_revisions,
    supplement_intakes,
    supplement_phase_doses,
    supplement_product_aliases,
    supplement_product_components,
    supplement_product_versions,
    supplement_products,
    supplement_regimen_phases,
    supplement_regimen_revisions,
    supplement_regimens,
    supplement_substances,
)

revision = "0019_supplement_foundation"
down_revision = "0018_recovery_checkins"
branch_labels = None
depends_on = None


TABLES = (
    supplement_substances,
    nutrient_reference_sets,
    nutrient_reference_values,
    supplement_products,
    supplement_product_versions,
    supplement_product_components,
    supplement_product_aliases,
    supplement_certifications,
    supplement_regimens,
    supplement_regimen_revisions,
    supplement_regimen_phases,
    supplement_phase_doses,
    supplement_intakes,
    supplement_intake_revisions,
    supplement_intake_components,
    health_context_events,
)

IMMUTABLE = (
    supplement_substances,
    supplement_product_aliases,
    health_context_events,
)

SEALED_PARENTS = (
    (nutrient_reference_sets, (nutrient_reference_values,)),
    (
        supplement_product_versions,
        (supplement_product_components, supplement_certifications),
    ),
    (
        supplement_regimen_revisions,
        (supplement_regimen_phases, supplement_phase_doses),
    ),
    (supplement_intake_revisions, (supplement_intake_components,)),
)


def _guard(table_name: str) -> None:
    op.execute(
        f"""CREATE TRIGGER {table_name}_immutable_update BEFORE UPDATE ON {table_name}
        BEGIN SELECT RAISE(ABORT, 'immutable supplement history'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER {table_name}_immutable_delete BEFORE DELETE ON {table_name}
        BEGIN SELECT RAISE(ABORT, 'immutable supplement history'); END"""
    )


def _guard_sealed(parent: sa.Table, children: tuple[sa.Table, ...]) -> None:
    op.execute(
        f"""CREATE TRIGGER {parent.name}_sealed_update BEFORE UPDATE ON {parent.name}
        WHEN OLD.sealed = 1
        BEGIN SELECT RAISE(ABORT, 'immutable supplement history'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER {parent.name}_sealed_delete BEFORE DELETE ON {parent.name}
        WHEN OLD.sealed = 1
        BEGIN SELECT RAISE(ABORT, 'immutable supplement history'); END"""
    )
    for child in children:
        foreign_key = (
            "reference_set_id"
            if child is nutrient_reference_values
            else "product_version_id"
            if child in (supplement_product_components, supplement_certifications)
            else "regimen_revision_id"
            if child in (supplement_regimen_phases, supplement_phase_doses)
            else "intake_revision_id"
        )
        for operation, row in (("insert", "NEW"), ("update", "OLD"), ("delete", "OLD")):
            op.execute(
                f"""CREATE TRIGGER {child.name}_{operation}_sealed
                BEFORE {operation.upper()} ON {child.name}
                WHEN EXISTS (SELECT 1 FROM {parent.name}
                    WHERE id = {row}.{foreign_key} AND sealed = 1)
                BEGIN SELECT RAISE(ABORT, 'immutable supplement history'); END"""
            )


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        table.create(bind)
    for table in IMMUTABLE:
        _guard(table.name)
    for parent, children in SEALED_PARENTS:
        _guard_sealed(parent, children)


def downgrade() -> None:
    bind = op.get_bind()
    if any(
        bind.exec_driver_sql(f"SELECT count(*) FROM {table.name}").scalar_one() for table in TABLES
    ):
        raise RuntimeError(
            "Supplement reference or personal history exists; restore a matching backup "
            "instead of discarding it."
        )
    for table in reversed(TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table.name}_immutable_update")
        op.execute(f"DROP TRIGGER IF EXISTS {table.name}_immutable_delete")
        op.drop_table(table.name)
