"""Durable interactive food discovery."""

from alembic import op

from nutrition_bot.adapters.database.schema_food_discovery import food_lookup_jobs

revision = "0026_food_discovery"
down_revision = "0025_ai_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    food_lookup_jobs.create(op.get_bind())


def downgrade() -> None:
    op.drop_table(food_lookup_jobs.name)
