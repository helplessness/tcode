"""TCode 自有 Python 插件社区注册表与本地安装记录。"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .package import (
    inspect_package,
    public_key_fingerprint,
    public_key_pem,
    sign_digest,
    verify_digest_signature,
)


COMMUNITY_REGISTRY_SCHEMA = "tcode.community-plugin-registry.v1"
REVIEW_STATUSES = frozenset(
    {"official", "verified", "community", "quarantined", "revoked"}
)


def _utc_now() -> str:
    """返回不依赖本机时区的 UTC 审计时间。"""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class CommunityRelease:
    """保存一个不可变的社区插件版本记录。"""

    name: str
    version: str
    sdk: str
    python: str
    package: str
    sha256: str
    size: int
    permissions: tuple[dict[str, Any], ...]
    review_status: str
    published_at: str
    signer: str = ""
    signature: str = ""
    public_key: str = ""
    key_fingerprint: str = ""
    reviewed_at: str = ""
    reviewed_by: str = ""
    review_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """返回可持久化且不含私钥的发布元数据。"""

        value = asdict(self)
        value["permissions"] = [dict(item) for item in self.permissions]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CommunityRelease":
        """从注册表字典恢复发布记录。"""

        return cls(
            name=str(value.get("name") or ""),
            version=str(value.get("version") or ""),
            sdk=str(value.get("sdk") or ""),
            python=str(value.get("python") or ""),
            package=str(value.get("package") or ""),
            sha256=str(value.get("sha256") or ""),
            size=int(value.get("size") or 0),
            permissions=tuple(
                dict(item) for item in value.get("permissions") or []
                if isinstance(item, dict)
            ),
            review_status=str(value.get("review_status") or "community"),
            published_at=str(value.get("published_at") or ""),
            signer=str(value.get("signer") or ""),
            signature=str(value.get("signature") or ""),
            public_key=str(value.get("public_key") or ""),
            key_fingerprint=str(value.get("key_fingerprint") or ""),
            reviewed_at=str(value.get("reviewed_at") or ""),
            reviewed_by=str(value.get("reviewed_by") or ""),
            review_note=str(value.get("review_note") or ""),
        )


class CommunityRegistry:
    """管理本地文件型社区注册表、审核和安装审计。"""

    def __init__(self, path: str | Path) -> None:
        """绑定注册表路径，但不在构造阶段产生文件。"""

        self.path = Path(path).resolve()

    def _empty(self) -> dict[str, Any]:
        """返回新注册表的唯一初始结构。"""

        return {
            "schema": COMMUNITY_REGISTRY_SCHEMA,
            "releases": [],
            "installations": [],
            "audit": [],
        }

    def load(self) -> dict[str, Any]:
        """加载并校验注册表根结构；文件不存在时返回空表。"""

        if not self.path.is_file():
            return self._empty()
        document = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema") != COMMUNITY_REGISTRY_SCHEMA:
            raise ValueError("unsupported community registry schema")
        for field in ("releases", "installations", "audit"):
            if not isinstance(document.get(field), list):
                raise ValueError(f"community registry field must be an array: {field}")
        return document

    def _write(self, document: dict[str, Any]) -> None:
        """使用同目录临时文件和原子替换持久化注册表。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            temporary = Path(temporary_name)
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _release_key(value: dict[str, Any]) -> tuple[str, str]:
        """返回社区版本的唯一名称与版本键。"""

        return str(value.get("name") or ""), str(value.get("version") or "")

    def publish(
        self,
        package_path: str | Path,
        *,
        signer: str = "",
        private_key_path: str | Path | None = None,
    ) -> CommunityRelease:
        """校验包并提交一个默认为 community 的不可变版本。"""

        package = inspect_package(package_path)
        package_file = Path(package_path).resolve()
        with zipfile.ZipFile(package_file) as archive:
            manifest = json.loads(archive.read("plugin.json").decode("utf-8"))
        signature = ""
        public_key = ""
        fingerprint = ""
        if bool(signer.strip()) != bool(private_key_path):
            raise ValueError("signer and private signing key must be provided together")
        if private_key_path is not None:
            public_key = public_key_pem(private_key_path)
            signature = sign_digest(str(package["sha256"]), private_key_path)
            fingerprint = public_key_fingerprint(public_key)
        release = CommunityRelease(
            name=str(package["name"]),
            version=str(package["version"]),
            sdk=str(manifest.get("sdk") or ""),
            python=str(manifest.get("python") or ""),
            package=str(package_file),
            sha256=str(package["sha256"]),
            size=int(package["size"]),
            permissions=tuple(
                dict(item) for item in manifest.get("permissions") or []
                if isinstance(item, dict)
            ),
            review_status="community",
            published_at=_utc_now(),
            signer=str(signer or "").strip(),
            signature=signature,
            public_key=public_key,
            key_fingerprint=fingerprint,
        )
        document = self.load()
        key = (release.name, release.version)
        if any(self._release_key(item) == key for item in document["releases"]):
            raise ValueError(f"community release already exists: {release.name}@{release.version}")
        document["releases"].append(release.to_dict())
        document["releases"].sort(key=self._release_key)
        document["audit"].append(
            {"at": _utc_now(), "action": "publish", "name": release.name, "version": release.version}
        )
        self._write(document)
        return release

    def get(self, name: str, version: str) -> CommunityRelease:
        """按精确名称和版本返回发布记录。"""

        for item in self.load()["releases"]:
            if self._release_key(item) == (name, version):
                return CommunityRelease.from_dict(item)
        raise KeyError(f"community release not found: {name}@{version}")

    def review(
        self,
        name: str,
        version: str,
        *,
        status: str,
        reviewer: str,
        note: str = "",
    ) -> CommunityRelease:
        """以注册表管理员身份记录审核、隔离或撤销结论。"""

        if status not in REVIEW_STATUSES:
            raise ValueError(f"unsupported review status: {status}")
        if status in {"official", "verified"} and not reviewer.strip():
            raise ValueError("verified review requires a reviewer")
        document = self.load()
        selected: CommunityRelease | None = None
        for index, item in enumerate(document["releases"]):
            if self._release_key(item) != (name, version):
                continue
            updated = {
                **item,
                "review_status": status,
                "reviewed_at": _utc_now(),
                "reviewed_by": reviewer.strip(),
                "review_note": note.strip(),
            }
            selected = CommunityRelease.from_dict(updated)
            if status in {"official", "verified"}:
                valid, errors = self.verify_release(selected)
                if not selected.signature or not valid:
                    detail = "; ".join(errors) or "release is unsigned"
                    raise ValueError(
                        f"verified review requires a valid package signature: {detail}"
                    )
            document["releases"][index] = selected.to_dict()
            break
        if selected is None:
            raise KeyError(f"community release not found: {name}@{version}")
        document["audit"].append(
            {
                "at": _utc_now(),
                "action": "review",
                "name": name,
                "version": version,
                "status": status,
                "reviewer": reviewer.strip(),
            }
        )
        self._write(document)
        return selected

    def verify_release(self, release: CommunityRelease) -> tuple[bool, list[str]]:
        """验证包存在性、摘要和可选签名，但不自动授予权限。"""

        errors: list[str] = []
        path = Path(release.package)
        if not path.is_file():
            return False, ["package file is missing"]
        package = inspect_package(path)
        if package["sha256"] != release.sha256:
            errors.append("package digest mismatch")
        if release.signature or release.public_key:
            if not release.signature or not release.public_key:
                errors.append("signature metadata is incomplete")
            elif not verify_digest_signature(
                release.sha256,
                release.signature,
                release.public_key,
            ):
                errors.append("package signature is invalid")
            elif public_key_fingerprint(release.public_key) != release.key_fingerprint:
                errors.append("public key fingerprint mismatch")
        return not errors, errors

    def record_installation(
        self,
        release: CommunityRelease,
        *,
        path: str | Path,
        enabled: bool = False,
    ) -> None:
        """记录固定版本、摘要和本地路径的安装状态。"""

        document = self.load()
        document["installations"] = [
            item for item in document["installations"]
            if str(item.get("name") or "") != release.name
        ]
        document["installations"].append(
            {
                "name": release.name,
                "version": release.version,
                "sha256": release.sha256,
                "path": str(Path(path).resolve()),
                "enabled": bool(enabled),
                "installed_at": _utc_now(),
                "execution": "isolated-worker",
            }
        )
        document["installations"].sort(key=lambda item: str(item.get("name") or ""))
        document["audit"].append(
            {"at": _utc_now(), "action": "install", "name": release.name, "version": release.version}
        )
        self._write(document)

    def remove_installation(self, name: str) -> dict[str, Any]:
        """删除本地安装记录并返回被删除的条目。"""

        document = self.load()
        removed = next(
            (item for item in document["installations"] if item.get("name") == name),
            None,
        )
        if removed is None:
            raise KeyError(f"plugin installation not found: {name}")
        document["installations"] = [
            item for item in document["installations"] if item.get("name") != name
        ]
        document["audit"].append(
            {"at": _utc_now(), "action": "uninstall", "name": name}
        )
        self._write(document)
        return dict(removed)


__all__ = [
    "COMMUNITY_REGISTRY_SCHEMA",
    "REVIEW_STATUSES",
    "CommunityRegistry",
    "CommunityRelease",
]
