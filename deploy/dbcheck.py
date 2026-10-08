"""Check isolated SQLite state; print only schema and content fingerprints."""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def inspect(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Database foreign keys failed")
        schema = db.execute("SELECT version_num FROM alembic_version").fetchone()
        if schema is None:
            raise ValueError("Database schema is missing")
        tables = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if name in {"alembic_version", "runtime_heartbeat"}:
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            # Sorted row digests avoid relying on storage order or printing personal data.
            rows = sorted(
                hashlib.sha256(repr(row).encode()).hexdigest()
                for row in db.execute(f"SELECT * FROM {quoted}")
            )
            tables[name] = {
                "count": len(rows),
                "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest(),
            }
    return {"schema": schema[0], "tables": tables}


def verify_preserved(current: dict[str, object], baseline: dict[str, object]) -> None:
    before = baseline["tables"]
    after = current["tables"]
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError("Invalid verification manifest")
    if any(after.get(name) != evidence for name, evidence in before.items()):
        raise ValueError("Existing database content changed; operator review required")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    result = inspect(args.database)
    if args.baseline:
        verify_preserved(result, json.loads(args.baseline.read_text()))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
