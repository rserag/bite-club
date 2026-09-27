import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

training_sessions = sa.Table(
    "training_sessions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "current_revision_id",
        sa.Integer,
        sa.ForeignKey(
            "training_session_revisions.id",
            name="training_session_current_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    ),
    sa.Column("source_chat_id", sa.BigInteger, nullable=False),
    sa.Column("source_message_id", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("source_chat_id", "source_message_id", name="training_source_message"),
    sqlite_autoincrement=True,
)

training_session_revisions = sa.Table(
    "training_session_revisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "training_session_id",
        sa.Integer,
        sa.ForeignKey("training_sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column(
        "previous_revision_id",
        sa.Integer,
        sa.ForeignKey("training_session_revisions.id", deferrable=True, initially="DEFERRED"),
    ),
    sa.Column(
        "action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("local_date", sa.Date, nullable=False),
    sa.Column("occurred_at", sa.Float, nullable=False),
    sa.Column("timezone", sa.String, nullable=False),
    sa.Column("duration_minutes", sa.Integer, nullable=False),
    sa.Column("focus", sa.String),
    sa.Column("intensity", sa.String),
    sa.Column("session_rpe_tenths", sa.Integer, nullable=False),
    sa.Column("rpe_source", sa.String, nullable=False),
    sa.Column("deleted", sa.Boolean, nullable=False),
    sa.Column("operation", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint(
        "training_session_id", "revision_number", name="training_session_revision_number"
    ),
    sa.CheckConstraint("revision_number > 0", name="training_revision_positive"),
    sa.CheckConstraint("kind IN ('gym','bjj')", name="training_kind"),
    sa.CheckConstraint("duration_minutes BETWEEN 1 AND 600", name="training_duration"),
    sa.CheckConstraint(
        "(kind = 'gym' AND focus IS NOT NULL AND intensity IS NULL) OR "
        "(kind = 'bjj' AND focus IS NULL)",
        name="training_kind_fields",
    ),
    sa.CheckConstraint(
        "intensity IS NULL OR intensity IN ('easy','medium','hard')", name="training_intensity"
    ),
    sa.CheckConstraint("session_rpe_tenths BETWEEN 0 AND 100", name="training_rpe"),
    sa.CheckConstraint(
        "rpe_source IN ('reported','personal_history','intensity_map','system_default')",
        name="training_rpe_source",
    ),
    sa.CheckConstraint("deleted IN (0,1)", name="training_deleted"),
    sa.CheckConstraint("operation IN ('create','edit','delete','undo')", name="training_operation"),
    sa.Index("ix_training_revision_date", "local_date", "kind"),
    sqlite_autoincrement=True,
)

gym_exercise_occurrences = sa.Table(
    "gym_exercise_occurrences",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "training_revision_id",
        sa.Integer,
        sa.ForeignKey("training_session_revisions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("occurrence_index", sa.Integer, nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.UniqueConstraint(
        "training_revision_id", "occurrence_index", name="gym_exercise_occurrence_index"
    ),
    sa.CheckConstraint("occurrence_index > 0", name="gym_exercise_occurrence_positive"),
    sa.CheckConstraint("length(trim(name)) BETWEEN 1 AND 120", name="gym_exercise_name"),
    sqlite_autoincrement=True,
)

gym_workout_sets = sa.Table(
    "gym_workout_sets",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "exercise_occurrence_id",
        sa.Integer,
        sa.ForeignKey("gym_exercise_occurrences.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("set_index", sa.Integer, nullable=False),
    sa.Column("reps", sa.Integer),
    sa.Column("load_grams", sa.Integer),
    sa.Column("load_convention", sa.String),
    sa.Column("set_rpe_tenths", sa.Integer),
    sa.Column("set_type", sa.String),
    sa.UniqueConstraint("exercise_occurrence_id", "set_index", name="gym_workout_set_index"),
    sa.CheckConstraint("set_index > 0", name="gym_workout_set_positive"),
    sa.CheckConstraint("reps IS NULL OR reps BETWEEN 1 AND 1000", name="gym_workout_reps"),
    sa.CheckConstraint(
        "load_grams IS NULL OR load_grams BETWEEN 1 AND 1000000", name="gym_workout_load"
    ),
    sa.CheckConstraint(
        "(load_convention IS NULL AND load_grams IS NULL) OR "
        "(load_convention = 'external_kg' AND load_grams IS NOT NULL) OR "
        "(load_convention = 'bodyweight' AND load_grams IS NULL)",
        name="gym_workout_load_convention",
    ),
    sa.CheckConstraint(
        "set_rpe_tenths IS NULL OR set_rpe_tenths BETWEEN 0 AND 100", name="gym_workout_rpe"
    ),
    sa.CheckConstraint(
        "set_type IS NULL OR set_type IN ('warmup','working')", name="gym_workout_set_type"
    ),
    sa.CheckConstraint(
        "reps IS NOT NULL OR load_convention IS NOT NULL OR set_rpe_tenths IS NOT NULL",
        name="gym_workout_set_nonempty",
    ),
    sqlite_autoincrement=True,
)

bjj_session_details = sa.Table(
    "bjj_session_details",
    metadata,
    sa.Column(
        "training_revision_id",
        sa.Integer,
        sa.ForeignKey("training_session_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("warmup_minutes", sa.Integer),
    sa.Column("technical_minutes", sa.Integer),
    sa.Column("positional_rounds", sa.Integer),
    sa.Column("positional_round_minutes", sa.Integer),
    sa.Column("sparring_rounds", sa.Integer),
    sa.Column("sparring_round_minutes", sa.Integer),
    sa.CheckConstraint(
        "warmup_minutes IS NULL OR warmup_minutes BETWEEN 1 AND 600", name="bjj_warmup_minutes"
    ),
    sa.CheckConstraint(
        "technical_minutes IS NULL OR technical_minutes BETWEEN 1 AND 600",
        name="bjj_technical_minutes",
    ),
    sa.CheckConstraint(
        "positional_rounds IS NULL OR positional_rounds BETWEEN 1 AND 100",
        name="bjj_positional_rounds",
    ),
    sa.CheckConstraint(
        "positional_round_minutes IS NULL OR positional_round_minutes BETWEEN 1 AND 60",
        name="bjj_positional_round_minutes",
    ),
    sa.CheckConstraint(
        "sparring_rounds IS NULL OR sparring_rounds BETWEEN 1 AND 100",
        name="bjj_sparring_rounds",
    ),
    sa.CheckConstraint(
        "sparring_round_minutes IS NULL OR sparring_round_minutes BETWEEN 1 AND 60",
        name="bjj_sparring_round_minutes",
    ),
    sa.CheckConstraint(
        "(positional_rounds IS NULL) = (positional_round_minutes IS NULL)",
        name="bjj_positional_pair",
    ),
    sa.CheckConstraint(
        "(sparring_rounds IS NULL) = (sparring_round_minutes IS NULL)",
        name="bjj_sparring_pair",
    ),
)

bjj_template_revisions = sa.Table(
    "bjj_template_revisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("revision_number", sa.Integer, nullable=False, unique=True),
    sa.Column("previous_revision_id", sa.Integer, sa.ForeignKey("bjj_template_revisions.id")),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("typical_duration_minutes", sa.Integer, nullable=False),
    sa.Column("warmup_min_minutes", sa.Integer),
    sa.Column("warmup_max_minutes", sa.Integer),
    sa.Column("technical_min_minutes", sa.Integer),
    sa.Column("technical_max_minutes", sa.Integer),
    sa.Column("positional_min_rounds", sa.Integer),
    sa.Column("positional_max_rounds", sa.Integer),
    sa.Column("positional_round_minutes", sa.Integer),
    sa.Column("sparring_min_rounds", sa.Integer),
    sa.Column("sparring_max_rounds", sa.Integer),
    sa.Column("sparring_round_minutes", sa.Integer),
    sa.Column("deleted", sa.Boolean, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint("typical_duration_minutes BETWEEN 1 AND 600", name="bjj_template_duration"),
    sa.CheckConstraint("deleted IN (0,1)", name="bjj_template_deleted"),
    sa.CheckConstraint(
        "(warmup_min_minutes IS NULL AND warmup_max_minutes IS NULL) OR "
        "(warmup_min_minutes BETWEEN 1 AND 600 AND "
        "warmup_max_minutes BETWEEN warmup_min_minutes AND 600)",
        name="bjj_template_warmup_range",
    ),
    sa.CheckConstraint(
        "(technical_min_minutes IS NULL AND technical_max_minutes IS NULL) OR "
        "(technical_min_minutes BETWEEN 1 AND 600 AND "
        "technical_max_minutes BETWEEN technical_min_minutes AND 600)",
        name="bjj_template_technical_range",
    ),
    sa.CheckConstraint(
        "(positional_min_rounds IS NULL AND positional_max_rounds IS NULL AND "
        "positional_round_minutes IS NULL) OR "
        "(positional_min_rounds BETWEEN 1 AND 100 AND "
        "positional_max_rounds BETWEEN positional_min_rounds AND 100 AND "
        "positional_round_minutes BETWEEN 1 AND 60)",
        name="bjj_template_positional_range",
    ),
    sa.CheckConstraint(
        "(sparring_min_rounds IS NULL AND sparring_max_rounds IS NULL AND "
        "sparring_round_minutes IS NULL) OR "
        "(sparring_min_rounds BETWEEN 1 AND 100 AND "
        "sparring_max_rounds BETWEEN sparring_min_rounds AND 100 AND "
        "sparring_round_minutes BETWEEN 1 AND 60)",
        name="bjj_template_sparring_range",
    ),
    sqlite_autoincrement=True,
)

training_schedule_revisions = sa.Table(
    "training_schedule_revisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("revision_number", sa.Integer, nullable=False, unique=True),
    sa.Column("previous_revision_id", sa.Integer, sa.ForeignKey("training_schedule_revisions.id")),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("timezone", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sqlite_autoincrement=True,
)

training_schedule_rules = sa.Table(
    "training_schedule_rules",
    metadata,
    sa.Column(
        "schedule_revision_id",
        sa.Integer,
        sa.ForeignKey("training_schedule_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("weekday", sa.Integer, primary_key=True),
    sa.Column("kind", sa.String, primary_key=True),
    sa.Column("duration_minutes", sa.Integer, nullable=False),
    sa.Column("start_minute", sa.Integer),
    sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="training_schedule_weekday"),
    sa.CheckConstraint("kind IN ('gym','bjj')", name="training_schedule_kind"),
    sa.CheckConstraint("duration_minutes BETWEEN 1 AND 600", name="training_schedule_duration"),
    sa.CheckConstraint(
        "start_minute IS NULL OR start_minute BETWEEN 0 AND 1439", name="training_schedule_start"
    ),
)

training_plan_events = sa.Table(
    "training_plan_events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("local_date", sa.Date, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("kind", sa.String),
    sa.Column("duration_minutes", sa.Integer),
    sa.Column("start_minute", sa.Integer),
    sa.Column("timezone", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint(
        "state IN ('planned','cancelled','rest','clear')", name="training_plan_state"
    ),
    sa.CheckConstraint("kind IS NULL OR kind IN ('gym','bjj')", name="training_plan_kind"),
    sa.CheckConstraint(
        "duration_minutes IS NULL OR duration_minutes BETWEEN 1 AND 600",
        name="training_plan_duration",
    ),
    sa.CheckConstraint(
        "start_minute IS NULL OR start_minute BETWEEN 0 AND 1439", name="training_plan_start"
    ),
    sa.CheckConstraint(
        "(state = 'planned' AND kind IS NOT NULL AND duration_minutes IS NOT NULL) OR "
        "(state IN ('cancelled','rest','clear') AND kind IS NULL "
        "AND duration_minutes IS NULL AND start_minute IS NULL)",
        name="training_plan_fields",
    ),
    sa.Index("ix_training_plan_date", "local_date", "id"),
    sqlite_autoincrement=True,
)
