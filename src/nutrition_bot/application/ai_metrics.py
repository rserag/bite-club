"""Best-effort private measurements; reports never expose request or draft identifiers."""

import logging
import math
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import SCHEMA_REVISION
from nutrition_bot.adapters.database.schema_ai_metrics import ai_metrics

_METADATA = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}\Z")
_NUMERIC_FIELDS = (
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
_STATUSES = {"handled", "ready", "clarify", "disabled", "budget", "quota", "unavailable", "unknown"}
_FAILURES = {
    "auth",
    "policy",
    "catalog",
    "network",
    "timeout",
    "http",
    "usage_limit",
    "incomplete",
    "refusal",
    "oversized",
    "schema",
    "usage",
    "pricing",
    "interrupted",
    "validation",
    "download",
}


def _valid_metadata(value: str | None) -> bool:
    return value is None or (_METADATA.fullmatch(value) is not None and "://" not in value)


def _valid_number(value: int | None) -> bool:
    return value is None or (type(value) is int and 0 <= value < 2**63)


async def record_metric(
    connection: AsyncConnection,
    *,
    request_key: str,
    role: str,
    source: str,
    handling: str,
    status: str,
    created_at: float,
    completed_at: float | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    schema_version: str | None = None,
    reasoning_effort: str | None = None,
    elapsed_ms: int | None = None,
    auth_ms: int | None = None,
    model_catalog_ms: int | None = None,
    inference_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cached_tokens: int | None = None,
    charged_micro_usd: int | None = None,
    inference_sent: bool = False,
    attempts_sent: int | None = None,
    failure_category: str | None = None,
    draft_id: int | None = None,
    initial_revision: int | None = None,
) -> bool:
    """Write once inside a SAVEPOINT; instrumentation must not break the enclosing action.

    Callers provide application-selected version labels, never message/provider content.
    A replay preserves the first sent measurement, including unknown provider usage.
    An AI request denied before inference can upgrade once if it is later sent.
    """
    try:
        if attempts_sent is None:
            attempts_sent = int(inference_sent)
        if (
            not 1 <= len(request_key) <= 128
            or role not in {"meal_text", "meal_photo"}
            or source not in {"normal", "evaluation"}
            or handling not in {"local", "ai"}
            or status not in _STATUSES
            or failure_category is not None
            and failure_category not in _FAILURES
            or type(inference_sent) is not bool
            or type(attempts_sent) is not int
            or not 0 <= attempts_sent <= 2
            or inference_sent != (attempts_sent > 0)
            or not math.isfinite(created_at)
            or created_at < 0
            or completed_at is not None
            and not math.isfinite(completed_at)
            or not all(
                _valid_metadata(value)
                for value in (model, prompt_version, schema_version, reasoning_effort)
            )
            or (draft_id is None) != (initial_revision is None)
            or draft_id is not None
            and (type(draft_id) is not int or not 1 <= draft_id < 2**63)
            or initial_revision is not None
            and (type(initial_revision) is not int or not 1 <= initial_revision < 2**63)
        ):
            raise ValueError("Invalid measurement")
        if elapsed_ms is None and completed_at is not None:
            elapsed_ms = max(0, round((completed_at - created_at) * 1000))
        values: dict[str, Any] = {
            "request_key": request_key,
            "role": role,
            "source": source,
            "handling": handling,
            "status": status,
            "created_at": created_at,
            "completed_at": completed_at,
            "model": model,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "reasoning_effort": reasoning_effort,
            "elapsed_ms": elapsed_ms,
            "auth_ms": auth_ms,
            "model_catalog_ms": model_catalog_ms,
            "inference_ms": inference_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cached_tokens": cached_tokens,
            "charged_micro_usd": charged_micro_usd,
            "inference_sent": inference_sent,
            "failure_category": failure_category,
            "attempts_sent": attempts_sent,
            "draft_id": draft_id,
            "initial_revision": initial_revision,
        }
        if not all(_valid_number(values[field]) for field in _NUMERIC_FIELDS):
            raise ValueError("Invalid measurement")
        async with connection.begin_nested():
            statement = insert(ai_metrics).values(**values)
            if handling == "ai" and inference_sent:
                statement = statement.on_conflict_do_update(
                    index_elements=[ai_metrics.c.request_key],
                    set_={
                        field: value for field, value in values.items() if field != "request_key"
                    },
                    where=sa.and_(
                        ai_metrics.c.handling == "ai",
                        ai_metrics.c.inference_sent.is_(False),
                        ai_metrics.c.draft_id.is_(None),
                        ai_metrics.c.source == source,
                    ),
                )
            else:
                statement = statement.on_conflict_do_nothing(
                    index_elements=[ai_metrics.c.request_key]
                )
            await connection.execute(statement)
        return True
    except Exception:
        # Do not log exception details, keys, messages, URLs, account IDs or model output.
        logging.getLogger("nutrition_bot.ai_metrics").warning("ai_metric_unavailable")
        return False


async def link_draft(
    connection: AsyncConnection, request_key: str, draft_id: int, initial_revision: int
) -> bool:
    """Attach the first displayed revision once; later edits cannot reset the baseline."""
    try:
        if not (
            1 <= len(request_key) <= 128
            and type(draft_id) is int
            and 1 <= draft_id < 2**63
            and type(initial_revision) is int
            and 1 <= initial_revision < 2**63
        ):
            raise ValueError("Invalid measurement link")
        async with connection.begin_nested():
            await connection.execute(
                sa.update(ai_metrics)
                .where(
                    ai_metrics.c.request_key == request_key, ai_metrics.c.initial_revision.is_(None)
                )
                .values(draft_id=draft_id, initial_revision=initial_revision)
            )
        return True
    except Exception:
        logging.getLogger("nutrition_bot.ai_metrics").warning("ai_metric_unavailable")
        return False


def _timing(rows: list[sqlite3.Row], field: str) -> dict[str, int | float | None]:
    values = sorted(row[field] for row in rows if row[field] is not None)
    return {
        "known": len(values),
        "unknown": len(rows) - len(values),
        "median_ms": median(values) if values else None,
        # Nearest-rank percentile works for small samples and never interpolates guesses.
        "p95_ms": values[math.ceil(0.95 * len(values)) - 1] if values else None,
    }


def _usage(rows: list[sqlite3.Row], field: str) -> dict[str, int | None]:
    values = [row[field] for row in rows if row[field] is not None]
    return {
        "known": len(values),
        "unknown": len(rows) - len(values),
        "total": sum(values) if values else None,
    }


def _summarize(rows: list[sqlite3.Row]) -> dict[str, Any]:
    ai_rows = [row for row in rows if row["handling"] == "ai"]
    sent = [row for row in ai_rows if row["inference_sent"]]
    linked = [row for row in rows if row["initial_revision"] is not None]
    draft_states = Counter(row["draft_state"] or "removed" for row in linked)
    versions = Counter(
        (row["model"], row["prompt_version"], row["schema_version"], row["reasoning_effort"])
        for row in ai_rows
    )
    return {
        "requests": len(rows),
        "handling": {key: sum(row["handling"] == key for row in rows) for key in ("local", "ai")},
        "roles": dict(sorted(Counter(row["role"] for row in rows).items())),
        "statuses": dict(sorted(Counter(row["status"] for row in rows).items())),
        "failures": dict(
            sorted(
                Counter(
                    row["failure_category"] for row in rows if row["failure_category"] is not None
                ).items()
            )
        ),
        "inference_sent": len(sent),
        "attempts_sent": sum(row["attempts_sent"] for row in rows),
        "versions": [
            {
                "model": version[0],
                "prompt_version": version[1],
                "schema_version": version[2],
                "reasoning_effort": version[3],
                "requests": count,
            }
            for version, count in sorted(
                versions.items(), key=lambda item: tuple(value or "" for value in item[0])
            )
        ],
        "latency": {
            handling: _timing([row for row in rows if row["handling"] == handling], "elapsed_ms")
            for handling in ("local", "ai")
        },
        "ai_stages": {
            field: _timing(ai_rows, field)
            for field in ("auth_ms", "model_catalog_ms", "inference_ms")
        },
        "usage": {
            field: _usage(sent, field)
            for field in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "charged_micro_usd",
            )
        },
        "drafts": {
            "linked": len(linked),
            **{
                state: draft_states[state]
                for state in ("open", "saved", "cancelled", "expired", "removed")
            },
            "saved_unchanged": sum(
                row["draft_state"] == "saved" and row["draft_revision"] == row["initial_revision"]
                for row in linked
            ),
            "saved_edited": sum(
                row["draft_state"] == "saved" and row["draft_revision"] != row["initial_revision"]
                for row in linked
            ),
        },
    }


def metrics_summary(
    database_path: Path, *, days: int = 3, now: float | None = None
) -> dict[str, Any]:
    """Read a bounded rolling UTC window without acquiring the worker/migration lock.

    Current draft states are read in the same snapshot; this is a request cohort,
    not a count of approvals made during the window. No retention/expiry is triggered.
    """
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError("Choose 1–30 days")
    end = time.time() if now is None else now
    if not math.isfinite(end) or end < 0:
        raise ValueError("Invalid report time")
    start = end - days * 86400
    with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        revision = db.execute("SELECT version_num FROM alembic_version").fetchone()
        if revision is None or revision[0] != SCHEMA_REVISION:
            raise ValueError("Run the explicit migration first")
        rows = db.execute(
            "SELECT m.*, d.state AS draft_state, d.revision AS draft_revision "
            "FROM ai_metrics AS m LEFT JOIN meal_drafts AS d ON d.id = m.draft_id "
            "WHERE m.created_at >= ? AND m.created_at <= ? ORDER BY m.created_at",
            (start, end),
        ).fetchall()
        plan_days = db.execute(
            "SELECT day, SUM(CASE WHEN substr(request_key, 1, 5) = 'eval:' THEN 0 ELSE 1 END) "
            "AS normal, SUM(CASE WHEN substr(request_key, 1, 5) = 'eval:' THEN 1 ELSE 0 END) "
            "AS evaluation, COUNT(*) AS total FROM ai_plan_invocations "
            "WHERE day >= ? AND day <= ? GROUP BY day ORDER BY day",
            (
                datetime.fromtimestamp(start, UTC).date().isoformat(),
                datetime.fromtimestamp(end, UTC).date().isoformat(),
            ),
        ).fetchall()
        return {
            "days": days,
            "from_utc": datetime.fromtimestamp(start, UTC).isoformat(),
            "through_utc": datetime.fromtimestamp(end, UTC).isoformat(),
            "scope": "requests_started_in_window_current_draft_states",
            "normal_input_scope": "eligible_original_free_text_and_photo_inputs",
            "subscription_invocations_scope": "full_utc_days_overlapping_window",
            "subscription_invocations": [dict(row) for row in plan_days],
            "sources": {
                source: _summarize([row for row in rows if row["source"] == source])
                for source in ("normal", "evaluation")
            },
        }
