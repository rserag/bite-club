"""Restart-safe guided Telegram interactions."""

from alembic import op

from nutrition_bot.adapters.database.schema_ui import ui_flows

revision = "0024_guided_flows"
down_revision = "0023_ai_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ui_flows.create(op.get_bind())


def downgrade() -> None:
    op.drop_table(ui_flows.name)
