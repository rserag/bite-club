import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

body_weights = sa.Table(
    "body_weights",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "current_revision_id",
        sa.Integer,
        sa.ForeignKey(
            "body_weight_revisions.id",
            name="body_weight_current_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    ),
    sa.Column("source_chat_id", sa.BigInteger, nullable=False),
    sa.Column("source_message_id", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("source_chat_id", "source_message_id", name="body_weight_source_message"),
    sqlite_autoincrement=True,
)

body_weight_revisions = sa.Table(
    "body_weight_revisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "body_weight_id",
        sa.Integer,
        sa.ForeignKey("body_weights.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column(
        "previous_revision_id",
        sa.Integer,
        sa.ForeignKey("body_weight_revisions.id", deferrable=True, initially="DEFERRED"),
    ),
    sa.Column(
        "action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("local_date", sa.Date, nullable=False),
    sa.Column("measured_at", sa.Float, nullable=False),
    sa.Column("timezone", sa.String, nullable=False),
    sa.Column("weight_grams", sa.Integer, nullable=False),
    sa.Column("timing", sa.String, nullable=False),
    sa.Column("deleted", sa.Boolean, nullable=False),
    sa.Column("operation", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("body_weight_id", "revision_number", name="body_weight_revision_number"),
    sa.CheckConstraint("revision_number > 0", name="body_weight_revision_positive"),
    sa.CheckConstraint("weight_grams BETWEEN 20000 AND 500000", name="body_weight_range"),
    sa.CheckConstraint("timing IN ('morning','unspecified')", name="body_weight_timing"),
    sa.CheckConstraint("deleted IN (0,1)", name="body_weight_deleted"),
    sa.CheckConstraint(
        "operation IN ('create','edit','delete','undo')", name="body_weight_operation"
    ),
    sa.Index("ix_body_weight_revision_date", "local_date"),
    sqlite_autoincrement=True,
)
