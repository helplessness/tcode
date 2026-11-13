"""Read-only discovery of GitHub skills linked by a public introduction page.

Page content is untrusted data, never instructions. No scripts or install commands
are executed. Candidates are verified using SKILL.md at a pinned Git commit.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

import aiohttp

from .installer import GitHubSkillSource, _download_archive, _safe_extract, validate_skill_manifest

MAX_PAGE_BYTES = 1024 * 1024
MAX_REPOSITORIES = 3
MAX_CANDIDATES = 10


def _archive_candidates(repository: str, ref: str, path: str, hint: str) -> list[dict]:
    """Bounded anonymous fallback when GitHub's public API is unavailable."""
    if not re.fullmatch(r"[a-f0-9]{40}", ref):
        result = subprocess.run(
            ["git", "-c", "credential.helper=", "-c", "core.askPass=", "ls-remote", "--symref",
             f"https://github.com/{repository}.git", ref or "HEAD"],
            capture_output=True, text=True, timeout=15, check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""},
        )
        shas = re.findall(r"^([a-f0-9]{40})\s", result.stdout, flags=re.MULTILINE)
        if result.returncode or len(set(shas)) != 1:
            raise ValueError("无法匿名确认 GitHub 版本，请提供精确的公开技能目录地址")
        ref = shas[0]
    owner, repo = repository.split("/", 1)
    with tempfile.TemporaryDirectory(prefix="tcode-skill-preview-") as temporary:
        root = Path(temporary)
        source = GitHubSkillSource(owner, repo, ref, path or ".")
        unpacked = _safe_extract(_download_archive(source, root / "repo.zip"), root / "contents")
        manifests = [unpacked / path / "SKILL.md"] if path else sorted(unpacked.rglob("SKILL.md"))
        # Prefer the explicitly named introduction page's skill, but still require confirmation.
        manifests.sort(key=lambda item: item.parent.name != hint)
        candidates = []
        for manifest in manifests[:MAX_CANDIDATES]:
            if not manifest.is_file():
                continue
            try:
                name, metadata = validate_skill_manifest(manifest.read_text(encoding="utf-8"), directory_name=manifest.parent.name)
            except (ValueError, UnicodeError):
                continue
            skill_path = manifest.parent.relative_to(unpacked).as_posix()
            candidates.append(_candidate(repository, ref, "" if skill_path == "." else skill_path, name, metadata))
        return candidates


def _candidate(repository: str, sha: str, skill_path: str, name: str, metadata: dict) -> dict:
    return {"name": name, "description": str(metadata["description"])[:1000],
            "repository": repository, "path": skill_path or ".", "ref": sha,
            "source_url": f"https://github.com/{repository}/tree/{sha}/{quote(skill_path, safe='/')}" if skill_path else f"https://github.com/{repository}",
            "install_path": skill_path or "."}


async def _fetch_text(url: str) -> str:
    # Reuse the existing public-network policy, including DNS rebinding protection.
    from ..tools.builtin.webfetch import _PublicNetworkResolver, _validate_public_url

    connector = aiohttp.TCPConnector(resolver=_PublicNetworkResolver())
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                    timeout=aiohttp.ClientTimeout(total=15)) as session:
        for _ in range(5):
            _validate_public_url(url)
            async with session.get(url, allow_redirects=False,
                                   headers={"User-Agent": "tcode-skill-source/1", "Accept": "text/html,application/json,text/plain"}) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("来源页面的跳转地址缺失")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ValueError(f"无法读取来源（HTTP {response.status}），请提供 GitHub 技能目录地址")
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_PAGE_BYTES:
                        raise ValueError("来源页面过大，请提供更具体的技能目录地址")
                return chunks.decode("utf-8", errors="replace")
    raise ValueError("来源页面跳转次数过多")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)


def _github_location(url: str) -> tuple[str, str, str] | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts[:2]):
        return None
    repository = f"{parts[0]}/{parts[1].removesuffix('.git')}"
    if len(parts) == 2:
        return repository, "", ""
    if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
        ref = parts[3]
        path = "/".join(parts[4:])
        if parts[2] == "blob":
            if not path.endswith("SKILL.md"):
                return None
            path = path.removesuffix("SKILL.md").rstrip("/")
        if ".." in path.split("/") or "\\" in path or not ref or ref.startswith('-') or any(ord(c) < 32 for c in ref + path):
            return None
        return repository, ref, path
    return None


def github_links(page_url: str, html: str) -> list[tuple[str, str, str]]:
    parser = _Links()
    parser.feed(html)
    # Some introduction sites embed the source URL in structured page data.
    urls = parser.links + re.findall(r'https://github\.com/[^\s<>"\x27\\]+', html.replace("\\/", "/"))
    result = []
    for value in urls:
        location = _github_location(urljoin(page_url, value))
        if location and location not in result:
            result.append(location)
    return result


async def resolve_skill_source(page_url: str) -> dict:
    from ..tools.builtin.webfetch import _validate_public_url

    page_url = str(page_url or "").strip()
    _validate_public_url(page_url)
    async with asyncio.timeout(60):
        direct = _github_location(page_url)
        locations = [direct] if direct else github_links(page_url, await _fetch_text(page_url))
        # Prefer exact skill-directory links over broad repository home pages.
        locations.sort(key=lambda item: not bool(item[2]))
        repositories: set[str] = set()
        candidates: list[dict] = []
        warnings: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        commits: dict[tuple[str, str], str] = {}
        for repository, ref, path in locations:
            if repository not in repositories and len(repositories) >= MAX_REPOSITORIES:
                warnings.append("页面关联的仓库较多，仅检查前三个；请提供更具体的来源")
                break
            repositories.add(repository)
            try:
                if not ref:
                    info = json.loads(await _fetch_text(f"https://api.github.com/repos/{repository}"))
                    ref = str(info["default_branch"])
                key = (repository, ref)
                if key not in commits:
                    info = json.loads(await _fetch_text(f"https://api.github.com/repos/{repository}/commits/{quote(ref, safe='')}"))
                    sha = str(info.get("sha", ""))
                    if not re.fullmatch(r"[a-f0-9]{40}", sha):
                        raise ValueError("GitHub 返回了无效的版本信息")
                    commits[key] = sha
                sha = commits[key]
                paths = [path] if path else []
                if not path:
                    tree = json.loads(await _fetch_text(f"https://api.github.com/repos/{repository}/git/trees/{sha}?recursive=1"))
                    paths = [str(item["path"]).removesuffix("SKILL.md").rstrip("/")
                             for item in tree.get("tree", []) if item.get("type") == "blob"
                             and (item.get("path") == "SKILL.md" or str(item.get("path", "")).endswith("/SKILL.md"))]
                    if tree.get("truncated") or len(paths) > MAX_CANDIDATES:
                        warnings.append("仓库技能较多，当前仅展示部分候选；可提供精确技能目录地址")
                for skill_path in paths[:MAX_CANDIDATES]:
                    identity = (repository, sha, skill_path)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    manifest_path = f"{skill_path}/SKILL.md" if skill_path else "SKILL.md"
                    try:
                        text = await _fetch_text(f"https://raw.githubusercontent.com/{repository}/{sha}/{quote(manifest_path, safe='/')}")
                        name, metadata = validate_skill_manifest(text, directory_name=skill_path.rsplit("/", 1)[-1] or repository.split("/")[1])
                    except ValueError:
                        continue
                    candidates.append(_candidate(repository, sha, skill_path, name, metadata))
                    if len(candidates) >= MAX_CANDIDATES:
                        break
            except (ValueError, KeyError, aiohttp.ClientError) as exc:
                try:
                    fallback = await asyncio.to_thread(_archive_candidates, repository, ref, path, unquote(urlsplit(page_url).path.rstrip('/').rsplit('/', 1)[-1]))
                    for candidate in fallback:
                        if candidate not in candidates:
                            candidates.append(candidate)
                        if len(candidates) >= MAX_CANDIDATES:
                            break
                    warnings.append("GitHub API 暂不可用，已通过受限下载读取技能清单；未安装或执行任何内容")
                    if len(fallback) >= MAX_CANDIDATES:
                        warnings.append("仓库技能较多，当前仅展示部分候选；可提供精确技能目录地址")
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                    warnings.append(f"无法验证仓库 {repository}：{exc}")
            if len(candidates) >= MAX_CANDIDATES:
                break
        if not candidates:
            raise ValueError("未找到可验证的 GitHub 技能目录。请提供包含 SKILL.md 的目录链接；登录、动态加载或私有页面可能无法自动解析。")
        return {"page_url": page_url, "candidates": candidates, "warnings": list(dict.fromkeys(warnings)),
                "notice": "已验证技能清单格式，不代表来源可信或安全审计通过。安装后仍可能需要依赖、API Key 或 MCP 配置；安装不会执行包内脚本。"}


async def select_skill_source(page_url: str, source_url: str, path: str = "", ref: str = "") -> dict:
    """Recheck provenance before a confirmed install; never accept an invented link."""
    result = await resolve_skill_source(page_url)
    for candidate in result["candidates"]:
        if (candidate["source_url"] == source_url
                and (not path or candidate["install_path"] == path)
                and (not ref or candidate["ref"] == ref)):
            return candidate
    raise ValueError("所选来源已变化或不属于该介绍页，请重新解析并确认")
