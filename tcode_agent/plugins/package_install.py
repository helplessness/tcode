"""安装用户导入的隔离插件包；静态校验，不执行插件代码。"""
from pathlib import Path, PurePosixPath
import json
import re
import shutil
import stat
import tempfile
import zipfile

from ..atomic_storage import file_transaction
from ..plugin_sdk.package import inspect_package, unpack_package
from ..plugin_sdk.testing import contract_test_plugin
from .manifest import load_manifest

MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_PACKAGE_FILES = 2000


def delete_package(manager, name: str) -> dict:
    """Delete only an unloaded workspace import, never jobs or result directories."""
    from .registry import PluginRegistry
    from ..runtime.workspace_registry import registry_workspace
    from .async_targets import store
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
        raise ValueError('无效插件名称')
    workspace = Path(manager.workdir).resolve()
    base = workspace / '.tcode' / 'installed-plugins'
    target = base / name
    config = Path(manager.config_path)
    if config != workspace / '.tcode' / 'plugins.json':
        raise ValueError('只允许删除当前工作区导入的插件')
    for path in (workspace / '.tcode', base, target, config,
                 Path(str(config) + '.lock'), base / 'install.lock',
                 workspace / '.tcode' / 'plugin-async-targets.sqlite'):
        if path.is_symlink():
            raise ValueError('删除目标或配置不能是符号链接')
    if not target.is_dir():
        raise FileNotFoundError('当前工作区没有此插件的安装目录')
    with file_transaction(base / 'install'), file_transaction(config):
        with registry_workspace(workspace):
            if PluginRegistry.get(name) is not None:
                raise ValueError('请先卸载插件，再删除安装文件')
        original = manager._read_json_file(config)
        entries = original.get('plugins', [])
        matches = [p for p in entries if p.get('name') == name]
        if len(matches) != 1 or Path(matches[0].get('path', '')).resolve() != target:
            raise ValueError('插件登记路径与当前工作区安装目录不一致')
        updated = {**original, 'plugins': [p for p in entries if p.get('name') != name]}
        with store(workspace) as db:
            rows = db.execute('SELECT data FROM bindings WHERE plugin=?', (name,)).fetchall()
            if any(json.loads(row[0]).get('active') for row in rows):
                raise ValueError('请先停用此插件的异步目标，再删除插件')
            # Quarantine allows a failed configuration write to restore the package.
            with tempfile.TemporaryDirectory(prefix='.delete-', dir=base) as staging:
                staged = Path(staging) / name
                target.rename(staged)
                try:
                    db.execute('DELETE FROM bindings WHERE plugin=?', (name,))
                    manager._write_json_file(config, updated)
                    db.commit()
                except Exception:
                    db.rollback()
                    staged.rename(target)
                    manager._write_json_file(config, original)
                    raise
                shutil.rmtree(staged)
    return {'name': name, 'results_preserved': True}


def install_package(manager, package: Path) -> dict:
    """校验后发布到当前工作区，登记为禁用；同名插件不覆盖。"""
    with zipfile.ZipFile(package) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_PACKAGE_FILES or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
            raise ValueError('插件包解压后过大或文件过多')
        canonical = set()
        for info in infos:
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if (not info.filename or path.as_posix() != info.filename or ':' in info.filename
                    or info.flag_bits & 1 or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError('插件包包含不支持的路径或文件类型')
            key = info.filename.casefold()
            if key in canonical:
                raise ValueError('插件包包含重复或大小写冲突路径')
            canonical.add(key)
        for name in canonical:
            if any(parent.as_posix() in canonical for parent in PurePosixPath(name).parents if parent.as_posix() != '.'):
                raise ValueError('插件包包含文件与目录冲突路径')
    info = inspect_package(package)
    name = str(info['name'])
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
        raise ValueError('插件名称必须为小写字母、数字、连字符或下划线，且不超过 64 字符')
    workspace = Path(manager.workdir).resolve()
    base = workspace / '.tcode' / 'installed-plugins'
    for directory in (workspace / '.tcode', base):
        if directory.is_symlink():
            raise ValueError('插件安装目录不能是符号链接')
        directory.mkdir(exist_ok=True, mode=0o700)
    config_path = Path(manager.config_path)
    if not config_path.resolve().is_relative_to(workspace) or any(path.is_symlink() for path in (
        config_path, Path(str(config_path) + '.lock'), base / 'install.lock',
    )):
        raise ValueError('插件登记文件必须位于当前工作区，且不能是符号链接')
    with file_transaction(base / 'install'):
        target = base / name
        configured = manager._load_configured_plugins()
        if target.exists() or target.is_symlink() or any(str(p.get('name', '')).lower() == name for p in configured):
            raise FileExistsError('同名插件已存在；为避免覆盖，请先处理旧插件或使用新的插件名称')
        with tempfile.TemporaryDirectory(prefix='.stage-', dir=base) as staging:
            root = Path(staging) / 'plugin'
            unpack_package(package, root)
            errors = contract_test_plugin(root)
            if errors:
                raise ValueError('插件校验失败：' + '; '.join(error.message for error in errors))
            manifest = load_manifest(root)
            if manifest.async_targets:
                from .async_targets import declarations
                declarations(root, manifest.async_targets)
            # Imported archives never establish publisher identity or host trust.
            document = json.loads((root / 'plugin.json').read_text())
            document['requestedTrust'] = 'isolated'
            (root / 'plugin.json').write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n')
            root.rename(target)
            try:
                manager.persist_plugin_enabled_state(name=name, path=str(target), enabled=False, config={})
            except Exception:
                shutil.rmtree(target)
                raise
    return {'name': name, 'version': info['version'], 'path': str(target),
            'sha256': info['sha256'], 'status': 'installed-disabled',
            'async_targets': bool(manifest.async_targets), 'publisher_verified': False}
