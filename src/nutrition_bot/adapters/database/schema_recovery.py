import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

recovery_checkins = sa.Table(
    "recovery_checkins",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "current_revision_id",
        sa.Integer,
        sa.ForeignKey(
            "recovery_revisions.id", use_alter=True, deferrable=True, initially="DEFERRED"
        ),
    ),
    sa.Column("local_date", sa.Date, nullable=False, unique=True),
    sa.Column("created_at", sa.Float, nullable=False),
    sqlite_autoincrement=True,
)

recovery_revisions = sa.Table(
    "recovery_revisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "checkin_id",
        sa.Integer,
        sa.ForeignKey("recovery_checkins.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column(
        "previous_revision_id",
        sa.Integer,
        sa.ForeignKey("recovery_revisions.id", deferrable=True, initially="DEFERRED"),
    ),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("sleep_minutes", sa.Integer),
    sa.Column("soreness", sa.Integer),
    sa.Column("fatigue", sa.Integer),
    sa.Column("readiness", sa.Integer),
    sa.Column("deleted", sa.Boolean, nullable=False),
    sa.Column("operation", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("checkin_id", "revision_number", name="recovery_revision_number"),
    sa.CheckConstraint(
        "sleep_minutes IS NULL OR sleep_minutes BETWEEN 0 AND 1440", name="recovery_sleep"
    ),
    sa.CheckConstraint("soreness IS NULL OR soreness BETWEEN 1 AND 5", name="recovery_soreness"),
    sa.CheckConstraint("fatigue IS NULL OR fatigue BETWEEN 1 AND 5", name="recovery_fatigue"),
    sa.CheckConstraint("readiness IS NULL OR readiness BETWEEN 1 AND 5", name="recovery_readiness"),
    sa.CheckConstraint(
        "sleep_minutes IS NOT NULL OR soreness IS NOT NULL OR "
        "fatigue IS NOT NULL OR readiness IS NOT NULL",
        name="recovery_nonempty",
    ),
    sa.CheckConstraint("deleted IN (0,1)", name="recovery_deleted"),
    sa.CheckConstraint("operation IN ('create','edit','delete','undo')", name="recovery_operation"),
    sqlite_autoincrement=True,
)
