"""Initial transport schema. Frozen independently of application metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "profile",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("timezone", sa.String, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.CheckConstraint("id = 1", name="profile_singleton"),
    )
    op.create_table(
        "telegram_cursor",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("next_offset", sa.BigInteger, nullable=False),
        sa.CheckConstraint("id = 1", name="cursor_singleton"),
    )
    op.create_table(
        "telegram_inbox",
        sa.Column("update_id", sa.BigInteger, primary_key=True),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("received_at", sa.Float, nullable=False),
        sa.Column("processed_at", sa.Float),
        sa.CheckConstraint("status IN ('pending','done','rejected','failed')", name="inbox_status"),
    )
    op.create_index("ix_inbox_pending", "telegram_inbox", ["status", "update_id"])
    op.create_table(
        "actions",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column(
            "update_id", sa.BigInteger, sa.ForeignKey("telegram_inbox.update_id"), nullable=False
        ),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("owner_user_id", sa.BigInteger, nullable=False),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Float, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("sent_at", sa.Float),
        sa.Column("telegram_message_id", sa.BigInteger),
        sa.Column("button_token", sa.String, unique=True),
        sa.Column("error_type", sa.String),
        sa.UniqueConstraint("action_key", "kind", name="outbox_action_kind"),
        sa.CheckConstraint("status IN ('queued','sending','sent','failed')", name="outbox_status"),
        sa.CheckConstraint("kind IN ('message','callback_answer')", name="outbox_kind"),
    )
    op.create_index("ix_outbox_due", "outbox", ["status", "next_attempt_at"])
    op.create_table(
        "runtime_heartbeat",
        sa.Column("component", sa.String, primary_key=True),
        sa.Column("touched_at", sa.Float, nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("last_success_at", sa.Float),
    )


def downgrade():
    for table in (
        "runtime_heartbeat",
        "outbox",
        "actions",
        "telegram_inbox",
        "telegram_cursor",
        "profile",
    ):
        op.drop_table(table)
