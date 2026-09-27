"""Provider provenance, expiring lookup cache and selected-food identity links."""

import sqlalchemy as sa
from alembic import op

revision = "0003_food_sources"
down_revision = "0002_food_data"
branch_labels = None
depends_on = None


def upgrade():
    for name, kind in (
        ("source_external_id", sa.String),
        ("source_fetched_at", sa.Float),
        ("source_published_date", sa.String),
        ("source_adapter_version", sa.String),
        ("source_metadata", sa.JSON),
    ):
        op.add_column("food_versions", sa.Column(name, kind, nullable=True))
    op.create_table(
        "food_source_cache",
        sa.Column("provider", sa.String, primary_key=True),
        sa.Column("external_id", sa.String, primary_key=True),
        sa.Column("content_sha256", sa.String, nullable=False),
        sa.Column("document", sa.JSON, nullable=False),
        sa.Column("fetched_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
    )
    op.create_index("ix_food_source_cache_expiry", "food_source_cache", ["expires_at"])
    op.create_table(
        "food_source_links",
        sa.Column("provider", sa.String, primary_key=True),
        sa.Column("external_id", sa.String, primary_key=True),
        sa.Column("preparation", sa.String, primary_key=True),
        sa.Column("food_id", sa.Integer, sa.ForeignKey("foods.id"), nullable=False),
    )


def downgrade():
    op.drop_table("food_source_links")
    op.drop_table("food_source_cache")
    for name in (
        "source_metadata",
        "source_adapter_version",
        "source_published_date",
        "source_fetched_at",
        "source_external_id",
    ):
        op.drop_column("food_versions", name)
