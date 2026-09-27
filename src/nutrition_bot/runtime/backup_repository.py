"""Operator-side restic commands. Credentials and restic stay outside the bot image."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from nutrition_bot.runtime.backup import verify_snapshot

TAG = "bite-club-v1"


def restic(arguments: list[str], *, cwd: Path | None = None) -> str:
    if not os.environ.get("RESTIC_REPOSITORY") or not os.environ.get("RESTIC_PASSWORD_FILE"):
        raise ValueError("Set operator RESTIC_REPOSITORY and RESTIC_PASSWORD_FILE")
    result = subprocess.run(
        ["restic", "--json", *arguments],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    # Backend error text can include credentials or private paths. Never relay it.
    if result.returncode != 0:
        raise RuntimeError("Encrypted repository operation failed")
    return result.stdout


def upload(directory: Path) -> str:
    verify_snapshot(directory)
    output = restic(["backup", "--host", TAG, "--tag", TAG, "."], cwd=directory.resolve())
    summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
    snapshots = [
        row.get("snapshot_id") for row in summaries if row.get("message_type") == "summary"
    ]
    if len(snapshots) != 1 or not isinstance(snapshots[0], str):
        raise RuntimeError("No unique encrypted snapshot receipt")
    identifier = snapshots[0]
    if not re.fullmatch(r"[a-f0-9]{64}", identifier):
        raise RuntimeError("Invalid encrypted snapshot receipt")
    return identifier


def restore(identifier: str, output: Path) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", identifier):
        raise ValueError("Use an exact full snapshot ID")
    # Reserve a new destination. Never overwrite an existing database or restore.
    output.mkdir(mode=0o700)
    restic(["restore", identifier, "--target", str(output.resolve()), "--verify"])
    verify_snapshot(output)


def retention(*, apply: bool = False) -> list[str]:
    arguments = [
        "forget",
        "--host",
        TAG,
        "--tag",
        TAG,
        "--group-by",
        "host,tags",
        "--keep-daily",
        "7",
        "--keep-weekly",
        "4",
        "--keep-monthly",
        "6",
    ]
    # Require an explicit action for removal; never tie retention to upload success.
    if not apply:
        arguments += ["--dry-run"]
    groups = json.loads(restic(arguments))
    identifiers = []
    for group in groups:
        for entry in group.get("remove", []) or []:
            identifier = entry["id"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{64}", identifier):
                raise RuntimeError("Invalid retention receipt")
            identifiers.append(identifier)
    if apply:
        restic(["prune"])
    return identifiers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    upload_parser = commands.add_parser("upload")
    upload_parser.add_argument("--input", type=Path, required=True)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--snapshot", required=True)
    restore_parser.add_argument("--output", type=Path, required=True)
    retention_parser = commands.add_parser("retention")
    retention_parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "upload":
            print(
                json.dumps(
                    {"status": "encrypted_snapshot_uploaded", "snapshot": upload(args.input)}
                )
            )
        elif args.command == "restore":
            restore(args.snapshot, args.output)
            print(json.dumps({"status": "isolated_restore_verified"}))
        else:
            removed = retention(apply=args.apply)
            print(
                json.dumps(
                    {
                        "status": "retention_applied" if args.apply else "retention_dry_run",
                        "remove_snapshot_ids": removed,
                    }
                )
            )
    except Exception as exc:
        print(json.dumps({"error": "backup_operation_failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
