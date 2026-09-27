import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from aiogram.types import Update
from alembic import command
from alembic.config import Config

from nutrition_bot.adapters.database.schema_training import (
    gym_exercise_occurrences,
    gym_workout_sets,
    training_session_revisions,
    training_sessions,
)
from tests.helpers import callback, message
from tests.test_telegram_meals import process

MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


def training_press(receipt, operation, *, update_id=2, callback_id=None):
    value = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=callback_id or f"synthetic-training-{update_id}",
        message_id=receipt["telegram_message_id"],
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"training:{operation}:{receipt['button_token']}"
    return Update.model_validate(value)


async def test_quick_gym_preserves_reported_facts_and_labels_default_effort(service, store):
    saved = await process(service, store, message(1, "gym chest and triceps for 40 mins"))
    assert saved["payload"]["training_session_id"] == 1
    text = saved["payload"]["text"]
    assert "Gym" in text and "40 min · chest and triceps" in text
    assert "Session RPE: 6/10 · estimated from the visible default" in text
    assert "Approximate session load: 240 AU" in text
    assert "No exercises, sets or BJJ rounds were inferred" in text
    assert {button["text"] for button in saved["payload"]["buttons"]} == {
        "Edit",
        "Delete",
        "Undo",
    }
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(training_session_revisions))).mappings().one()
    assert row["focus"] == "chest and triceps"
    assert row["rpe_source"] == "system_default"


async def test_bjj_intensity_maps_effort_without_inventing_rounds(service, store):
    saved = await process(service, store, message(1, "BJJ 75 min medium intensity"))
    assert "75 min · medium" in saved["payload"]["text"]
    assert "Session RPE: 5/10 · estimated from medium intensity" in saved["payload"]["text"]
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(training_session_revisions))).mappings().one()
    assert row["kind"] == "bjj"
    assert row["focus"] is None
    assert row["intensity"] == "medium"
    assert row["rpe_source"] == "intensity_map"


async def test_reported_effort_and_comparable_history_are_distinct(service, store):
    for update_id, rpe in enumerate(("6", "7", "8"), 1):
        await process(
            service,
            store,
            message(update_id, f"gym chest and triceps 40 min rpe {rpe}"),
        )
    inferred = await process(service, store, message(4, "gym chest and triceps 50 min"))
    assert (
        "Session RPE: 7/10 · estimated from at least 3 comparable reported sessions"
        in inferred["payload"]["text"]
    )
    async with store.engine.connect() as connection:
        sources = (
            (
                await connection.execute(
                    sa.select(training_session_revisions.c.rpe_source).order_by(
                        training_session_revisions.c.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sources == ["reported", "reported", "reported", "personal_history"]


async def test_edit_delete_undo_append_revisions_and_stale_button_is_safe(service, store):
    saved = await process(service, store, message(1, "gym legs 60 min rpe 7"))
    edited = await process(
        service,
        store,
        message(2, "/gym edit T1r1 legs and core 50 min rpe 6.5"),
    )
    assert "T1r2" in edited["payload"]["text"]
    stale = await process(service, store, training_press(saved, "delete", update_id=3))
    assert "older receipt" in stale["payload"]["text"]
    deleted = await process(service, store, training_press(edited, "delete", update_id=4))
    assert "Deleted from current training history" in deleted["payload"]["text"]
    restored = await process(service, store, training_press(deleted, "undo", update_id=5))
    assert "T1r4" in restored["payload"]["text"]
    assert "50 min · legs and core" in restored["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(training_sessions)) == 1
        )
        rows = (
            (
                await connection.execute(
                    sa.select(training_session_revisions).order_by(training_session_revisions.c.id)
                )
            )
            .mappings()
            .all()
        )
        assert [row["operation"] for row in rows] == ["create", "edit", "delete", "undo"]
        try:
            await connection.execute(
                sa.update(training_session_revisions).values(duration_minutes=1)
            )
        except sa.exc.IntegrityError:
            pass
        else:
            raise AssertionError("training revision update was not rejected")


async def test_invalid_training_log_saves_nothing(service, store):
    rejected = await process(service, store, message(1, "gym chest and triceps"))
    assert "Include duration" in rejected["payload"]["text"]
    assert "No training session changed" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(training_sessions)) == 0
        )


async def test_optional_gym_details_revise_one_session_and_preserve_partial_fields(service, store):
    await process(service, store, message(1, "gym push 60 min rpe 7"))
    detailed = await process(
        service,
        store,
        message(
            2,
            "/gym details T1r1 bench press: 10x60kg @8 warmup, 8x60kg @9 working; "
            "pull-ups: 8, @7, bodyweight",
        ),
    )
    text = detailed["payload"]["text"]
    assert "Updated gym details T1r2" in text
    assert "set RPE is separate from session RPE" in text
    assert "10×60 kg · set RPE 8 · warmup" in text
    assert "8 reps, set RPE 7, bodyweight" in text
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(training_sessions)) == 1
        )
        revisions = (
            (
                await connection.execute(
                    sa.select(training_session_revisions).order_by(training_session_revisions.c.id)
                )
            )
            .mappings()
            .all()
        )
        exercises = (
            (
                await connection.execute(
                    sa.select(gym_exercise_occurrences).order_by(gym_exercise_occurrences.c.id)
                )
            )
            .mappings()
            .all()
        )
        sets = (
            (await connection.execute(sa.select(gym_workout_sets).order_by(gym_workout_sets.c.id)))
            .mappings()
            .all()
        )
    assert [row["operation"] for row in revisions] == ["create", "edit"]
    assert {row["training_revision_id"] for row in exercises} == {revisions[1]["id"]}
    assert [row["name"] for row in exercises] == ["bench press", "pull-ups"]
    assert len(sets) == 5
    assert sets[0]["reps"] == 10 and sets[0]["load_grams"] == 60_000
    assert sets[0]["set_rpe_tenths"] == 80 and sets[0]["set_type"] == "warmup"
    assert sets[2]["reps"] == 8 and sets[2]["load_convention"] is None
    assert sets[3]["reps"] is None and sets[3]["set_rpe_tenths"] == 70
    assert sets[4]["load_convention"] == "bodyweight" and sets[4]["reps"] is None


async def test_summary_edit_and_delete_undo_copy_current_detail_snapshot(service, store):
    await process(service, store, message(1, "gym legs 60 min rpe 7"))
    await process(
        service,
        store,
        message(2, "/gym details T1r1 squat: 5x100kg @8 working"),
    )
    edited = await process(
        service,
        store,
        message(3, "/gym edit T1r2 legs 55 min rpe 7"),
    )
    assert "squat: 5×100 kg" in edited["payload"]["text"]
    deleted = await process(service, store, training_press(edited, "delete", update_id=4))
    restored = await process(service, store, training_press(deleted, "undo", update_id=5))
    assert "squat: 5×100 kg" in restored["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(training_sessions)) == 1
        )
        assert (
            await connection.scalar(
                sa.select(sa.func.count()).select_from(gym_exercise_occurrences)
            )
            == 4
        )


async def test_details_replace_or_clear_snapshot_without_mutating_history(service, store):
    await process(service, store, message(1, "gym back 45 min"))
    await process(service, store, message(2, "/gym details T1r1 row: 10x50kg"))
    replaced = await process(
        service,
        store,
        message(3, "/gym details T1r2 pull-down: 12 reps @7"),
    )
    assert "pull-down" in replaced["payload"]["text"] and "row:" not in replaced["payload"]["text"]
    cleared = await process(service, store, message(4, "/gym details T1r3 clear"))
    assert "No exercises, sets or BJJ rounds were inferred" in cleared["payload"]["text"]
    async with store.engine.connect() as connection:
        names = (
            (
                await connection.execute(
                    sa.select(gym_exercise_occurrences.c.name).order_by(
                        gym_exercise_occurrences.c.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert names == ["row", "pull-down"]


async def test_bjj_or_malformed_detail_request_is_all_or_nothing(service, store):
    await process(service, store, message(1, "BJJ 60 min medium"))
    bjj = await process(service, store, message(2, "/gym details T1r1 squat: 5x100kg"))
    assert "current, non-deleted gym session" in bjj["payload"]["text"]
    await process(service, store, message(3, "gym legs 60 min"))
    malformed = await process(
        service,
        store,
        message(4, "/gym details T2r1 squat: 5x100kg; bench: nonsense"),
    )
    assert "Use sets like" in malformed["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(sa.func.count()).select_from(gym_exercise_occurrences)
            )
            == 0
        )


async def test_populated_0015_upgrade_preserves_quick_session_and_pointer(service, store, settings):
    await process(service, store, message(1, "gym legs 60 min rpe 7"))
    await store.close()
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    await asyncio.to_thread(command.downgrade, config, "0015_training_sessions")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        before = connection.execute(
            "SELECT id,current_revision_id,source_chat_id,source_message_id FROM training_sessions"
        ).fetchall()
        revisions = connection.execute(
            "SELECT id,training_session_id,revision_number,operation "
            "FROM training_session_revisions"
        ).fetchall()
    await asyncio.to_thread(command.upgrade, config, "head")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert (
            connection.execute(
                "SELECT id,current_revision_id,source_chat_id,source_message_id "
                "FROM training_sessions"
            ).fetchall()
            == before
        )
        assert (
            connection.execute(
                "SELECT id,training_session_id,revision_number,operation "
                "FROM training_session_revisions"
            ).fetchall()
            == revisions
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_downgrade_refuses_to_discard_gym_detail_history(service, store, settings):
    await process(service, store, message(1, "gym legs 60 min"))
    await process(service, store, message(2, "/gym details T1r1 squat: 5x100kg"))
    await store.close()
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    with pytest.raises(RuntimeError, match="Gym detail history exists"):
        await asyncio.to_thread(command.downgrade, config, "0015_training_sessions")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0016_gym_details",
        )
        assert connection.execute("SELECT count(*) FROM gym_workout_sets").fetchone() == (1,)
