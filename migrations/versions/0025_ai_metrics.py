"""Private content-free AI measurement records."""

from alembic import op

from nutrition_bot.adapters.database.schema_ai_metrics import ai_metrics

revision = "0025_ai_metrics"
down_revision = "0024_guided_flows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ai_metrics.create(op.get_bind())


def downgrade() -> None:
    op.drop_table(ai_metrics.name)
