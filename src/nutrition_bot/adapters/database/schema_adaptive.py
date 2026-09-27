import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

adaptive_reviews = sa.Table(
    "adaptive_reviews",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("review_end", sa.Date, nullable=False, unique=True),
    sa.Column("window_start", sa.Date, nullable=False),
    sa.Column("eligible", sa.Boolean, nullable=False),
    sa.Column("reason_codes", sa.JSON, nullable=False),
    sa.Column("days_since_target_change", sa.Integer, nullable=False),
    sa.Column("measured_dates", sa.Integer, nullable=False),
    sa.Column("weight_span_days", sa.Integer, nullable=False),
    sa.Column("max_weight_gap_days", sa.Integer, nullable=False),
    sa.Column("block_measurement_counts", sa.JSON, nullable=False),
    sa.Column("complete_food_days", sa.Integer, nullable=False),
    sa.Column("unresolved_drafts", sa.Integer, nullable=False),
    sa.Column("complete_energy_days", sa.Integer, nullable=False),
    sa.Column("targeted_energy_days", sa.Integer, nullable=False),
    sa.Column("mean_intake_kcal", sa.Float),
    sa.Column("mean_target_kcal", sa.Float),
    sa.Column("target_rate_grams_per_week", sa.Integer, nullable=False),
    sa.Column("observed_rate_grams_per_week", sa.Float),
    sa.Column("mismatch_direction", sa.Integer, nullable=False),
    sa.Column("raw_delta_kcal", sa.Float),
    sa.Column(
        "action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint("eligible IN (0,1)", name="adaptive_review_eligible"),
    sa.CheckConstraint("mismatch_direction IN (-1,0,1)", name="adaptive_review_direction"),
    sqlite_autoincrement=True,
)

adaptive_proposals = sa.Table(
    "adaptive_proposals",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "review_id",
        sa.Integer,
        sa.ForeignKey("adaptive_reviews.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("goal_id", sa.Integer, sa.ForeignKey("goals.id"), nullable=False),
    sa.Column(
        "current_target_plan_id", sa.Integer, sa.ForeignKey("target_plans.id"), nullable=False
    ),
    sa.Column("proposed_delta_kcal", sa.Integer, nullable=False),
    sa.Column("energy_kcal", sa.Integer, nullable=False),
    sa.Column("protein_grams", sa.Integer, nullable=False),
    sa.Column("fat_grams", sa.Integer, nullable=False),
    sa.Column("carbohydrate_grams", sa.Integer, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("resolved_action_key", sa.String, sa.ForeignKey("actions.key")),
    sa.Column("applied_target_plan_id", sa.Integer, sa.ForeignKey("target_plans.id")),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("resolved_at", sa.Float),
    sa.CheckConstraint("state IN ('pending','applied','kept')", name="adaptive_proposal_state"),
    sa.CheckConstraint(
        "proposed_delta_kcal IN (-150,-100,-50,50,100,150)", name="adaptive_proposal_delta"
    ),
    sa.CheckConstraint(
        "energy_kcal BETWEEN 1 AND 10000 AND protein_grams BETWEEN 20 AND 500 "
        "AND fat_grams BETWEEN 20 AND 300 AND carbohydrate_grams BETWEEN 0 AND 1200",
        name="adaptive_proposal_targets",
    ),
    sqlite_autoincrement=True,
)
