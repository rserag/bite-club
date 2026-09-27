import sqlalchemy as sa

SCHEMA_REVISION = "0021_training_allocations"
metadata = sa.MetaData()

profile = sa.Table(
    "profile",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("timezone", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint("id = 1", name="profile_singleton"),
)
cursor = sa.Table(
    "telegram_cursor",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("next_offset", sa.BigInteger, nullable=False),
    sa.CheckConstraint("id = 1", name="cursor_singleton"),
)
inbox = sa.Table(
    "telegram_inbox",
    metadata,
    sa.Column("update_id", sa.BigInteger, primary_key=True),
    sa.Column("payload", sa.JSON, nullable=True),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("received_at", sa.Float, nullable=False),
    sa.Column("processed_at", sa.Float),
    sa.CheckConstraint("status IN ('pending','done','rejected','failed')", name="inbox_status"),
    sa.Index("ix_inbox_pending", "status", "update_id"),
)
actions = sa.Table(
    "actions",
    metadata,
    sa.Column("key", sa.String, primary_key=True),
    sa.Column(
        "update_id", sa.BigInteger, sa.ForeignKey("telegram_inbox.update_id"), nullable=False
    ),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
outbox = sa.Table(
    "outbox",
    metadata,
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
    sa.Index("ix_outbox_due", "status", "next_attempt_at"),
)
heartbeat = sa.Table(
    "runtime_heartbeat",
    metadata,
    sa.Column("component", sa.String, primary_key=True),
    sa.Column("touched_at", sa.Float, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("last_success_at", sa.Float),
)
nutrients = sa.Table(
    "nutrients",
    metadata,
    sa.Column("code", sa.String, primary_key=True),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("unit", sa.String, nullable=False),
    sa.Column("definition", sa.String, nullable=False),
    sa.CheckConstraint("unit IN ('kcal','g','mg','ug')", name="nutrient_unit"),
)
foods = sa.Table(
    "foods",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("preparation", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint(
        "preparation IN ('raw','cooked','as_sold','as_prepared','unspecified')",
        name="food_preparation",
    ),
    sqlite_autoincrement=True,
)
food_versions = sa.Table(
    "food_versions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("food_id", sa.Integer, sa.ForeignKey("foods.id"), nullable=False),
    sa.Column("version_number", sa.Integer, nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("brand", sa.String),
    sa.Column("source_kind", sa.String, nullable=False),
    sa.Column("source_external_id", sa.String),
    sa.Column("source_fetched_at", sa.Float),
    sa.Column("source_published_date", sa.String),
    sa.Column("source_adapter_version", sa.String),
    sa.Column("source_metadata", sa.JSON),
    sa.Column("source_reference", sa.String, nullable=False),
    sa.Column("source_url", sa.String),
    sa.Column("source_license", sa.String, nullable=False),
    sa.Column("source_basis_milligrams", sa.BigInteger, nullable=False),
    sa.Column("reviewed_at", sa.Float, nullable=False),
    sa.Column("calculation_version", sa.String, nullable=False),
    sa.Column("content_sha256", sa.String, nullable=False),
    sa.Column("sealed", sa.Boolean, nullable=False, server_default="0"),
    sa.UniqueConstraint("food_id", "version_number", name="food_version_number"),
    sa.CheckConstraint("version_number > 0", name="food_version_positive"),
    sa.CheckConstraint("sealed IN (0,1)", name="food_version_sealed"),
    sa.CheckConstraint(
        "typeof(source_basis_milligrams) = 'integer' AND source_basis_milligrams > 0",
        name="food_source_basis_positive",
    ),
    sqlite_autoincrement=True,
)
food_nutrients = sa.Table(
    "food_nutrients",
    metadata,
    sa.Column(
        "food_version_id",
        sa.Integer,
        sa.ForeignKey("food_versions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("nutrient_code", sa.String, sa.ForeignKey("nutrients.code"), primary_key=True),
    sa.Column("amount_scaled", sa.BigInteger),
    sa.Column("source_amount", sa.String),
    sa.Column("source_unit", sa.String, nullable=False),
    sa.Column("quality", sa.String, nullable=False),
    sa.Column("note", sa.String),
    sa.CheckConstraint(
        "amount_scaled IS NULL OR (typeof(amount_scaled) = 'integer' AND amount_scaled >= 0)",
        name="food_nutrient_nonnegative",
    ),
    sa.CheckConstraint(
        "(amount_scaled IS NULL) = (source_amount IS NULL)",
        name="food_nutrient_missingness",
    ),
    sa.CheckConstraint("source_unit IN ('kcal','g','mg','ug')", name="food_nutrient_source_unit"),
)
food_portions = sa.Table(
    "food_portions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "food_version_id",
        sa.Integer,
        sa.ForeignKey("food_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("label", sa.String, nullable=False),
    sa.Column("edible_milligrams", sa.BigInteger, nullable=False),
    sa.Column("original_measure", sa.String, nullable=False),
    sa.Column("source", sa.String, nullable=False),
    sa.Column("is_estimate", sa.Boolean, nullable=False),
    sa.UniqueConstraint("food_version_id", "label", name="food_portion_label"),
    sa.CheckConstraint(
        "typeof(edible_milligrams) = 'integer' AND edible_milligrams > 0",
        name="food_portion_positive",
    ),
    sa.CheckConstraint("is_estimate IN (0,1)", name="food_portion_estimate"),
)

food_source_cache = sa.Table(
    "food_source_cache",
    metadata,
    sa.Column("provider", sa.String, primary_key=True),
    sa.Column("external_id", sa.String, primary_key=True),
    sa.Column("content_sha256", sa.String, nullable=False),
    sa.Column("document", sa.JSON, nullable=False),
    sa.Column("fetched_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Index("ix_food_source_cache_expiry", "expires_at"),
)

food_source_links = sa.Table(
    "food_source_links",
    metadata,
    sa.Column("provider", sa.String, primary_key=True),
    sa.Column("external_id", sa.String, primary_key=True),
    sa.Column("preparation", sa.String, primary_key=True),
    sa.Column("food_id", sa.Integer, sa.ForeignKey("foods.id"), nullable=False),
)

meals = sa.Table(
    "meals",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "current_revision_id",
        sa.Integer,
        sa.ForeignKey(
            "meal_revisions.id",
            name="meal_current_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    ),
    sa.Column("source_chat_id", sa.BigInteger, nullable=False),
    sa.Column("source_message_id", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("source_chat_id", "source_message_id", name="meal_source_message"),
    sqlite_autoincrement=True,
)
meal_revisions = sa.Table(
    "meal_revisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("meal_id", sa.Integer, sa.ForeignKey("meals.id", ondelete="CASCADE"), nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column(
        "previous_revision_id",
        sa.Integer,
        sa.ForeignKey("meal_revisions.id", deferrable=True, initially="DEFERRED"),
    ),
    sa.Column(
        "action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("label", sa.String, nullable=False),
    sa.Column("local_date", sa.Date, nullable=False),
    sa.Column("timezone", sa.String, nullable=False),
    sa.Column("consumed_at", sa.Float, nullable=False),
    sa.Column("deleted", sa.Boolean, nullable=False),
    sa.Column("operation", sa.String, nullable=False),
    sa.Column("sealed", sa.Boolean, nullable=False, server_default="0"),
    sa.UniqueConstraint("meal_id", "revision_number", name="meal_revision_number"),
    sa.CheckConstraint("revision_number > 0", name="meal_revision_positive"),
    sa.CheckConstraint("deleted IN (0,1) AND sealed IN (0,1)", name="meal_revision_booleans"),
    sa.CheckConstraint("operation IN ('create','edit','delete','undo')", name="meal_operation"),
    sa.CheckConstraint("length(label) BETWEEN 1 AND 120", name="meal_label_length"),
    sa.Index("ix_meal_revision_date", "local_date"),
    sqlite_autoincrement=True,
)
meal_items = sa.Table(
    "meal_items",
    metadata,
    sa.Column(
        "revision_id",
        sa.Integer,
        sa.ForeignKey("meal_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("item_index", sa.Integer, primary_key=True),
    sa.Column(
        "food_version_id",
        sa.Integer,
        sa.ForeignKey("food_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("food_name", sa.String, nullable=False),
    sa.Column("preparation", sa.String, nullable=False),
    sa.Column("source_kind", sa.String, nullable=False),
    sa.Column("source_reference", sa.String, nullable=False),
    sa.Column("source_url", sa.String),
    sa.Column("source_license", sa.String, nullable=False),
    sa.Column("food_content_sha256", sa.String, nullable=False),
    sa.Column("calculation_version", sa.String, nullable=False),
    sa.Column("provenance", sa.JSON),
    sa.Column("edible_milligrams", sa.BigInteger, nullable=False),
    sa.Column("original_quantity", sa.String, nullable=False),
    sa.Column("original_unit", sa.String, nullable=False),
    sa.Column("recipe_share", sa.JSON(none_as_null=True)),
    sa.Column("recipe_version_id", sa.Integer, sa.ForeignKey("recipe_versions.id")),
    sa.CheckConstraint(
        "(recipe_share IS NULL) = (recipe_version_id IS NULL)", name="meal_recipe_presence"
    ),
    sa.Column("quantity_method", sa.String, nullable=False),
    sa.Column("quantity_basis", sa.String),
    sa.Column("approval_action_key", sa.String, sa.ForeignKey("actions.key", ondelete="RESTRICT")),
    sa.Column("approved_at", sa.Float),
    sa.Column("approval_draft_id", sa.BigInteger),
    sa.Column("approval_draft_revision", sa.BigInteger),
    sa.CheckConstraint("item_index BETWEEN 0 AND 9", name="meal_item_index"),
    sa.CheckConstraint(
        "typeof(edible_milligrams) = 'integer' AND edible_milligrams BETWEEN 1 AND 50000000",
        name="meal_item_mass",
    ),
    sa.CheckConstraint(
        "(quantity_method = 'measured' AND quantity_basis IS NULL "
        "AND approval_action_key IS NULL AND approved_at IS NULL "
        "AND approval_draft_id IS NULL AND approval_draft_revision IS NULL) OR "
        "(quantity_method = 'approved_estimate' AND quantity_basis IS NOT NULL "
        "AND length(trim(quantity_basis)) >= 1 AND length(quantity_basis) <= 300 "
        "AND approval_action_key IS NOT NULL AND substr(approval_action_key,1,9) = 'callback:' "
        "AND length(approval_action_key) > 9 AND approved_at IS NOT NULL "
        "AND approved_at > 0 AND approved_at <= 1.7976931348623157e308 "
        "AND typeof(approval_draft_id) = 'integer' AND approval_draft_id >= 1 "
        "AND typeof(approval_draft_revision) = 'integer' AND approval_draft_revision >= 1)",
        name="meal_quantity_method",
    ),
    sa.CheckConstraint(
        "preparation IN ('raw','cooked','as_sold','as_prepared')", name="meal_food_preparation"
    ),
)
meal_item_nutrients = sa.Table(
    "meal_item_nutrients",
    metadata,
    sa.Column("revision_id", sa.Integer, primary_key=True),
    sa.Column("item_index", sa.Integer, primary_key=True),
    sa.Column(
        "nutrient_code",
        sa.String,
        sa.ForeignKey("nutrients.code", ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column("unit", sa.String, nullable=False),
    sa.Column("amount_scaled", sa.BigInteger),
    sa.Column("quality", sa.String, nullable=False),
    sa.ForeignKeyConstraint(
        ["revision_id", "item_index"],
        ["meal_items.revision_id", "meal_items.item_index"],
        ondelete="CASCADE",
    ),
    sa.CheckConstraint("unit IN ('kcal','g','mg','ug')", name="meal_nutrient_unit"),
    sa.CheckConstraint(
        "amount_scaled IS NULL OR (typeof(amount_scaled) = 'integer' AND amount_scaled >= 0)",
        name="meal_nutrient_amount",
    ),
)

# Register temporary draft tables after their shared foreign-key targets exist.
from nutrition_bot.adapters.database import schema_adaptive as schema_adaptive  # noqa: E402
from nutrition_bot.adapters.database import schema_aliases as schema_aliases  # noqa: E402
from nutrition_bot.adapters.database import schema_allocations as schema_allocations  # noqa: E402
from nutrition_bot.adapters.database import schema_checkins as schema_checkins  # noqa: E402
from nutrition_bot.adapters.database import schema_drafts as schema_drafts  # noqa: E402
from nutrition_bot.adapters.database import schema_favorites as schema_favorites  # noqa: E402
from nutrition_bot.adapters.database import schema_goals as schema_goals  # noqa: E402
from nutrition_bot.adapters.database import schema_recipes as schema_recipes  # noqa: E402
from nutrition_bot.adapters.database import schema_recovery as schema_recovery  # noqa: E402
from nutrition_bot.adapters.database import schema_training as schema_training  # noqa: E402
from nutrition_bot.adapters.database import schema_weights as schema_weights  # noqa: E402
