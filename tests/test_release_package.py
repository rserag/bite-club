"""The manual source package includes runtime assets without private state."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def packager() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/package-release.py"
    spec = importlib.util.spec_from_file_location("release_packager", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_package_has_runtime_and_container_test_inputs() -> None:
    module = packager()
    contents = module.release_contents(module.ROOT)
    required = {
        "src/nutrition_bot/miniapp/static/app.js",
        "src/nutrition_bot/miniapp/static/app.css",
        "src/nutrition_bot/miniapp/static/index.html",
        "src/nutrition_bot/data/reference/supplement_protocols.json",
        "deploy/dbcheck.py",
        "deploy/bite-club-deploy",
        "scripts/container-smoke.sh",
        "evals/fixtures/meal-photos.json",
    }
    assert required <= contents.keys()
    assert not any(name.startswith(("private/", ".beads/", ".git/")) for name in contents)
    assert not any(name.endswith((".sqlite3", ".env", ".log")) for name in contents)


def test_package_excludes_private_files_and_symlink_targets(tmp_path: Path) -> None:
    module = packager()
    for name in (*module.FILES, *module.DEPLOY_FILES):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic public input")
    private = tmp_path / "private"
    private.mkdir()
    secret = private / "secret.json"
    secret.write_text("synthetic secret marker")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/linked.json").symlink_to(secret)
    (tmp_path / "src/linked-directory").symlink_to(private, target_is_directory=True)
    (tmp_path / "src/runtime.sqlite3").write_text("synthetic database")
    contents = module.release_contents(tmp_path)
    assert not any(b"synthetic secret marker" in body for body in contents.values())
    assert "src/runtime.sqlite3" not in contents
    required = tmp_path / "Dockerfile"
    required.unlink()
    required.symlink_to(secret)
    with pytest.raises(ValueError, match="symlink"):
        module.release_contents(tmp_path)
    required.unlink()
    required.write_text("synthetic public input")
    (tmp_path / "deploy").rename(private / "deploy")
    (tmp_path / "deploy").symlink_to(private / "deploy", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        module.release_contents(tmp_path)
