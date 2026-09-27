"""Private, offline SQLite snapshots and verification; never starts a worker."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import closing, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nutrition_bot.adapters.database.schema import SCHEMA_REVISION
from nutrition_bot.runtime.lock import database_lock


class FileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    format: Literal[1] = 1
    created_at: str
    schema_revision: str
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    files: dict[str, FileRecord]
    table_counts: dict[str, int]


def fingerprint(path: Path) -> FileRecord:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Only regular backup files are supported")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return FileRecord(size=path.stat().st_size, sha256=digest)


def inspect_database(path: Path) -> tuple[str, dict[str, int]]:
    # Snapshot files have no WAL. immutable avoids creating journals or touching a restore.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?immutable=1", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Snapshot integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Snapshot foreign key check failed")
        revisions = db.execute("SELECT version_num FROM alembic_version").fetchall()
        if revisions != [(SCHEMA_REVISION,)]:
            raise ValueError("Use the release matching the snapshot schema")
        counts = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            quoted = name.replace('"', '""')
            counts[name] = db.execute(f'SELECT count(*) FROM "{quoted}"').fetchone()[0]
        return revisions[0][0], counts


def snapshot(
    database: Path,
    output: Path,
    image_digest: str,
    *,
    media_root: Path | None = None,
    timeout_seconds: float = 60,
) -> Manifest:
    """Copy committed WAL data; media copies require an exclusively stopped worker.

    output must be new. A manifest is written last; incomplete output is never valid.
    The caller owns the containing directory and must keep it private.
    """
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest):
        raise ValueError("An immutable image digest is required")
    if timeout_seconds <= 0:
        raise ValueError("Backup timeout must be positive")
    if database.is_symlink() or not database.is_file():
        raise ValueError("Source database must exist and be a regular file")
    output = output.absolute()
    if media_root is not None:
        if media_root.is_symlink() or not media_root.is_dir():
            raise ValueError("Media directory must exist")
        if output.resolve().is_relative_to(media_root.resolve()):
            raise ValueError("Snapshot output cannot be inside media")
    output.mkdir(mode=0o700)
    try:
        lock = database_lock(database) if media_root is not None else nullcontext()
        with lock:
            started = time.monotonic()

            def progress(status: int, remaining: int, total: int) -> None:
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError("SQLite snapshot deadline exceeded")

            target = output / "app.sqlite3"
            target.touch(mode=0o600, exist_ok=False)
            with (
                closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as src,
                closing(sqlite3.connect(target)) as dest,
            ):
                src.backup(dest, pages=256, progress=progress, sleep=0.05)
                dest.execute("PRAGMA journal_mode=DELETE")
            if media_root is not None:
                # Explicit media-only root: no recursive copying of the runtime directory.
                (output / "media").mkdir(mode=0o700)
                for source in sorted(media_root.rglob("*")):
                    if source.is_symlink():
                        raise ValueError("Media links are not supported")
                    destination = output / "media" / source.relative_to(media_root)
                    if source.is_dir():
                        destination.mkdir(mode=0o700)
                    elif source.is_file():
                        destination.touch(mode=0o600, exist_ok=False)
                        shutil.copyfile(source, destination)
                    else:
                        raise ValueError("Media must contain only regular files")
            revision, counts = inspect_database(target)
            files = {
                path.relative_to(output).as_posix(): fingerprint(path)
                for path in sorted(output.rglob("*"))
                if path.is_file()
            }
            manifest = Manifest(
                created_at=datetime.now(UTC).isoformat(),
                schema_revision=revision,
                image_digest=image_digest,
                files=files,
                table_counts=counts,
            )
            manifest_path = output / "manifest.json"
            with manifest_path.open("x") as stream:
                manifest_path.chmod(0o600)
                stream.write(manifest.model_dump_json(indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            verify_snapshot(output)
            return manifest
    except BaseException:
        shutil.rmtree(output)
        raise


def verify_snapshot(directory: Path) -> Manifest:
    """Validate all bytes and counts without mutating the snapshot or sending messages."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Snapshot directory must exist")
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Invalid snapshot manifest")
    manifest = Manifest.model_validate(json.loads(manifest_path.read_text()))
    if "app.sqlite3" not in manifest.files or manifest.schema_revision != SCHEMA_REVISION:
        raise ValueError("Missing database or incompatible schema")
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Unexpected snapshot file type")
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(directory).as_posix())
    if actual != set(manifest.files):
        raise ValueError("Snapshot file set does not match manifest")
    for name, record in manifest.files.items():
        if name != "app.sqlite3" and not name.startswith("media/"):
            raise ValueError("Unexpected snapshot member")
        if fingerprint(directory / name) != record:
            raise ValueError("Snapshot checksum mismatch")
    revision, counts = inspect_database(directory / "app.sqlite3")
    if revision != manifest.schema_revision or counts != manifest.table_counts:
        raise ValueError("Snapshot database metadata mismatch")
    return manifest
