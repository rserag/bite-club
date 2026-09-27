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
    "src/nutrition_bot/data/reference/supplement_protocols.json",
)
DIRECTORIES = ("src", "migrations", "tests", "scripts")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    paths = [ROOT / name for name in FILES]
    for name in DIRECTORIES:
        paths.extend(
            path
            for path in (ROOT / name).rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and "__pycache__" not in path.parts
            and path.suffix in (".py", ".sh", ".mako")
        )
    contents = {str(path.relative_to(ROOT)): path.read_bytes() for path in sorted(paths)}
    manifest = {name: hashlib.sha256(body).hexdigest() for name, body in contents.items()}
    release = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:12]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz") as archive:
        for name, body in contents.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), 0o644
            archive.addfile(info, io.BytesIO(body))
        body = json.dumps({"release": release, "files": manifest}, indent=2).encode()
        info = tarfile.TarInfo("release-manifest.json")
        info.size, info.mode = len(body), 0o644
        archive.addfile(info, io.BytesIO(body))
    print(json.dumps({"release": release, "archive": str(args.output), "files": len(contents)}))


if __name__ == "__main__":
    main()
