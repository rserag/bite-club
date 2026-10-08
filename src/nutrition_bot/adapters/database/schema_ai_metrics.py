"""Private, content-free AI efficiency measurements and draft links."""

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

ai_metrics = sa.Table(
    "ai_metrics",
    metadata,
    sa.Column("request_key", sa.String, primary_key=True),
    sa.Column("role", sa.String, nullable=False),
    sa.Column("source", sa.String, nullable=False),
    sa.Column("handling", sa.String, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("model", sa.String),
    sa.Column("prompt_version", sa.String),
    sa.Column("schema_version", sa.String),
    sa.Column("reasoning_effort", sa.String),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("completed_at", sa.Float),
    sa.Column("elapsed_ms", sa.BigInteger),
    sa.Column("auth_ms", sa.BigInteger),
    sa.Column("model_catalog_ms", sa.BigInteger),
    sa.Column("inference_ms", sa.BigInteger),
    sa.Column("input_tokens", sa.BigInteger),
    sa.Column("output_tokens", sa.BigInteger),
    sa.Column("reasoning_tokens", sa.BigInteger),
    sa.Column("cached_tokens", sa.BigInteger),
    sa.Column("charged_micro_usd", sa.BigInteger),
    sa.Column("inference_sent", sa.Boolean, nullable=False),
    sa.Column("attempts_sent", sa.Integer, nullable=False),
    sa.Column("failure_category", sa.String),
    sa.Column("draft_id", sa.Integer, sa.ForeignKey("meal_drafts.id", ondelete="SET NULL")),
    sa.Column("initial_revision", sa.Integer),
    sa.CheckConstraint("role IN ('meal_text','meal_photo')", name="ai_metric_role"),
    sa.CheckConstraint("source IN ('normal','evaluation')", name="ai_metric_source"),
    sa.CheckConstraint("handling IN ('local','ai')", name="ai_metric_handling"),
    sa.CheckConstraint(
        "status IN ('handled','ready','clarify','disabled','budget','quota',"
        "'unavailable','unknown')",
        name="ai_metric_status",
    ),
    sa.CheckConstraint("inference_sent IN (0,1)", name="ai_metric_inference_sent"),
    sa.CheckConstraint(
        "typeof(attempts_sent) = 'integer' AND attempts_sent BETWEEN 0 AND 2 "
        "AND (inference_sent = 0) = (attempts_sent = 0)",
        name="ai_metric_attempts_sent",
    ),
    sa.CheckConstraint(
        "failure_category IS NULL OR failure_category IN "
        "('auth','policy','catalog','network','timeout','http','usage_limit','incomplete',"
        "'refusal','oversized','schema','usage','pricing','interrupted','validation','download')",
        name="ai_metric_failure_category",
    ),
    sa.CheckConstraint(
        "initial_revision IS NULL OR initial_revision > 0", name="ai_metric_revision"
    ),
    sa.CheckConstraint(
        "draft_id IS NULL OR initial_revision IS NOT NULL", name="ai_metric_draft_revision"
    ),
    *(
        sa.CheckConstraint(
            f"{field} IS NULL OR (typeof({field}) = 'integer' AND {field} >= 0)",
            name=f"ai_metric_{field}",
        )
        for field in (
            "elapsed_ms",
            "auth_ms",
            "model_catalog_ms",
            "inference_ms",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "charged_micro_usd",
        )
    ),
    sa.Index("ix_ai_metrics_period", "created_at", "source"),
)
