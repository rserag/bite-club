"""Synthetic checks for the deployment authorization and data-preservation boundary."""

import importlib.util
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "deploy/bite-club-deploy"


@pytest.fixture
def dbcheck() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deployment_dbcheck", ROOT / "deploy/dbcheck.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            "CREATE TABLE alembic_version (version_num TEXT);"
            "INSERT INTO alembic_version VALUES ('synthetic_revision');"
            "CREATE TABLE runtime_heartbeat (touched_at REAL);"
            "INSERT INTO runtime_heartbeat VALUES (1);"
            "CREATE TABLE meals (id INTEGER PRIMARY KEY, nutrient INTEGER);"
            "INSERT INTO meals VALUES (1, NULL), (2, 20);"
        )
    return path


def test_fingerprints_detect_edits_and_deletions(dbcheck: ModuleType, database: Path) -> None:
    baseline = dbcheck.inspect(database)
    assert baseline["tables"]["meals"]["count"] == 2
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE meals SET nutrient = 0 WHERE nutrient IS NULL")
    with pytest.raises(ValueError, match="content changed"):
        dbcheck.verify_preserved(dbcheck.inspect(database), baseline)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("DELETE FROM meals")
    with pytest.raises(ValueError, match="content changed"):
        dbcheck.verify_preserved(dbcheck.inspect(database), baseline)


def test_migration_can_add_tables_without_rewriting_history(
    dbcheck: ModuleType, database: Path
) -> None:
    baseline = dbcheck.inspect(database)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE alembic_version SET version_num = 'next_synthetic_revision'")
        db.execute("UPDATE runtime_heartbeat SET touched_at = 2")
        db.execute("CREATE TABLE new_feature (id INTEGER)")
    dbcheck.verify_preserved(dbcheck.inspect(database), baseline)


def test_broken_foreign_keys_prevent_deployment(dbcheck: ModuleType, database: Path) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript(
            "CREATE TABLE portions (meal_id INTEGER REFERENCES meals(id));"
            "INSERT INTO portions VALUES (999);"
        )
    with pytest.raises(ValueError, match="foreign keys"):
        dbcheck.inspect(database)


def shell(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; shift; ' + code, "test", str(CONTROLLER), *args],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "revision,image",
    [
        ("main", "ghcr.io/rserag/bite-club@sha256:" + "0" * 64),
        ("0" * 40, "ghcr.io/rserag/bite-club:latest"),
        ("0" * 40, "ghcr.io/example/unrelated@sha256:" + "0" * 64),
        ("0" * 40 + ";id", "ghcr.io/rserag/bite-club@sha256:" + "0" * 64),
    ],
)
def test_controller_rejects_untrusted_refs(revision: str, image: str) -> None:
    assert shell('validate_revision "$1"; validate_image "$2"', revision, image).returncode != 0


def test_controller_accepts_exact_allowlisted_ref() -> None:
    assert (
        shell(
            'validate_revision "$1"; validate_image "$2"',
            "0" * 40,
            "ghcr.io/rserag/bite-club@sha256:" + "0" * 64,
        ).returncode
        == 0
    )


def test_isolated_command_keeps_image_before_command() -> None:
    result = shell(
        'docker() { printf "%s\\n" "$@"; }; '
        'isolated synthetic-volume synthetic-image --entrypoint python -- -c "print(1)"'
    )
    assert result.returncode == 0
    args = result.stdout.splitlines()
    assert args[-3:] == ["synthetic-image", "-c", "print(1)"]
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--cpus") + 1] == ".5"


@pytest.mark.parametrize("suffix", ["\nOTHER=value\n", "\nBOT_IMAGE=untrusted\n"])
def test_release_record_cannot_inject_extra_environment(tmp_path: Path, suffix: str) -> None:
    record = tmp_path / "release.env"
    record.write_text(
        "DEPLOYED_REVISION="
        + "0" * 40
        + "\nBOT_IMAGE=ghcr.io/rserag/bite-club@sha256:"
        + "0" * 64
        + suffix
    )
    assert shell('read_release "$1"', str(record)).returncode != 0


def test_check_cli_outputs_only_fingerprints(database: Path) -> None:
    result = subprocess.run(
        ["python3", str(ROOT / "deploy/dbcheck.py"), str(database)],
        text=True,
        capture_output=True,
        check=True,
    )
    evidence = json.loads(result.stdout)
    assert evidence["schema"] == "synthetic_revision"
    assert set(evidence["tables"]["meals"]) == {"count", "sha256"}
