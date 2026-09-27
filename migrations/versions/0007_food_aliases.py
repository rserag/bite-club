"""Explicit aliases pinned to reviewed food versions. Frozen migration."""

import sqlalchemy as sa
from alembic import op

revision = "0007_food_aliases"
down_revision = "0006_meal_drafts"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
sa.Table("actions", metadata, sa.Column("key", sa.String, primary_key=True))
sa.Table("food_versions", metadata, sa.Column("id", sa.Integer, primary_key=True))

food_aliases = sa.Table(
    "food_aliases",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("normalized_name", sa.String, nullable=False, unique=True),
    sa.Column(
        "food_version_id",
        sa.Integer,
        sa.ForeignKey("food_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column(
        "created_action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "updated_action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.CheckConstraint("revision > 0 AND typeof(revision) = 'integer'", name="alias_revision"),
    sa.CheckConstraint("length(name) BETWEEN 1 AND 80", name="alias_name_length"),
    sa.CheckConstraint("length(normalized_name) BETWEEN 1 AND 240", name="alias_canonical_length"),
    sa.CheckConstraint("active IN (0,1)", name="alias_active"),
    sqlite_autoincrement=True,
)

_TRIGGERS = {
    "alias_no_replacement": """
        BEFORE INSERT ON food_aliases
        WHEN EXISTS (SELECT 1 FROM food_aliases
                     WHERE id = NEW.id OR normalized_name = NEW.normalized_name
                        OR created_action_key = NEW.created_action_key
                        OR updated_action_key = NEW.updated_action_key)
        BEGIN SELECT RAISE(ABORT, 'alias identity cannot be replaced'); END
    """,
    "alias_no_delete": """
        BEFORE DELETE ON food_aliases
        BEGIN SELECT RAISE(ABORT, 'deactivate aliases instead of deleting them'); END
    """,
    "alias_identity_revision": """
        BEFORE UPDATE ON food_aliases
        WHEN NEW.id != OLD.id OR NEW.name != OLD.name
          OR NEW.normalized_name != OLD.normalized_name
          OR NEW.created_action_key != OLD.created_action_key
          OR NEW.created_at != OLD.created_at OR NEW.revision != OLD.revision + 1
          OR NEW.updated_action_key = OLD.updated_action_key
        BEGIN SELECT RAISE(ABORT, 'alias identity and revision must be preserved'); END
    """,
    "alias_reviewed_target_insert": """
        BEFORE INSERT ON food_aliases
        WHEN NEW.revision != 1 OR NOT EXISTS (
            SELECT 1 FROM food_versions v JOIN foods f ON f.id = v.food_id
            WHERE v.id = NEW.food_version_id AND v.sealed = 1
              AND f.preparation IN ('raw','cooked','as_sold','as_prepared'))
        BEGIN SELECT RAISE(ABORT, 'alias requires a reviewed food version'); END
    """,
    "alias_reviewed_target_update": """
        BEFORE UPDATE ON food_aliases
        WHEN NOT EXISTS (
            SELECT 1 FROM food_versions v JOIN foods f ON f.id = v.food_id
            WHERE v.id = NEW.food_version_id AND v.sealed = 1
              AND f.preparation IN ('raw','cooked','as_sold','as_prepared'))
        BEGIN SELECT RAISE(ABORT, 'alias requires a reviewed food version'); END
    """,
}


def upgrade() -> None:
    food_aliases.create(op.get_bind())
    for name, statement in _TRIGGERS.items():
        op.execute(f"CREATE TRIGGER {name} {statement}")


def downgrade() -> None:
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER {name}")
    food_aliases.drop(op.get_bind())
