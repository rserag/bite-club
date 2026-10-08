"""Short-lived, restart-safe guided interactions; no diary data is duplicated."""

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

ui_flows = sa.Table(
    "ui_flows",
    metadata,
    sa.Column("owner_id", sa.BigInteger, primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("stage", sa.String, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.CheckConstraint("revision > 0", name="ui_flow_revision"),
)
