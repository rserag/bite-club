import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

plan_proposals = sa.Table(
    "supplement_plan_proposals",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), unique=True, nullable=False),
    sa.Column("plan", sa.JSON, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sqlite_autoincrement=True,
)
plan_resolutions = sa.Table(
    "supplement_plan_resolutions",
    metadata,
    sa.Column(
        "proposal_id", sa.Integer, sa.ForeignKey("supplement_plan_proposals.id"), primary_key=True
    ),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), unique=True, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("regimen_id", sa.Integer, sa.ForeignKey("supplement_regimens.id")),
    sa.CheckConstraint("state IN ('approved','cancelled')"),
)
plan_dose_marks = sa.Table(
    "supplement_plan_dose_marks",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), unique=True, nullable=False),
    sa.Column("regimen_id", sa.Integer, sa.ForeignKey("supplement_regimens.id"), nullable=False),
    sa.Column(
        "revision_id", sa.Integer, sa.ForeignKey("supplement_regimen_revisions.id"), nullable=False
    ),
    sa.Column("day", sa.Date, nullable=False),
    sa.Column("slot", sa.Integer, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("intake_id", sa.Integer, sa.ForeignKey("supplement_intakes.id")),
    sa.CheckConstraint("status IN ('taken','skipped','unconfirmed')"),
    sa.CheckConstraint("slot BETWEEN 1 AND 24"),
    sa.CheckConstraint(
        "(status = 'taken' AND intake_id IS NOT NULL) OR (status != 'taken' AND intake_id IS NULL)"
    ),
    sqlite_autoincrement=True,
)
