import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

supplement_substances = sa.Table(
    "supplement_substances",
    metadata,
    sa.Column("code", sa.String, primary_key=True),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("category", sa.String, nullable=False),
    sa.Column("canonical_unit", sa.String, nullable=False),
    sa.Column("nutrient_code", sa.String, sa.ForeignKey("nutrients.code"), unique=True),
    sa.Column("definition", sa.String, nullable=False),
    sa.Column("source_reference", sa.String, nullable=False),
    sa.Column("reviewed_at", sa.Float, nullable=False),
    sa.CheckConstraint("length(trim(code)) BETWEEN 1 AND 64", name="supplement_substance_code"),
    sa.CheckConstraint("length(trim(name)) BETWEEN 1 AND 200", name="supplement_substance_name"),
    sa.CheckConstraint(
        "category IN ('nutrient','performance_compound','other')",
        name="supplement_substance_category",
    ),
    sa.CheckConstraint(
        "canonical_unit IN ('g','mg','ug','IU','CFU','mL')",
        name="supplement_substance_unit",
    ),
    sa.CheckConstraint(
        "(category = 'nutrient' AND nutrient_code IS NOT NULL) OR "
        "(category != 'nutrient' AND nutrient_code IS NULL)",
        name="supplement_substance_nutrient_link",
    ),
)

nutrient_reference_sets = sa.Table(
    "nutrient_reference_sets",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("framework", sa.String, nullable=False),
    sa.Column("version", sa.String, nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("source_url", sa.String, nullable=False),
    sa.Column("reviewed_at", sa.Float, nullable=False),
    sa.Column("content_sha256", sa.String, nullable=False),
    sa.Column("sealed", sa.Boolean, nullable=False),
    sa.UniqueConstraint("framework", "version", name="nutrient_reference_framework_version"),
    sa.CheckConstraint("framework IN ('US_DRI','EFSA')", name="nutrient_reference_framework"),
    sa.CheckConstraint("length(content_sha256) = 64", name="nutrient_reference_hash"),
    sa.CheckConstraint("sealed IN (0,1)", name="nutrient_reference_sealed"),
    sqlite_autoincrement=True,
)

nutrient_reference_values = sa.Table(
    "nutrient_reference_values",
    metadata,
    sa.Column(
        "reference_set_id",
        sa.Integer,
        sa.ForeignKey("nutrient_reference_sets.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("reference_group", sa.String, primary_key=True),
    sa.Column("nutrient_code", sa.String, sa.ForeignKey("nutrients.code"), primary_key=True),
    sa.Column("kind", sa.String, primary_key=True),
    sa.Column("applicability", sa.String, primary_key=True),
    sa.Column("chemical_form", sa.String, primary_key=True, server_default=""),
    sa.Column("amount_scaled", sa.BigInteger, nullable=False),
    sa.Column("unit", sa.String, nullable=False),
    sa.Column("note", sa.String),
    sa.CheckConstraint(
        "length(trim(reference_group)) BETWEEN 1 AND 120", name="nutrient_reference_group"
    ),
    sa.CheckConstraint("kind IN ('RDA','AI','limit','UL')", name="nutrient_reference_kind"),
    sa.CheckConstraint(
        "applicability IN ('food','supplement','fortified_and_supplement','total')",
        name="nutrient_reference_applicability",
    ),
    sa.CheckConstraint("amount_scaled > 0", name="nutrient_reference_amount"),
    sa.CheckConstraint("unit IN ('kcal','g','mg','ug')", name="nutrient_reference_unit"),
)

supplement_products = sa.Table(
    "supplement_products",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "current_version_id",
        sa.Integer,
        sa.ForeignKey(
            "supplement_product_versions.id",
            name="supplement_product_current_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sqlite_autoincrement=True,
)

supplement_product_versions = sa.Table(
    "supplement_product_versions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "product_id",
        sa.Integer,
        sa.ForeignKey("supplement_products.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("version_number", sa.Integer, nullable=False),
    sa.Column("previous_version_id", sa.Integer, sa.ForeignKey("supplement_product_versions.id")),
    sa.Column(
        "review_action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True
    ),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("brand", sa.String),
    sa.Column("form", sa.String, nullable=False),
    sa.Column("jurisdiction", sa.String),
    sa.Column("serving_description", sa.String, nullable=False),
    sa.Column("source_kind", sa.String, nullable=False),
    sa.Column("source_external_id", sa.String),
    sa.Column("source_reference", sa.String, nullable=False),
    sa.Column("source_url", sa.String),
    sa.Column("source_license", sa.String),
    sa.Column("reviewed_at", sa.Float, nullable=False),
    sa.Column("content_sha256", sa.String, nullable=False),
    sa.Column("sealed", sa.Boolean, nullable=False),
    sa.UniqueConstraint("product_id", "version_number", name="supplement_product_version_number"),
    sa.CheckConstraint("version_number > 0", name="supplement_product_version_positive"),
    sa.CheckConstraint(
        "form IN ('powder','capsule','tablet','liquid','gummy','other')",
        name="supplement_product_form",
    ),
    sa.CheckConstraint(
        "source_kind IN ('manual_label','DSLD','label_photo')", name="supplement_product_source"
    ),
    sa.CheckConstraint("length(content_sha256) = 64", name="supplement_product_hash"),
    sa.CheckConstraint("sealed IN (0,1)", name="supplement_product_sealed"),
    sqlite_autoincrement=True,
)

supplement_product_components = sa.Table(
    "supplement_product_components",
    metadata,
    sa.Column(
        "product_version_id",
        sa.Integer,
        sa.ForeignKey("supplement_product_versions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("component_index", sa.Integer, primary_key=True),
    sa.Column(
        "substance_code", sa.String, sa.ForeignKey("supplement_substances.code"), nullable=False
    ),
    sa.Column("printed_name", sa.String, nullable=False),
    sa.Column("chemical_form", sa.String),
    sa.Column("comparison", sa.String, nullable=False),
    sa.Column("source_amount", sa.String),
    sa.Column("source_unit", sa.String),
    sa.Column("amount_scaled", sa.BigInteger),
    sa.Column("conversion_version", sa.String),
    sa.Column("daily_value_percent", sa.String),
    sa.Column("note", sa.String),
    sa.CheckConstraint("component_index > 0", name="supplement_component_index"),
    sa.CheckConstraint(
        "comparison IN ('exact','less_than','at_least','unknown')",
        name="supplement_component_comparison",
    ),
    sa.CheckConstraint(
        "source_unit IS NULL OR source_unit IN ('g','mg','ug','IU','CFU','mL','other')",
        name="supplement_component_source_unit",
    ),
    sa.CheckConstraint(
        "amount_scaled IS NULL OR amount_scaled >= 0", name="supplement_component_amount"
    ),
    sa.CheckConstraint(
        "(comparison = 'unknown' AND source_amount IS NULL AND source_unit IS NULL "
        "AND amount_scaled IS NULL AND conversion_version IS NULL) OR "
        "(comparison != 'unknown' AND source_amount IS NOT NULL AND source_unit IS NOT NULL)",
        name="supplement_component_knownness",
    ),
    sa.CheckConstraint(
        "(amount_scaled IS NULL AND conversion_version IS NULL) OR "
        "(amount_scaled IS NOT NULL AND conversion_version IS NOT NULL)",
        name="supplement_component_conversion",
    ),
)

supplement_product_aliases = sa.Table(
    "supplement_product_aliases",
    metadata,
    sa.Column("normalized_name", sa.String, primary_key=True),
    sa.Column("display_name", sa.String, nullable=False),
    sa.Column(
        "product_id",
        sa.Integer,
        sa.ForeignKey("supplement_products.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint(
        "length(trim(normalized_name)) BETWEEN 1 AND 100", name="supplement_alias_normalized"
    ),
    sa.CheckConstraint(
        "length(trim(display_name)) BETWEEN 1 AND 100", name="supplement_alias_display"
    ),
)

supplement_certifications = sa.Table(
    "supplement_certifications",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "product_version_id",
        sa.Integer,
        sa.ForeignKey("supplement_product_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("provider", sa.String, nullable=False),
    sa.Column("program", sa.String, nullable=False),
    sa.Column("lot", sa.String),
    sa.Column("evidence_url", sa.String, nullable=False),
    sa.Column("checked_at", sa.Float, nullable=False),
    sa.UniqueConstraint(
        "product_version_id", "provider", "program", "lot", name="supplement_certification_identity"
    ),
    sqlite_autoincrement=True,
)

supplement_regimens = sa.Table(
    "supplement_regimens",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "current_revision_id",
        sa.Integer,
        sa.ForeignKey(
            "supplement_regimen_revisions.id",
            name="supplement_regimen_current_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sqlite_autoincrement=True,
)

supplement_regimen_revisions = sa.Table(
    "supplement_regimen_revisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "regimen_id",
        sa.Integer,
        sa.ForeignKey("supplement_regimens.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("previous_revision_id", sa.Integer, sa.ForeignKey("supplement_regimen_revisions.id")),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("product_version_id", sa.Integer, sa.ForeignKey("supplement_product_versions.id")),
    sa.Column("substance_code", sa.String, sa.ForeignKey("supplement_substances.code")),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("purpose", sa.String),
    sa.Column("start_date", sa.Date, nullable=False),
    sa.Column("end_date", sa.Date),
    sa.Column("timezone", sa.String, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("provenance", sa.String, nullable=False),
    sa.Column("protocol_code", sa.String),
    sa.Column("protocol_version", sa.String),
    sa.Column("reference_weight_grams", sa.Integer),
    sa.Column("sealed", sa.Boolean, nullable=False),
    sa.Column("deleted", sa.Boolean, nullable=False),
    sa.Column("operation", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("regimen_id", "revision_number", name="supplement_regimen_revision_number"),
    sa.CheckConstraint("revision_number > 0", name="supplement_regimen_revision_positive"),
    sa.CheckConstraint(
        "(product_version_id IS NULL) != (substance_code IS NULL)",
        name="supplement_regimen_subject",
    ),
    sa.CheckConstraint(
        "end_date IS NULL OR end_date >= start_date", name="supplement_regimen_dates"
    ),
    sa.CheckConstraint("state IN ('active','paused','stopped')", name="supplement_regimen_state"),
    sa.CheckConstraint(
        "provenance IN ('user_selected','clinician_directed')", name="supplement_regimen_provenance"
    ),
    sa.CheckConstraint(
        "reference_weight_grams IS NULL OR reference_weight_grams BETWEEN 30000 AND 300000",
        name="supplement_regimen_weight",
    ),
    sa.CheckConstraint("sealed IN (0,1)", name="supplement_regimen_sealed"),
    sa.CheckConstraint("deleted IN (0,1)", name="supplement_regimen_deleted"),
    sa.CheckConstraint(
        "operation IN ('create','edit','pause','resume','stop','undo')",
        name="supplement_regimen_operation",
    ),
    sqlite_autoincrement=True,
)

supplement_regimen_phases = sa.Table(
    "supplement_regimen_phases",
    metadata,
    sa.Column(
        "regimen_revision_id",
        sa.Integer,
        sa.ForeignKey("supplement_regimen_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("phase_index", sa.Integer, primary_key=True),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("start_day_offset", sa.Integer, nullable=False),
    sa.Column("duration_days", sa.Integer),
    sa.Column("daily_target_scaled", sa.BigInteger, nullable=False),
    sa.Column("frequency", sa.Integer, nullable=False),
    sa.CheckConstraint("phase_index > 0", name="supplement_phase_index"),
    sa.CheckConstraint("start_day_offset >= 0", name="supplement_phase_start"),
    sa.CheckConstraint(
        "duration_days IS NULL OR duration_days > 0", name="supplement_phase_duration"
    ),
    sa.CheckConstraint("daily_target_scaled > 0", name="supplement_phase_target"),
    sa.CheckConstraint("frequency BETWEEN 1 AND 24", name="supplement_phase_frequency"),
)

supplement_phase_doses = sa.Table(
    "supplement_phase_doses",
    metadata,
    sa.Column("regimen_revision_id", sa.Integer, primary_key=True),
    sa.Column("phase_index", sa.Integer, primary_key=True),
    sa.Column("slot_index", sa.Integer, primary_key=True),
    sa.Column("amount_scaled", sa.BigInteger, nullable=False),
    sa.Column("reminder_minute", sa.Integer),
    sa.ForeignKeyConstraint(
        ["regimen_revision_id", "phase_index"],
        ["supplement_regimen_phases.regimen_revision_id", "supplement_regimen_phases.phase_index"],
        ondelete="CASCADE",
    ),
    sa.CheckConstraint("slot_index > 0", name="supplement_phase_slot"),
    sa.CheckConstraint("amount_scaled > 0", name="supplement_phase_dose_amount"),
    sa.CheckConstraint(
        "reminder_minute IS NULL OR reminder_minute BETWEEN 0 AND 1439",
        name="supplement_phase_reminder",
    ),
)

supplement_intakes = sa.Table(
    "supplement_intakes",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "current_revision_id",
        sa.Integer,
        sa.ForeignKey(
            "supplement_intake_revisions.id",
            name="supplement_intake_current_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    ),
    sa.Column("source_chat_id", sa.BigInteger, nullable=False),
    sa.Column("source_message_id", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint(
        "source_chat_id", "source_message_id", name="supplement_intake_source_message"
    ),
    sqlite_autoincrement=True,
)

supplement_intake_revisions = sa.Table(
    "supplement_intake_revisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "intake_id",
        sa.Integer,
        sa.ForeignKey("supplement_intakes.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("previous_revision_id", sa.Integer, sa.ForeignKey("supplement_intake_revisions.id")),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("local_date", sa.Date, nullable=False),
    sa.Column("consumed_at", sa.Float, nullable=False),
    sa.Column("timezone", sa.String, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("product_version_id", sa.Integer, sa.ForeignKey("supplement_product_versions.id")),
    sa.Column("servings_scaled", sa.BigInteger),
    sa.Column("substance_code", sa.String, sa.ForeignKey("supplement_substances.code")),
    sa.Column("amount_scaled", sa.BigInteger),
    sa.Column("regimen_revision_id", sa.Integer, sa.ForeignKey("supplement_regimen_revisions.id")),
    sa.Column("phase_index", sa.Integer),
    sa.Column("slot_index", sa.Integer),
    sa.Column("sealed", sa.Boolean, nullable=False),
    sa.Column("deleted", sa.Boolean, nullable=False),
    sa.Column("operation", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("intake_id", "revision_number", name="supplement_intake_revision_number"),
    sa.ForeignKeyConstraint(
        ["regimen_revision_id", "phase_index", "slot_index"],
        [
            "supplement_phase_doses.regimen_revision_id",
            "supplement_phase_doses.phase_index",
            "supplement_phase_doses.slot_index",
        ],
    ),
    sa.CheckConstraint("revision_number > 0", name="supplement_intake_revision_positive"),
    sa.CheckConstraint("status IN ('taken','skipped')", name="supplement_intake_status"),
    sa.CheckConstraint("sealed IN (0,1)", name="supplement_intake_sealed"),
    sa.CheckConstraint(
        "(regimen_revision_id IS NULL AND phase_index IS NULL AND slot_index IS NULL) OR "
        "(regimen_revision_id IS NOT NULL AND phase_index IS NOT NULL "
        "AND slot_index IS NOT NULL)",
        name="supplement_intake_plan_link",
    ),
    sa.CheckConstraint(
        "(status = 'taken' AND ((product_version_id IS NOT NULL AND servings_scaled > 0 "
        "AND substance_code IS NULL AND amount_scaled IS NULL) OR "
        "(product_version_id IS NULL AND servings_scaled IS NULL "
        "AND substance_code IS NOT NULL AND amount_scaled > 0))) OR "
        "(status = 'skipped' AND product_version_id IS NULL AND servings_scaled IS NULL "
        "AND substance_code IS NULL AND amount_scaled IS NULL "
        "AND regimen_revision_id IS NOT NULL AND phase_index IS NOT NULL "
        "AND slot_index IS NOT NULL)",
        name="supplement_intake_subject",
    ),
    sa.CheckConstraint("deleted IN (0,1)", name="supplement_intake_deleted"),
    sa.CheckConstraint(
        "operation IN ('create','edit','delete','undo')", name="supplement_intake_operation"
    ),
    sa.Index("ix_supplement_intake_date", "local_date", "status"),
    sqlite_autoincrement=True,
)

supplement_intake_components = sa.Table(
    "supplement_intake_components",
    metadata,
    sa.Column(
        "intake_revision_id",
        sa.Integer,
        sa.ForeignKey("supplement_intake_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("component_index", sa.Integer, primary_key=True),
    sa.Column(
        "substance_code", sa.String, sa.ForeignKey("supplement_substances.code"), nullable=False
    ),
    sa.Column("amount_scaled", sa.BigInteger),
    sa.Column("value_origin", sa.String, nullable=False),
    sa.CheckConstraint("component_index > 0", name="supplement_intake_component_index"),
    sa.CheckConstraint(
        "amount_scaled IS NULL OR amount_scaled >= 0", name="supplement_intake_component_amount"
    ),
    sa.CheckConstraint(
        "value_origin IN ('label_scaled','direct_reported','unknown')",
        name="supplement_intake_component_origin",
    ),
    sa.CheckConstraint(
        "(value_origin = 'unknown' AND amount_scaled IS NULL) OR "
        "(value_origin != 'unknown' AND amount_scaled IS NOT NULL)",
        name="supplement_intake_component_knownness",
    ),
)

health_context_events = sa.Table(
    "health_context_events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("local_date", sa.Date, nullable=False),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("regimen_revision_id", sa.Integer, sa.ForeignKey("supplement_regimen_revisions.id")),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint(
        "kind IN ('creatine_start','creatine_loading_start','creatine_loading_end',"
        "'creatine_stop','creatine_restart','other_fluid_context')",
        name="health_context_kind",
    ),
    sa.Index("ix_health_context_date", "local_date", "kind"),
    sqlite_autoincrement=True,
)
