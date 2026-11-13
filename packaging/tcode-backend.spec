from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).parent
WEB_DIST = ROOT / "web" / "dist"


def tree(source: Path, destination: str, *, excluded=()):
    result = []
    for path in source.rglob("*"):
        if not path.is_file() or path.name == ".DS_Store":
            continue
        relative = path.relative_to(source)
        if any(part in excluded for part in relative.parts):
            continue
        result.append((str(path), str(Path(destination) / relative.parent)))
    return result


datas = [
    item
    for item in collect_data_files("tcode_agent", include_py_files=False)
    if "/server/static/" not in item[0]
]
datas += tree(WEB_DIST, "tcode_agent/server/static")
datas += tree(ROOT / "plugins", "plugins", excluded={"__pycache__", ".cache", "tests"})
datas += tree(ROOT / "config" / "templates", "config/templates")
datas += tree(ROOT / ".claude" / "skills" / "anatomy", ".claude/skills/anatomy",
              excluded={"__pycache__", "node_modules"})
datas += [
    (str(ROOT / "scripts" / name), "scripts")
    for name in (
        "runtime_env.py",
        "tracker_scope.py",
        "shared_tracker_symphony_sidecar.py",
    )
]

hiddenimports = collect_submodules("tcode_agent")

a = Analysis(
    [str(ROOT / "packaging" / "tcode_backend_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "tkinter", "tmcp"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tcode-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="tcode-backend",
)
