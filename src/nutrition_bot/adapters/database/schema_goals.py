import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

goal_proposals = sa.Table(
    "goal_proposals",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("method", sa.String, nullable=False),
    sa.Column("mode", sa.String, nullable=False),
    sa.Column("target_rate_grams_per_week", sa.Integer, nullable=False),
    sa.Column("reference_weight_grams", sa.Integer, nullable=False),
    sa.Column("inputs", sa.JSON, nullable=False),
    sa.Column("estimated_tdee_kcal", sa.Integer),
    sa.Column("energy_kcal", sa.Integer, nullable=False),
    sa.Column("protein_grams", sa.Integer, nullable=False),
    sa.Column("fat_grams", sa.Integer, nullable=False),
    sa.Column("carbohydrate_grams", sa.Integer, nullable=False),
    sa.Column("energy_range_low_kcal", sa.Integer),
    sa.Column("energy_range_high_kcal", sa.Integer),
    sa.Column("calculation_version", sa.String, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("resolved_action_key", sa.String, sa.ForeignKey("actions.key")),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("resolved_at", sa.Float),
    sa.CheckConstraint("method IN ('estimate','manual')", name="goal_proposal_method"),
    sa.CheckConstraint("mode IN ('loss','maintenance','gain')", name="goal_proposal_mode"),
    sa.CheckConstraint("state IN ('open','applied','cancelled')", name="goal_proposal_state"),
    sa.CheckConstraint(
        "reference_weight_grams BETWEEN 30000 AND 300000", name="goal_proposal_weight"
    ),
    sa.CheckConstraint(
        "energy_kcal BETWEEN 1 AND 10000 AND protein_grams BETWEEN 20 AND 500 "
        "AND fat_grams BETWEEN 20 AND 300 AND carbohydrate_grams BETWEEN 0 AND 1200",
        name="goal_proposal_targets",
    ),
    sqlite_autoincrement=True,
)

goals = sa.Table(
    "goals",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("mode", sa.String, nullable=False),
    sa.Column("target_rate_grams_per_week", sa.Integer, nullable=False),
    sa.Column("started_on", sa.Date, nullable=False),
    sa.Column("ended_on", sa.Date),
    sa.Column("active_slot", sa.Integer, unique=True),
    sa.Column(
        "proposal_id", sa.Integer, sa.ForeignKey("goal_proposals.id"), nullable=False, unique=True
    ),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint("mode IN ('loss','maintenance','gain')", name="goal_mode"),
    sa.CheckConstraint("ended_on IS NULL OR ended_on >= started_on", name="goal_dates"),
    sa.CheckConstraint(
        "(ended_on IS NULL AND active_slot = 1) OR (ended_on IS NOT NULL AND active_slot IS NULL)",
        name="goal_active_slot",
    ),
    sqlite_autoincrement=True,
)

target_plans = sa.Table(
    "target_plans",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("goal_id", sa.Integer, sa.ForeignKey("goals.id"), nullable=False),
    sa.Column("effective_from", sa.Date, nullable=False),
    sa.Column("energy_kcal", sa.Integer, nullable=False),
    sa.Column("protein_grams", sa.Integer, nullable=False),
    sa.Column("fat_grams", sa.Integer, nullable=False),
    sa.Column("carbohydrate_grams", sa.Integer, nullable=False),
    sa.Column("energy_range_low_kcal", sa.Integer),
    sa.Column("energy_range_high_kcal", sa.Integer),
    sa.Column("reference_weight_grams", sa.Integer, nullable=False),
    sa.Column("estimated_tdee_kcal", sa.Integer),
    sa.Column("source", sa.String, nullable=False),
    sa.Column("calculation_version", sa.String, nullable=False),
    sa.Column("proposal_id", sa.Integer, sa.ForeignKey("goal_proposals.id"), unique=True),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint(
        "source IN ('initial_estimate','manual','adaptive')", name="target_plan_source"
    ),
    sa.CheckConstraint(
        "energy_kcal BETWEEN 1 AND 10000 AND protein_grams BETWEEN 20 AND 500 "
        "AND fat_grams BETWEEN 20 AND 300 AND carbohydrate_grams BETWEEN 0 AND 1200",
        name="target_plan_targets",
    ),
    sa.CheckConstraint(
        "(energy_range_low_kcal IS NULL AND energy_range_high_kcal IS NULL) OR "
        "(energy_range_low_kcal IS NOT NULL AND energy_range_high_kcal IS NOT NULL "
        "AND energy_range_low_kcal BETWEEN 1 AND energy_kcal "
        "AND energy_range_high_kcal BETWEEN energy_kcal AND 10000)",
        name="target_plan_energy_range",
    ),
    sa.Index("ix_target_plans_effective", "effective_from", "id"),
    sqlite_autoincrement=True,
)
