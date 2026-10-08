"""Private preferences, immutable settings receipts and durable notification identities."""

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

notification_settings = sa.Table(
    "notification_settings",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("report_length", sa.String, nullable=False),
    sa.Column("quiet_start_minute", sa.Integer),
    sa.Column("quiet_end_minute", sa.Integer),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.CheckConstraint("id = 1", name="notification_settings_singleton"),
    sa.CheckConstraint("revision > 0", name="notification_settings_revision"),
    sa.CheckConstraint("report_length IN ('short','full')", name="notification_report_length"),
    sa.CheckConstraint(
        "(quiet_start_minute IS NULL AND quiet_end_minute IS NULL) OR "
        "(quiet_start_minute BETWEEN 0 AND 1439 AND quiet_end_minute BETWEEN 0 AND 1439 "
        "AND quiet_start_minute != quiet_end_minute)",
        name="notification_quiet_hours",
    ),
)

schedule_rules = sa.Table(
    "schedule_rules",
    metadata,
    sa.Column("category", sa.String, primary_key=True),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("local_minute", sa.Integer),
    sa.Column("weekday", sa.Integer),
    sa.Column("offset_minutes", sa.Integer),
    sa.CheckConstraint(
        "category IN ('weight','recovery','evening','weekly','training_pre','training_post')",
        name="schedule_category",
    ),
    sa.CheckConstraint("enabled IN (0,1)", name="schedule_enabled"),
    sa.CheckConstraint(
        "local_minute IS NULL OR local_minute BETWEEN 0 AND 1439", name="schedule_time"
    ),
    sa.CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6", name="schedule_weekday"),
    sa.CheckConstraint(
        "(category IN ('training_pre','training_post') AND local_minute IS NULL "
        "AND weekday IS NULL AND offset_minutes BETWEEN -1440 AND 1440) OR "
        "(category IN ('weight','recovery','evening') AND weekday IS NULL "
        "AND offset_minutes IS NULL AND (enabled = 0 OR local_minute IS NOT NULL)) OR "
        "(category = 'weekly' AND offset_minutes IS NULL "
        "AND (enabled = 0 OR (local_minute IS NOT NULL AND weekday IS NOT NULL)))",
        name="schedule_fields",
    ),
)

settings_events = sa.Table(
    "settings_events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False, unique=True),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("snapshot", sa.JSON, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sqlite_autoincrement=True,
)

reminder_jobs = sa.Table(
    "reminder_jobs",
    metadata,
    sa.Column("logical_key", sa.String, primary_key=True),
    sa.Column("category", sa.String, nullable=False),
    sa.Column("local_date", sa.Date, nullable=False),
    sa.Column("session_kind", sa.String),
    sa.Column("settings_revision", sa.Integer, nullable=False),
    sa.Column("plan_fingerprint", sa.String),
    sa.Column("due_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("outbox_id", sa.Integer, sa.ForeignKey("outbox.id")),
    sa.Column("owner_user_id", sa.BigInteger, nullable=False),
    sa.Column("chat_id", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("reason", sa.String),
    sa.CheckConstraint(
        "category IN ('weight','recovery','evening','weekly','training_pre','training_post')",
        name="reminder_category",
    ),
    sa.CheckConstraint(
        "status IN ('scheduled','queued','sent','suppressed','expired','failed')",
        name="reminder_status",
    ),
    sa.CheckConstraint("expires_at > due_at", name="reminder_expiry"),
    sa.CheckConstraint(
        "session_kind IS NULL OR session_kind IN ('gym','bjj')", name="reminder_kind"
    ),
    sa.Index("ix_reminder_due", "status", "due_at"),
)

reminder_deliveries = sa.Table(
    "reminder_deliveries",
    metadata,
    sa.Column("outbox_id", sa.Integer, sa.ForeignKey("outbox.id"), primary_key=True),
    sa.Column(
        "logical_key",
        sa.String,
        sa.ForeignKey("reminder_jobs.logical_key", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Index("ix_reminder_delivery_identity", "logical_key"),
)
