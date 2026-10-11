"""Build an explicit, credential-free source archive for manual SSH deployment."""

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    ".env.example",
    ".python-version",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "alembic.ini",
)
DIRECTORY_SUFFIXES = {
    "src": {".py", ".json", ".html", ".css", ".js"},
    "migrations": {".py", ".mako"},
    "tests": {".py"},
    "scripts": {".py", ".sh"},
    "evals": {".json", ".jsonl", ".jpg", ".jpeg", ".png"},
}
DEPLOY_FILES = (
    "deploy/compose.yml",
    "deploy/dbcheck.py",
    "deploy/bite-club-deploy",
    "deploy/bite-club-deploy-ssh",
    "deploy/nginx.conf.example",
)


def release_contents(root: Path) -> dict[str, bytes]:
    """Package only public runtime and verification inputs, never private state."""
    paths = [root / name for name in (*FILES, *DEPLOY_FILES)]
    for name, suffixes in DIRECTORY_SUFFIXES.items():
        paths.extend(
            path
            for path in (root / name).rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and "__pycache__" not in path.parts
            and not any(
                parent.is_symlink() for parent in path.parents if parent.is_relative_to(root)
            )
            and path.suffix in suffixes
        )
    if any(
        path.is_symlink()
        or not path.is_file()
        or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root))
        for path in paths
    ):
        raise ValueError("A required release input is missing or is a symlink.")
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(paths)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    contents = release_contents(ROOT)
    manifest = {name: hashlib.sha256(body).hexdigest() for name, body in contents.items()}
    release = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:12]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz") as archive:
        for name, body in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o755 if name in DEPLOY_FILES[2:4] or name.endswith(".sh") else 0o644
            archive.addfile(info, io.BytesIO(body))
        body = json.dumps({"release": release, "files": manifest}, indent=2).encode()
        info = tarfile.TarInfo("release-manifest.json")
        info.size, info.mode = len(body), 0o644
        archive.addfile(info, io.BytesIO(body))
    print(json.dumps({"release": release, "archive": str(args.output), "files": len(contents)}))


if __name__ == "__main__":
    main()
