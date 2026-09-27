"""Check the tracked/staged source boundary without displaying credential contents.

This is a fast repository guard, not a substitute for a dedicated secret scanner
or manual publication review. Run --staged before the initial commit.
"""

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PARTS = {
    "private",
    ".beads",
    ".dolt",
    ".doltcfg",
    ".agents",
    ".codex",
    ".venv",
    "runtime-data",
    "backups",
    "exports",
    "logs",
    "__pycache__",
}
PRIVATE_SUFFIXES = (
    ".sqlite",
    ".sqlite3",
    ".db",
    ".session",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".zip",
    ".log",
    ".bak",
    ".pyc",
)
PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    "telegram-token": re.compile(rb"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    "github-token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})"),
    "personal-home": re.compile(rb"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
}
SYNTHETIC_TOKENS = {
    b"123456:synthetic_token_for_offline_tests_only",
    b"678901:synthetic_token_for_offline_tests_only",
}


def inspect(name: str, body: bytes) -> list[str]:
    path = Path(name)
    problems = []
    if (
        set(path.parts) & PRIVATE_PARTS
        or name == "config.yaml"
        or (path.name.startswith(".env") and path.name != ".env.example")
        or name.endswith(PRIVATE_SUFFIXES)
        or ".session-" in name
        or name.endswith(("-wal", "-shm", "-journal"))
    ):
        problems.append("private-file")
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(body):
            if (
                kind == "telegram-token"
                and match.group() in SYNTHETIC_TOKENS
                and (
                    name.startswith(("tests/", "tools/telegram_e2e/tests/"))
                    or name in {"scripts/demo.py", "scripts/audit_publication.py"}
                )
            ):
                continue
            problems.append(kind)
    return sorted(set(problems))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="inspect exact index contents")
    args = parser.parse_args()
    output = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=ROOT)
    entries = [part for part in output.split(b"\0") if part]
    if not entries:
        raise SystemExit("No tracked/staged files; nothing has been audited.")
    failures = []
    for entry in entries:
        metadata, encoded_name = entry.split(b"\t", 1)
        mode, _, stage = metadata.split()
        name = encoded_name.decode()
        if mode not in {b"100644", b"100755"} or stage != b"0":
            failures.append((name, ["symlink-submodule-or-conflict"]))
            continue
        body = (
            subprocess.check_output(["git", "show", f":{name}"], cwd=ROOT)
            if args.staged
            else (ROOT / name).read_bytes()
        )
        problems = inspect(name, body)
        if problems:
            failures.append((name, problems))
    for name, problems in failures:
        print(f"{name}: {', '.join(problems)}")
    if failures:
        raise SystemExit("Publication boundary check failed; credential contents were not printed.")
    print(f"Publication boundary check passed for {len(entries)} files.")


if __name__ == "__main__":
    main()
