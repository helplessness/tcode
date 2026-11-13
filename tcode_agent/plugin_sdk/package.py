"""Python Plugin SDK v1 的可复现打包、摘要和 Ed25519 签名工具。"""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


PACKAGE_SUFFIX = ".tcode-plugin"
PACKAGE_METADATA = "TCODE-PACKAGE.json"
_IGNORED_PARTS = frozenset(
    {".git", ".pytest_cache", "__pycache__", "build", "dist"}
)


@dataclass(frozen=True, slots=True)
class PackageArtifact:
    """保存已生成插件包的不可变元数据。"""

    path: str
    sha256: str
    size: int
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """返回适合 CLI 和注册表存储的字典。"""

        return asdict(self)


def sha256_file(path: str | Path) -> str:
    """以流式读取方式返回文件的 SHA-256 十六进制摘要。"""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_files(root: Path) -> list[Path]:
    """返回稳定排序且已排除构建垃圾的包内文件。"""

    files: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_file() and path.name != PACKAGE_METADATA and path.suffix != ".pyc":
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _zip_info(relative_path: str, *, executable: bool) -> zipfile.ZipInfo:
    """构造一个时间戳和权限均已固定的 ZIP 条目。"""

    info = zipfile.ZipInfo(relative_path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def pack_plugin(
    plugin_root: str | Path,
    output: str | Path | None = None,
) -> PackageArtifact:
    """生成内容和时间戳都可复现的 ``.tcode-plugin`` 包。"""

    root = Path(plugin_root).resolve()
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    name = str(manifest["name"])
    version = str(manifest["version"])
    target = Path(output) if output is not None else root.parent / f"{name}-{version}{PACKAGE_SUFFIX}"
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    files = _package_files(root)
    package_document = {
        "schema_version": 1,
        "name": name,
        "version": version,
        "files": [path.relative_to(root).as_posix() for path in files],
    }
    with zipfile.ZipFile(target, "w") as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            archive.writestr(
                _zip_info(relative, executable=executable),
                path.read_bytes(),
            )
        archive.writestr(
            _zip_info(PACKAGE_METADATA, executable=False),
            json.dumps(
                package_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    return PackageArtifact(
        path=str(target),
        sha256=sha256_file(target),
        size=target.stat().st_size,
        files=tuple(package_document["files"]),
    )


def inspect_package(package_path: str | Path) -> dict[str, object]:
    """校验归档路径、重复项和元数据后返回包摘要。"""

    path = Path(package_path).resolve()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("plugin package contains duplicate paths")
        for info in archive.infolist():
            relative = PurePosixPath(info.filename)
            if relative.is_absolute() or ".." in relative.parts or "\\" in info.filename:
                raise ValueError(f"unsafe plugin package path: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"plugin package cannot contain symlinks: {info.filename}")
        if PACKAGE_METADATA not in names or "plugin.json" not in names:
            raise ValueError("plugin package is missing metadata or plugin.json")
        metadata = json.loads(archive.read(PACKAGE_METADATA).decode("utf-8"))
        manifest = json.loads(archive.read("plugin.json").decode("utf-8"))
        declared = set(metadata.get("files") or [])
        actual = set(names) - {PACKAGE_METADATA}
        if declared != actual:
            raise ValueError("plugin package file inventory does not match archive")
        if (
            metadata.get("name") != manifest.get("name")
            or metadata.get("version") != manifest.get("version")
        ):
            raise ValueError("plugin package metadata does not match manifest")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
        "name": str(metadata["name"]),
        "version": str(metadata["version"]),
        "files": sorted(actual),
    }


def unpack_package(package_path: str | Path, destination: str | Path) -> Path:
    """在完成路径安全校验后解包到一个不存在的目录。"""

    inspect_package(package_path)
    target = Path(destination).resolve()
    if target.exists():
        raise FileExistsError(str(target))
    target.mkdir(parents=True)
    with zipfile.ZipFile(Path(package_path).resolve()) as archive:
        for info in archive.infolist():
            if info.filename == PACKAGE_METADATA:
                continue
            output = (target / PurePosixPath(info.filename)).resolve()
            output.relative_to(target)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.read(info.filename))
            mode = info.external_attr >> 16
            output.chmod(mode & 0o777 or 0o644)
    return target


def generate_signing_key(private_key_path: str | Path, public_key_path: str | Path) -> None:
    """生成一对 PEM 格式 Ed25519 发布签名密钥。"""

    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_path = Path(private_key_path).resolve()
    public_path = Path(public_key_path).resolve()
    if private_path.exists() or public_path.exists():
        raise FileExistsError("signing key target already exists")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(private_bytes)
    private_path.chmod(0o600)
    public_path.write_bytes(public_bytes)


def public_key_pem(private_key_path: str | Path) -> str:
    """从 PEM 私钥派生可发布的 PEM 公钥文本。"""

    private_key = serialization.load_pem_private_key(
        Path(private_key_path).read_bytes(),
        password=None,
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("signing key must be Ed25519")
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def sign_digest(digest: str, private_key_path: str | Path) -> str:
    """使用 Ed25519 对小写 SHA-256 摘要文本生成 Base64 签名。"""

    private_key = serialization.load_pem_private_key(
        Path(private_key_path).read_bytes(),
        password=None,
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("signing key must be Ed25519")
    signature = private_key.sign(str(digest).lower().encode("ascii"))
    return base64.b64encode(signature).decode("ascii")


def verify_digest_signature(digest: str, signature: str, public_key: str) -> bool:
    """验证 Ed25519 摘要签名，任何解析或验签失败均返回假。"""

    try:
        key = serialization.load_pem_public_key(public_key.encode("ascii"))
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(
            base64.b64decode(signature.encode("ascii"), validate=True),
            str(digest).lower().encode("ascii"),
        )
        return True
    except Exception:
        return False


def public_key_fingerprint(public_key: str) -> str:
    """返回公钥 DER 内容的短 SHA-256 指纹。"""

    key = serialization.load_pem_public_key(public_key.encode("ascii"))
    der = key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


__all__ = [
    "PACKAGE_METADATA",
    "PACKAGE_SUFFIX",
    "PackageArtifact",
    "generate_signing_key",
    "inspect_package",
    "pack_plugin",
    "public_key_fingerprint",
    "public_key_pem",
    "sha256_file",
    "sign_digest",
    "unpack_package",
    "verify_digest_signature",
]
