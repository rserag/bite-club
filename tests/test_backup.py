import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing

import pytest

from nutrition_bot.runtime.backup import fingerprint, snapshot, verify_snapshot
from nutrition_bot.runtime.backup_repository import restore, retention, upload
from nutrition_bot.runtime.lock import database_lock

DIGEST = "sha256:" + "a" * 64


def test_snapshot_includes_committed_wal_and_not_later_writes(migrated, tmp_path):
    output = tmp_path / "snapshot"
    with closing(sqlite3.connect(migrated.database_path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE synthetic_backup_probe (value TEXT)")
        writer.execute("INSERT INTO synthetic_backup_probe VALUES ('synthetic-before')")
        writer.commit()
        assert migrated.database_path.with_suffix(".sqlite3-wal").stat().st_size > 0
        # A live worker lock must not prevent SQLite's online backup API.
        with database_lock(migrated.database_path):
            manifest = snapshot(migrated.database_path, output, DIGEST)
        writer.execute("INSERT INTO synthetic_backup_probe VALUES ('synthetic-after')")
        writer.commit()
        with closing(sqlite3.connect(output / "app.sqlite3")) as restored:
            assert restored.execute("SELECT * FROM synthetic_backup_probe").fetchall() == [
                ("synthetic-before",)
            ]
    before = fingerprint(output / "app.sqlite3")
    assert verify_snapshot(output) == manifest
    assert fingerprint(output / "app.sqlite3") == before
    assert not (output / "app.sqlite3-wal").exists()
    assert (output.stat().st_mode & 0o777) == 0o700
    assert ((output / "app.sqlite3").stat().st_mode & 0o777) == 0o600
    assert ((output / "manifest.json").stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("damage", ["missing", "changed", "extra", "counts", "schema", "link"])
def test_reject_damaged_snapshot(migrated, tmp_path, damage):
    output = tmp_path / "snapshot"
    snapshot(migrated.database_path, output, DIGEST)
    if damage == "missing":
        (output / "app.sqlite3").unlink()
    elif damage == "changed":
        with (output / "app.sqlite3").open("ab") as stream:
            stream.write(b"corruption")
    elif damage == "extra":
        (output / "unexpected").write_text("synthetic")
    elif damage == "link":
        (output / "link").symlink_to(migrated.database_path)
    else:
        manifest = json.loads((output / "manifest.json").read_text())
        if damage == "counts":
            manifest["table_counts"]["alembic_version"] = 99
        else:
            manifest["schema_revision"] = "unknown"
        (output / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_snapshot(output)


def test_output_is_never_overwritten_and_failed_copy_is_removed(migrated, tmp_path, monkeypatch):
    output = tmp_path / "snapshot"
    output.mkdir()
    (output / "keep").write_text("synthetic")
    with pytest.raises(FileExistsError):
        snapshot(migrated.database_path, output, DIGEST)
    assert (output / "keep").read_text() == "synthetic"

    def fail(*args, **kwargs):
        raise OSError("synthetic disk full")

    monkeypatch.setattr("nutrition_bot.runtime.backup.inspect_database", fail)
    with pytest.raises(OSError):
        snapshot(migrated.database_path, tmp_path / "failed", DIGEST)
    assert not (tmp_path / "failed").exists()
    assert migrated.database_path.is_file()


def test_media_requires_stopped_worker_and_retains_exact_files(migrated, tmp_path):
    media = tmp_path / "media"
    (media / "nested").mkdir(parents=True)
    (media / "nested" / "synthetic.txt").write_text("synthetic media")
    output = tmp_path / "snapshot"
    with database_lock(migrated.database_path), pytest.raises(RuntimeError):
        snapshot(migrated.database_path, output, DIGEST, media_root=media)
    assert not output.exists()
    snapshot(migrated.database_path, output, DIGEST, media_root=media)
    assert (output / "media/nested/synthetic.txt").read_text() == "synthetic media"
    (output / "media/nested/synthetic.txt").unlink()
    with pytest.raises(ValueError):
        verify_snapshot(output)


def test_media_symlinks_fail_closed(migrated, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "outside").symlink_to(migrated.database_path)
    with pytest.raises(ValueError):
        snapshot(migrated.database_path, tmp_path / "snapshot", DIGEST, media_root=media)


def test_foreign_key_violations_rejected(migrated, tmp_path):
    with closing(sqlite3.connect(migrated.database_path)) as db:
        db.executescript("""
            CREATE TABLE synthetic_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE synthetic_child (id INTEGER REFERENCES synthetic_parent(id));
            INSERT INTO synthetic_child VALUES (123);
        """)
    with pytest.raises(ValueError, match="foreign key"):
        snapshot(migrated.database_path, tmp_path / "snapshot", DIGEST)


def test_restic_failure_does_not_claim_success_or_leak_output(migrated, tmp_path, monkeypatch):
    directory = tmp_path / "snapshot"
    snapshot(migrated.database_path, directory, DIGEST)
    monkeypatch.setenv("RESTIC_REPOSITORY", str(tmp_path / "repo"))
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(tmp_path / "password"))
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 3, "", "private-error")
    )
    with pytest.raises(RuntimeError, match="^Encrypted repository operation failed$"):
        upload(directory)


def test_retention_is_scoped_and_dry_run_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "nutrition_bot.runtime.backup_repository.restic", lambda args: calls.append(args) or "[]"
    )
    retention()
    retention(apply=True)
    assert "--dry-run" in calls[0] and "--prune" not in calls[0]
    assert "--dry-run" not in calls[1]
    assert calls[2] == ["prune"]
    assert calls[0][1:7] == [
        "--host",
        "bite-club-v1",
        "--tag",
        "bite-club-v1",
        "--group-by",
        "host,tags",
    ]
    assert calls[0][7:13] == ["--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6"]


@pytest.mark.skipif(shutil.which("restic") is None, reason="operator restic not installed")
def test_encrypted_repository_round_trip(migrated, tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("RESTIC_"):
            monkeypatch.delenv(key)
    password = tmp_path / "password"
    password.write_text("synthetic-offline-test-password")
    password.chmod(0o600)
    monkeypatch.setenv("RESTIC_REPOSITORY", str(tmp_path / "encrypted"))
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(password))
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "cache"))
    # No network backend, production credentials, or Telegram client.
    subprocess.run(["restic", "init"], check=True, capture_output=True, env=os.environ)
    source = tmp_path / "snapshot"
    original = snapshot(migrated.database_path, source, DIGEST)
    identifier = upload(source)
    destination = tmp_path / "restore"
    restore(identifier, destination)
    assert verify_snapshot(destination) == original
    assert retention() == []
    assert retention(apply=True) == []
    with pytest.raises(FileExistsError):
        restore(identifier, destination)
    password.write_text("wrong-synthetic-password")
    with pytest.raises(RuntimeError):
        restore(identifier, tmp_path / "wrong-password")
