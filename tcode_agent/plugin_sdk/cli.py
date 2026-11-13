"""TCode Python Plugin SDK v1 的官方开发者命令行。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any, Sequence

from .community import CommunityRegistry, REVIEW_STATUSES
from .package import (
    generate_signing_key,
    inspect_package,
    pack_plugin,
    unpack_package,
)
from .testing import contract_test_plugin, doctor_plugin, validate_plugin_package


_PLUGIN_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _template_root() -> Path:
    """返回 SDK 随包发布的 Python 插件项目模板。"""

    return Path(__file__).with_name("templates") / "python-plugin"


def _default_registry() -> Path:
    """返回当前项目内默认的自有社区注册表路径。"""

    return Path.cwd() / ".tcode" / "community" / "registry.json"


def _print_json(value: Any) -> None:
    """以稳定 UTF-8 JSON 格式输出 CLI 结果。"""

    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _create(args: argparse.Namespace) -> int:
    """从官方模板创建一个默认隔离的 Python 插件项目。"""

    name = str(args.name).strip().lower()
    if not _PLUGIN_NAME_PATTERN.fullmatch(name):
        raise ValueError("plugin name must use lowercase letters, digits, '.', '_' or '-'")
    target = Path(args.path or name).resolve()
    if target.exists():
        raise FileExistsError(str(target))
    shutil.copytree(
        _template_root(),
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    replacements = {
        "{{plugin_name}}": name,
        "{{service_name}}": name.replace("-", "_").replace(".", "_"),
        "{{tool_name}}": f"{name.replace('-', '_').replace('.', '_')}_echo",
    }
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        text = path.read_text(encoding="utf-8")
        for source, destination in replacements.items():
            text = text.replace(source, destination)
        path.write_text(text, encoding="utf-8")
    _print_json({"status": "created", "name": name, "path": str(target)})
    return 0


def _validate(args: argparse.Namespace) -> int:
    """静态校验插件包，绝不执行入口代码。"""

    errors = validate_plugin_package(args.path)
    _print_json({"status": "pass" if not errors else "failed", "errors": errors})
    return 0 if not errors else 1


def _test(args: argparse.Namespace) -> int:
    """运行 Manifest、语法和公开导入合同测试。"""

    diagnostics = contract_test_plugin(args.path)
    _print_json(
        {
            "status": "pass" if not diagnostics else "failed",
            "diagnostics": [item.to_dict() for item in diagnostics],
        }
    )
    return 0 if not diagnostics else 1


def _doctor(args: argparse.Namespace) -> int:
    """输出不含配置值和 Secret 的机器可读诊断报告。"""

    report = doctor_plugin(args.path)
    _print_json(report)
    return 0 if report["status"] == "pass" else 1


def _pack(args: argparse.Namespace) -> int:
    """在合同通过后生成可复现插件包及 SHA-256 摘要。"""

    diagnostics = contract_test_plugin(args.path)
    if diagnostics:
        _print_json(
            {"status": "failed", "diagnostics": [item.to_dict() for item in diagnostics]}
        )
        return 1
    artifact = pack_plugin(args.path, args.output)
    _print_json({"status": "packed", **artifact.to_dict()})
    return 0


def _keygen(args: argparse.Namespace) -> int:
    """生成用于社区发布的 Ed25519 签名密钥对。"""

    generate_signing_key(args.private_key, args.public_key)
    _print_json(
        {
            "status": "created",
            "private_key": str(Path(args.private_key).resolve()),
            "public_key": str(Path(args.public_key).resolve()),
        }
    )
    return 0


def _publish(args: argparse.Namespace) -> int:
    """把已打包版本提交到自有注册表，但不自动安装或授权。"""

    registry = CommunityRegistry(args.registry)
    release = registry.publish(
        args.package,
        signer=args.signer,
        private_key_path=args.signing_key,
    )
    _print_json({"status": "published", "release": release.to_dict()})
    return 0


def _review(args: argparse.Namespace) -> int:
    """以显式审核人身份更新社区版本治理状态。"""

    registry = CommunityRegistry(args.registry)
    release = registry.review(
        args.name,
        args.version,
        status=args.status,
        reviewer=args.reviewer,
        note=args.note,
    )
    _print_json({"status": "reviewed", "release": release.to_dict()})
    return 0


def _install(args: argparse.Namespace) -> int:
    """验证固定版本与摘要后安装，默认保持禁用和隔离。"""

    registry = CommunityRegistry(args.registry)
    release = registry.get(args.name, args.version)
    if release.review_status in {"quarantined", "revoked"}:
        raise PermissionError(
            f"release cannot be installed with status={release.review_status}"
        )
    valid, errors = registry.verify_release(release)
    if not valid:
        raise ValueError("; ".join(errors))
    package = inspect_package(release.package)
    if package["sha256"] != release.sha256:
        raise ValueError("registry digest does not match package")
    plugin_home = Path(args.plugin_home).resolve()
    target = plugin_home / release.name
    unpack_package(release.package, target)
    try:
        manifest_path = target / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if release.review_status not in {"official", "verified"}:
            manifest["requestedTrust"] = "isolated"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        registry.record_installation(release, path=target, enabled=False)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    _print_json(
        {
            "status": "installed-disabled",
            "name": release.name,
            "version": release.version,
            "path": str(target),
            "execution": (
                "trusted-candidate"
                if release.review_status in {"official", "verified"}
                else "isolated-worker"
            ),
        }
    )
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    """删除注册表精确记录的安装目录，不触及其他路径。"""

    registry = CommunityRegistry(args.registry)
    document = registry.load()
    installation = next(
        (
            dict(item)
            for item in document["installations"]
            if str(item.get("name") or "") == args.name
        ),
        None,
    )
    if installation is None:
        raise KeyError(f"plugin installation not found: {args.name}")
    target = Path(str(installation["path"])).resolve()
    plugin_home = Path(args.plugin_home).resolve()
    target.relative_to(plugin_home)
    if target.name != args.name:
        raise ValueError("installation path does not match plugin name")
    if not target.is_dir():
        raise FileNotFoundError(
            f"recorded plugin directory is missing or not a directory: {target}"
        )
    shutil.rmtree(target)
    registry.remove_installation(args.name)
    _print_json({"status": "uninstalled", "name": args.name, "path": str(target)})
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """构建一个不含远程自动安装的 SDK v1 命令行解析器。"""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="从官方模板创建插件")
    create.add_argument("name")
    create.add_argument("--path")
    create.set_defaults(handler=_create)

    for command, handler, help_text in (
        ("validate", _validate, "静态校验插件包"),
        ("test", _test, "运行 SDK 合同测试"),
        ("doctor", _doctor, "输出脱敏诊断"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("path")
        command_parser.set_defaults(handler=handler)

    pack = subparsers.add_parser("pack", help="生成可复现插件包")
    pack.add_argument("path")
    pack.add_argument("--output")
    pack.set_defaults(handler=_pack)

    keygen = subparsers.add_parser("keygen", help="生成 Ed25519 签名密钥")
    keygen.add_argument("--private-key", required=True)
    keygen.add_argument("--public-key", required=True)
    keygen.set_defaults(handler=_keygen)

    publish = subparsers.add_parser("publish", help="提交到自有社区注册表")
    publish.add_argument("package")
    publish.add_argument("--registry", default=str(_default_registry()))
    publish.add_argument("--signer", default="")
    publish.add_argument("--signing-key")
    publish.set_defaults(handler=_publish)

    review = subparsers.add_parser("review", help="记录显式审核结论")
    review.add_argument("name")
    review.add_argument("version")
    review.add_argument("--status", required=True, choices=sorted(REVIEW_STATUSES))
    review.add_argument("--reviewer", required=True)
    review.add_argument("--note", default="")
    review.add_argument("--registry", default=str(_default_registry()))
    review.set_defaults(handler=_review)

    install = subparsers.add_parser("install", help="安装固定版本并默认禁用")
    install.add_argument("name")
    install.add_argument("version")
    install.add_argument("--registry", default=str(_default_registry()))
    install.add_argument("--plugin-home", default=str(Path.cwd() / ".tcode" / "plugins"))
    install.set_defaults(handler=_install)

    uninstall = subparsers.add_parser("uninstall", help="精确卸载本地社区插件")
    uninstall.add_argument("name")
    uninstall.add_argument("--registry", default=str(_default_registry()))
    uninstall.add_argument("--plugin-home", default=str(Path.cwd() / ".tcode" / "plugins"))
    uninstall.set_defaults(handler=_uninstall)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行 SDK CLI，并把可预期失败转为无堆栈的稳定诊断。"""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception as exc:
        _print_json(
            {
                "status": "failed",
                "error": type(exc).__name__,
                "message": str(exc),
            }
        )
        return 1


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
