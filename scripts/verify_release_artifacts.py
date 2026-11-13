#!/usr/bin/env python3
"""Validate TCode Python and npm release archives before publication."""

from __future__ import annotations

import argparse
import ast
import json
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]


def _molstar_files() -> set[str]:
    version = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))["dependencies"]["molstar"]
    vendor = f"tcode_agent/server/static/vendor/molstar-{version}"
    return {f"{vendor}/{name}" for name in ("molstar.js", "molstar.css", "LICENSE", "images/cells.jpg")}


class ReleaseArtifactError(RuntimeError):
    """Raised when a release archive violates the public package contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseArtifactError(message)


def _package_version() -> str:
    tree = ast.parse((ROOT / "tcode_agent" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str):
            return value
    raise ReleaseArtifactError("tcode_agent.__version__ is not a static string")


def verify_metadata() -> dict[str, str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    npm = json.loads((ROOT / "packages" / "tcode-agui-client" / "package.json").read_text(encoding="utf-8"))
    release_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    python_version = str(pyproject["project"]["version"])
    import_version = _package_version()
    npm_version = str(npm["version"])

    expected = Version(release_version)
    for label, value in (
        ("pyproject.toml", python_version),
        ("tcode_agent.__version__", import_version),
        ("npm package", npm_version),
    ):
        _require(Version(value) == expected, f"{label} version {value!r} does not match VERSION {release_version!r}")

    project = pyproject["project"]
    _require(project.get("license") == "MIT", "Python package license must be MIT")
    _require(project.get("readme") == "README.md", "Python package README metadata is missing")
    _require({"Documentation", "Issues", "Source"} <= set(project.get("urls") or {}), "Python project URLs are incomplete")
    _require({"tcode", "tcode-server", "tcode-plugin"} <= set(project.get("scripts") or {}), "Python console scripts are incomplete")

    _require(npm.get("license") == "MIT", "npm package license must be MIT")
    _require(npm.get("sideEffects") is False, "npm package must declare sideEffects=false")
    _require(str((npm.get("engines") or {}).get("node") or ""), "npm Node.js engine is missing")
    _require((npm.get("publishConfig") or {}).get("access") == "public", "scoped npm package must publish with public access")
    _require({"repository", "homepage", "bugs", "keywords"} <= set(npm), "npm public metadata is incomplete")
    _require(set(npm.get("files") or []) == {"dist", "README.md", "LICENSE"}, "npm files allowlist changed")

    return {
        "release": release_version,
        "python": python_version,
        "npm": npm_version,
    }


def _safe_names(names: set[str], *, label: str) -> None:
    forbidden_directories = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "node_modules",
    }
    forbidden_filenames = {
        ".coverage",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "local.env",
    }
    for name in names:
        path = PurePosixPath(name)
        lowered_parts = {part.lower() for part in path.parts}
        lowered_name = path.name.lower()
        _require(not path.is_absolute() and ".." not in path.parts, f"{label} contains unsafe path: {name}")
        _require("__pycache__" not in path.parts, f"{label} contains __pycache__: {name}")
        _require(path.name != ".DS_Store", f"{label} contains .DS_Store: {name}")
        _require(path.suffix not in {".pyc", ".pyo"}, f"{label} contains bytecode: {name}")
        _require(not forbidden_directories.intersection(lowered_parts), f"{label} contains cache or dependency directory: {name}")
        _require(
            lowered_name not in forbidden_filenames
            and lowered_name != ".env"
            and not lowered_name.startswith(".env.")
            and not lowered_name.startswith("config.local.")
            and path.suffix.lower() not in {".key", ".p12", ".pem", ".pfx"},
            f"{label} contains local configuration or credential material: {name}",
        )


def verify_wheel(path: Path) -> dict[str, object]:
    _require(path.is_file() and path.suffix == ".whl", f"wheel does not exist: {path}")
    with zipfile.ZipFile(path) as archive:
        names = {name.rstrip("/") for name in archive.namelist() if name and not name.endswith("/")}
    _safe_names(names, label="wheel")

    required = {
        "tcode_agent/__init__.py",
        "tcode_agent/protocol/schema/web-protocol-v1.schema.json",
        "tcode_agent/protocol/v2/schema/protocol.schema.json",
        "tcode_agent/plugin_sdk/schema/plugin-manifest-v2.schema.json",
        "tcode_agent/plugin_sdk/templates/python-plugin/plugin.json",
        "tcode_agent/server/static/index.html",
        "tcode_agent/server/static/desktop.html",
        "tcode_agent/external_harness/runtimes/opencode/README.md",
        "tcode_agent/external_harness/schema/harness-runtime-v2.schema.json",
        "tcode_agent/external_harness/provider.py",
        "tcode_agent/external_harness/conformance/runner.py",
    } | _molstar_files()
    missing = sorted(required - names)
    _require(not missing, f"wheel is missing required files: {missing}")
    _require(any(name.startswith("tcode_agent/server/static/assets/") for name in names), "wheel has no built Web assets")
    _require(any(name.endswith(".dist-info/METADATA") for name in names), "wheel METADATA is missing")
    _require(any(".dist-info/licenses/LICENSE" in name for name in names), "wheel license is missing")
    return {"path": str(path), "files": len(names)}


def _tar_names(path: Path) -> set[str]:
    with tarfile.open(path, mode="r:*") as archive:
        return {member.name.rstrip("/") for member in archive.getmembers() if member.isfile()}


def _strip_archive_root(names: set[str]) -> set[str]:
    stripped: set[str] = set()
    for name in names:
        parts = PurePosixPath(name).parts
        if len(parts) > 1:
            stripped.add(PurePosixPath(*parts[1:]).as_posix())
    return stripped


def verify_sdist(path: Path) -> dict[str, object]:
    _require(path.is_file() and path.name.endswith(".tar.gz"), f"sdist does not exist: {path}")
    raw_names = _tar_names(path)
    _safe_names(raw_names, label="sdist")
    names = _strip_archive_root(raw_names)
    required = {
        "LICENSE",
        "README.md",
        "VERSION",
        "pyproject.toml",
        "tcode_agent/protocol/v2/schema/protocol.schema.json",
        "tcode_agent/server/static/index.html",
    } | _molstar_files()
    missing = sorted(required - names)
    _require(not missing, f"sdist is missing required files: {missing}")
    return {"path": str(path), "files": len(raw_names)}


def verify_npm_tarball(path: Path) -> dict[str, object]:
    _require(path.is_file() and path.suffix == ".tgz", f"npm tarball does not exist: {path}")
    names = _tar_names(path)
    _safe_names(names, label="npm tarball")
    required = {
        "package/package.json",
        "package/README.md",
        "package/LICENSE",
        "package/dist/index.js",
        "package/dist/index.d.ts",
    }
    missing = sorted(required - names)
    _require(not missing, f"npm tarball is missing required files: {missing}")
    allowed_files = {"package/package.json", "package/README.md", "package/LICENSE"}
    unexpected = sorted(name for name in names if name not in allowed_files and not name.startswith("package/dist/"))
    _require(not unexpected, f"npm tarball contains files outside the public allowlist: {unexpected}")
    _require(not any("/src/" in name or "/node_modules/" in name for name in names), "npm tarball contains source or node_modules")
    return {"path": str(path), "files": len(names)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--npm-tarball", type=Path)
    args = parser.parse_args()

    report: dict[str, object] = {"schema": "tcode.release-artifacts.v1", "metadata": verify_metadata()}
    if args.wheel:
        report["wheel"] = verify_wheel(args.wheel.resolve())
    if args.sdist:
        report["sdist"] = verify_sdist(args.sdist.resolve())
    if args.npm_tarball:
        report["npm"] = verify_npm_tarball(args.npm_tarball.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseArtifactError as exc:
        raise SystemExit(f"release artifact verification failed: {exc}") from exc
