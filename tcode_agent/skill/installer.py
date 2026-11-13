"""Safe, transactional installation of Tcode skills from GitHub."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import yaml

from .manager import SkillManager


MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_SKILL_BYTES = 5 * 1024 * 1024
MAX_FILES = 128
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class GitHubSkillSource:
    owner: str
    repo: str
    ref: str
    path: str

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_github_source(source: str, *, path: str = "", ref: str = "main") -> GitHubSkillSource:
    raw = str(source or "").strip()
    requested_path = str(path or "").strip().strip("/")
    requested_ref = str(ref or "main").strip() or "main"
    if raw.startswith("https://github.com/"):
        parts = [unquote(part) for part in urlparse(raw).path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("GitHub URL must include owner and repository")
        owner, repo = parts[0], parts[1].removesuffix(".git")
        if len(parts) >= 5 and parts[2] == "tree":
            requested_ref = parts[3]
            requested_path = "/".join(parts[4:])
        elif len(parts) > 2:
            raise ValueError("Use a repository URL or /tree/<ref>/<skill-path> URL")
    else:
        parts = [part for part in raw.split("/") if part]
        if len(parts) != 2:
            raise ValueError("source must be a GitHub URL or owner/repo")
        owner, repo = parts[0], parts[1].removesuffix(".git")
    if not requested_path:
        raise ValueError("Skill path is required unless it is included in the GitHub URL")
    pure_path = PurePosixPath(requested_path)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise ValueError("Skill path must stay inside the repository")
    if not all(re.fullmatch(r"[A-Za-z0-9._-]+", value or "") for value in (owner, repo)):
        raise ValueError("Invalid GitHub repository")
    return GitHubSkillSource(owner, repo, requested_ref, pure_path.as_posix())


def _download_archive(source: GitHubSkillSource, destination: Path) -> Path:
    url = f"https://codeload.github.com/{source.owner}/{source.repo}/zip/{source.ref}"
    request = urllib.request.Request(url, headers={"User-Agent": "tcode-skill-installer/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as output:
            total = 0
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError("Repository archive exceeds 20 MiB")
                output.write(chunk)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub download failed with HTTP {exc.code}") from exc
    return destination


def _safe_extract(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > 10_000:
            raise ValueError("Repository archive contains too many files")
        if sum(member.file_size for member in members) > MAX_UNPACKED_BYTES:
            raise ValueError("Repository archive expands beyond 100 MiB")
        for member in members:
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Repository archive contains a symbolic link")
            target = (destination / member.filename).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise ValueError("Repository archive contains an unsafe path")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    roots = [item for item in destination.iterdir() if item.is_dir()]
    if len(roots) != 1:
        raise ValueError("GitHub archive has an unexpected layout")
    return roots[0]


def _git_sparse_checkout(source: GitHubSkillSource, destination: Path) -> Path:
    repository = f"https://github.com/{source.repository}.git"
    if re.fullmatch(r"[a-f0-9]{40}", source.ref):
        commands = [
            ["git", "init", str(destination)],
            ["git", "-C", str(destination), "remote", "add", "origin", repository],
            ["git", "-C", str(destination), "fetch", "--depth", "1", "--filter=blob:none", "origin", source.ref],
            ["git", "-C", str(destination), "checkout", "--detach", "FETCH_HEAD"],
        ]
    else:
        commands = [["git", "clone", "--filter=blob:none", "--depth", "1", "--sparse", "--branch", source.ref, repository, str(destination)]]
    if source.path != ".":
        sparse = ["git", "-C", str(destination), "sparse-checkout", "set", "--no-cone", source.path]
        if re.fullmatch(r"[a-f0-9]{40}", source.ref):
            commands.insert(3, sparse)  # Restrict paths before the pinned checkout downloads blobs.
        else:
            commands.append(sparse)
    for command in commands:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        if completed.returncode:
            raise RuntimeError((completed.stderr or completed.stdout or "git failed").strip())
    return destination


def _source_error(
    source: GitHubSkillSource,
    download_error: Exception,
    git_error: Exception,
) -> Exception:
    detail = f"{download_error} {git_error}".lower()
    if "http 404" in detail or "repository not found" in detail:
        return ValueError(
            f'找不到 GitHub 仓库 "{source.repository}"。'
            "请检查 owner/repo 是否正确，以及私有仓库凭据是否可用。"
        )
    if "remote branch" in detail or "couldn't find remote ref" in detail:
        return ValueError(f'仓库中不存在分支或标签 "{source.ref}"。')
    return RuntimeError(f"下载失败: {download_error}; Git 回退失败: {git_error}")


def _validate_skill(source_dir: Path, requested_name: str = "") -> tuple[str, dict]:
    manifest = source_dir / "SKILL.md"
    if not manifest.is_file():
        raise ValueError("The selected directory does not contain SKILL.md")
    files = [item for item in source_dir.rglob("*") if item.is_file() and '.git' not in item.relative_to(source_dir).parts]
    if any(item.is_symlink() for item in source_dir.rglob("*") if '.git' not in item.relative_to(source_dir).parts):
        raise ValueError("Skill packages may not contain symbolic links")
    if len(files) > MAX_FILES:
        raise ValueError(f"Skill contains more than {MAX_FILES} files")
    total = 0
    for item in files:
        if item.is_symlink():
            raise ValueError("Skill packages may not contain symbolic links")
        total += item.stat().st_size
    if total > MAX_SKILL_BYTES:
        raise ValueError("Skill package exceeds 5 MiB")
    return validate_skill_manifest(manifest.read_text(encoding="utf-8"), requested_name, source_dir.name)


def validate_skill_manifest(text: str, requested_name: str = "", directory_name: str = "") -> tuple[str, dict]:
    """Shared metadata validation for source preview and the existing installer."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if not match:
        raise ValueError("SKILL.md must begin with YAML frontmatter")
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError("SKILL.md frontmatter is not valid YAML") from exc
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md frontmatter must be an object")
    name = str(requested_name or metadata.get("name") or directory_name).strip().lower()
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("Skill name must use lowercase letters, numbers, hyphens, or underscores")
    if not str(metadata.get("description") or "").strip():
        raise ValueError("SKILL.md frontmatter must include description")
    return name, metadata


def _install_root(workdir: str, scope: str) -> Path:
    if scope == "home":
        return Path.home() / ".tcode" / "skills"
    if scope != "workspace":
        raise ValueError('scope must be "workspace" or "home"')
    return Path(workdir).expanduser().resolve() / ".tcode" / "skills"


def _record_path(workdir: str, scope: str) -> Path:
    root = Path.home() / ".tcode" if scope == "home" else Path(workdir).expanduser().resolve() / ".tcode"
    return root / "skill-installs.json"


def _write_record(workdir: str, scope: str, name: str, source: GitHubSkillSource) -> None:
    path = _record_path(workdir, scope)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "skills": {}}
    except Exception:
        payload = {"version": 1, "skills": {}}
    payload.setdefault("skills", {})[name] = {
        "source": f"https://github.com/{source.repository}",
        "path": source.path,
        "ref": source.ref,
        "scope": scope,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


async def install_skill_from_github(*, source_url: str, workdir: str, path: str = "", ref: str = "main", name: str = "", scope: str = "workspace", method: str = "auto") -> dict:
    source = parse_github_source(source_url, path=path, ref=ref)
    install_root = _install_root(workdir, scope)
    method = str(method or "auto").lower()
    if method not in {"auto", "download", "git"}:
        raise ValueError('method must be "auto", "download", or "git"')
    with tempfile.TemporaryDirectory(prefix="tcode-skill-") as temporary_name:
        temporary = Path(temporary_name)
        repository_root: Path | None = None
        download_error: Exception | None = None
        if method in {"auto", "download"}:
            try:
                repository_root = _safe_extract(_download_archive(source, temporary / "repo.zip"), temporary / "archive")
            except Exception as exc:
                download_error = exc
                if method == "download":
                    raise
        if repository_root is None:
            try:
                repository_root = _git_sparse_checkout(source, temporary / "git")
            except Exception as exc:
                if download_error:
                    raise _source_error(source, download_error, exc) from exc
                raise
        source_dir = (repository_root / source.path).resolve()
        if (repository_root.resolve() not in source_dir.parents and source_dir != repository_root.resolve()) or not source_dir.is_dir():
            raise ValueError(f'Skill path "{source.path}" was not found')
        skill_name, metadata = _validate_skill(source_dir, name)
        destination = install_root / skill_name
        if destination.exists():
            raise FileExistsError(f'Skill "{skill_name}" is already installed at {destination}')
        install_root.mkdir(parents=True, exist_ok=True)
        staging = install_root / f".{skill_name}.installing"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(source_dir, staging, ignore=shutil.ignore_patterns('.git'))
        os.replace(staging, destination)
    _write_record(workdir, scope, skill_name, source)
    await SkillManager.reload(workdir=workdir)
    return {"name": skill_name, "description": str(metadata.get("description") or ""), "location": str(destination), "source": f"https://github.com/{source.repository}/tree/{source.ref}/{source.path}", "scope": scope}
