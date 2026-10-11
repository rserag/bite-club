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


def test_local_release_record_remains_readable_but_remote_key_cannot_submit_it(
    tmp_path: Path,
) -> None:
    image = "sha256:" + "1" * 64
    record = tmp_path / "release.env"
    record.write_text("DEPLOYED_REVISION=" + "0" * 40 + "\nBOT_IMAGE=" + image + "\n")
    assert shell('read_release "$1"', str(record)).returncode == 0
    assert shell('validate_image "$1"', image).returncode != 0
    assert shell('validate_local_image "$1"', image).returncode == 0


@pytest.mark.parametrize(
    "image", ["nutrition-bot:manual", "sha256:" + "1" * 63, "sha256:" + "1" * 64 + ";id"]
)
def test_local_release_requires_exact_immutable_image_id(image: str) -> None:
    assert shell('validate_local_image "$1"', image).returncode != 0


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


@pytest.mark.parametrize("rewrite_actual", [False, True])
def test_actual_migration_preservation_is_verified_before_starting_candidate(
    tmp_path: Path, database: Path, rewrite_actual: bool
) -> None:
    """A successful trial cannot hide a differing production migration outcome."""
    for name in ("state", "backups", "releases"):
        (tmp_path / name).mkdir()
    (tmp_path / "runtime.env").write_text("SYNTHETIC_ONLY=true\n")
    (tmp_path / "dbcheck.py").write_bytes((ROOT / "deploy/dbcheck.py").read_bytes())
    (tmp_path / "state/current.env").write_text(
        f"DEPLOYED_REVISION={'1' * 40}\nBOT_IMAGE=sha256:{'1' * 64}\n"
    )
    controller = tmp_path / "isolated-controller"
    controller.write_text(
        CONTROLLER.read_text().replace("readonly root=/opt/bite-club", 'readonly root="$2"')
    )
    # Only deployment orchestration is real. Docker/service/backup IO is replaced
    # with isolated files and events; the actual dbcheck and verifier are used.
    code = (
        r"""
source "$1"; shift
rewrite_actual=$2
production_database=$3
stat() { printf '0:600\n'; }
docker() {
  if [[ $1 == image && $2 == inspect ]]; then
    case $4 in
      *revision*) printf '%s\n' "$wanted_revision" ;;
      *Architecture*) printf 'amd64\n' ;;
      *.Id*) printf '%s\n' "$wanted_image" ;;
    esac
  fi
}
compose() { printf 'compose:%s\n' "$2" >> "$root/events"; }
archive_volume() { cp "$production_database" "$2"; }
restore_volume() {
  printf 'restore:%s\n' "$1" >> "$root/events"
  if [[ $1 == "$volume" ]]; then cp "$3" "$production_database"
  else cp "$3" "$root/trial.sqlite3"; fi
}
check_db() {
  if [[ $1 == "$volume" ]]; then python3 "$root/dbcheck.py" "$production_database"
  else python3 "$root/dbcheck.py" "$root/trial.sqlite3"; fi
}
migrate_volume() {
  local database=$root/trial.sqlite3 rewrite=false
  if [[ $1 == "$volume" ]]; then database=$production_database; rewrite=$rewrite_actual; fi
  python3 - "$database" "$rewrite" <<'PY'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE alembic_version SET version_num='candidate_synthetic'")
    db.execute("CREATE TABLE candidate_addition (id INTEGER)")
    if sys.argv[2] == 'true':
        db.execute("UPDATE meals SET nutrient=0 WHERE nutrient IS NULL")
PY
}
healthy() { printf 'healthy\n' >> "$root/events"; }
deploy_release """
        + "3" * 40
        + " sha256:"
        + "2" * 64
        + " true\n"
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            code,
            "synthetic",
            str(controller),
            str(tmp_path),
            str(rewrite_actual).lower(),
            str(database),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    events = (tmp_path / "events").read_text().splitlines()
    assert "compose:stop" in events
    assert not any(event == "restore:nutrition-bot_bot-data" for event in events)
    runs = list((tmp_path / "backups").glob("cutover.*"))
    assert len(runs) == 1
    assert (runs[0] / "pre-start.json").is_file()
    with closing(sqlite3.connect(database)) as db:
        value = db.execute("SELECT nutrient FROM meals WHERE id=1").fetchone()[0]
    if rewrite_actual:
        assert result.returncode != 0
        assert "Migrated data preservation failed" in result.stderr
        assert "compose:up" not in events and "healthy" not in events
        assert value == 0  # Keep the candidate state for recovery; never discard it.
        assert (
            (tmp_path / "state/current.env").read_text().startswith("DEPLOYED_REVISION=" + "1" * 40)
        )
        with closing(sqlite3.connect(runs[0] / "data.tar.gz")) as backup:
            assert backup.execute("SELECT nutrient FROM meals WHERE id=1").fetchone() == (None,)
    else:
        assert result.returncode == 0, result.stderr
        assert "compose:up" in events and "healthy" in events
        assert value is None
        assert (
            (tmp_path / "state/current.env").read_text().startswith("DEPLOYED_REVISION=" + "3" * 40)
        )
