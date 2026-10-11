"""Check the tracked/staged source boundary without displaying credential contents.

This is a fast repository guard, not a substitute for a dedicated secret scanner
or manual publication review. Run --staged before the initial commit.
"""

import argparse
import hashlib
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
# Exact reviewed synthetic snapshots; never trust an extension or a mutable
# working-tree manifest when inspecting the index. New images need review.
REVIEWED_JPEGS = {
    "evals/fixtures/meal-photo-01.jpg": (
        "349ee21e77e00ecf8fa4c4b7fe0494ac2c99fe8008c911587235dbcb5f4c1218"
    ),
    "evals/fixtures/meal-photo-02.jpg": (
        "2f19df0adba13acbfd9625d8cffc3c1abadd6e9f07c54773ef4ae4e825044dfb"
    ),
    "evals/fixtures/meal-photo-03.jpg": (
        "9c8530348cd5e4fee32cbd014151859884eed9fd5a578d2f0225f2d23bbda3ae"
    ),
    "evals/fixtures/meal-photo-04.jpg": (
        "1e9aa35696207b059d939bceb72a27bfa2aa67162bf801ca9c42bc570a54abd1"
    ),
    "evals/fixtures/meal-photo-05.jpg": (
        "18550b3731b4dd72e4e2e31e413ebd93ebb4484c434021f3877a83a5f5b6429a"
    ),
    "evals/fixtures/meal-photo-06.jpg": (
        "2b6ae1b20a2308d1a742887c73d8de495cd04f53ded51b06ab1f3032c064fe01"
    ),
    "evals/fixtures/meal-photo-07.jpg": (
        "4236c2a85db2bd1f954b451cfa87ef93372685274e758018312e614f84b7e089"
    ),
    "evals/fixtures/meal-photo-08.jpg": (
        "0c95e62f2c1a0ecd20f647197d448e8a144ed20cfd0c0d9fdad3034a0299ccee"
    ),
    "evals/fixtures/meal-photo-09.jpg": (
        "8b932d931c43a9eb4411f7123296f409c16e410db48276184cedd1af0145cbbd"
    ),
    "evals/fixtures/meal-photo-10.jpg": (
        "ee086527a1a1ea41a168ce9b1314605b14ec7f20e29cf130f0eac97d6f6844e9"
    ),
}
# The standard luminance AC Huffman table contains a token-shaped byte sequence.
# Only that table's payload may supply an exception, never metadata or pixels.
STANDARD_AC_HUFFMAN_SHA256 = "f27ce9b718f91cd426885e6ce4a37d0ba4900db219f719eef29ecc1f7f4fd5f0"


def reviewed_huffman_ranges(name: str, body: bytes) -> list[tuple[int, int]]:
    if name not in REVIEWED_JPEGS or hashlib.sha256(body).hexdigest() != REVIEWED_JPEGS[name]:
        return []
    if not body.startswith(b"\xff\xd8"):
        return []
    ranges: list[tuple[int, int]] = []
    offset = 2
    while offset + 4 <= len(body):
        if body[offset] != 0xFF:
            return []
        marker = body[offset + 1]
        size = int.from_bytes(body[offset + 2 : offset + 4], "big")
        end = offset + 2 + size
        if size < 2 or end > len(body):
            return []
        if marker == 0xDA:  # Stop at scan data; every subsequent byte remains scanned.
            return ranges
        if marker == 0xC4:
            start = offset + 4
            if hashlib.sha256(body[start:end]).hexdigest() == STANDARD_AC_HUFFMAN_SHA256:
                ranges.append((start, end))
        offset = end
    return []


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
    huffman_ranges = reviewed_huffman_ranges(name, body)
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(body):
            if kind == "telegram-token" and any(
                start <= match.start() and match.end() <= end for start, end in huffman_ranges
            ):
                continue
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
