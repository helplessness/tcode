"""Owner-scoped, transactional SVG canvases. Generated SVG is data, never code."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..sqlite_runtime import connect

NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", NS)
ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,95}$")
TAGS = {"svg", "g", "rect", "circle", "ellipse", "line", "polyline", "polygon", "path", "text", "tspan", "title", "desc"}
ATTRS = {
    "id", "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "d", "points", "fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin",
    "opacity", "fill-opacity", "stroke-opacity", "font-size", "font-family", "font-weight", "text-anchor",
    "dominant-baseline", "transform", "fill-rule",
}
COLORS = re.compile(r"^(?:#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?|none|transparent|[a-zA-Z]{1,24})$")
NUMBERS = re.compile(r"^[0-9eE+.,\s-]+$")


class CanvasError(ValueError):
    def __init__(self, code: str, status: int = 422, *, details: dict | None = None):
        self.code, self.status = code, status
        self.details = details
        super().__init__(code)


class CanvasContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    type: Literal["canvas"]
    schema_id: Literal["tcode.canvas.v1"] = Field(alias="schema")
    kind: Literal["svg"]
    canvas_id: str = Field(pattern=r"^canvas_[0-9a-f]{32}$")
    revision_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")
    session_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=256)
    fallback: str = Field(max_length=512)


class Explosion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    x: float = Field(allow_inf_nan=False, ge=-1000, le=1000)
    y: float = Field(allow_inf_nan=False, ge=-1000, le=1000)
    order: int = Field(default=0, ge=0, le=11)


class CanvasAnimation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    duration_seconds: float = Field(default=6.0, ge=1, le=60, allow_inf_nan=False)
    mode: Literal['explode', 'assemble', 'roundtrip'] = 'roundtrip'


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    element_id: str = Field(pattern=ID.pattern)
    label: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4000)
    explode: Explosion | None = None
    illustration: Literal['isometric'] | None = None
    animation: CanvasAnimation | None = None
    solid_scene: dict | None = None

    @field_validator('solid_scene')
    @classmethod
    def validate_solid_scene(cls, value):
        if value is not None:
            from .isometric import IsometricScene
            return IsometricScene.model_validate(value).model_dump(exclude_none=True)
        return value


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    op: Literal["set_attribute", "set_text", "set_description", "set_label"]
    element_id: str = Field(pattern=ID.pattern)
    name: str = Field(default="", max_length=40)
    value: str = Field(max_length=4000)


class ExplosionOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    op: Literal['set_explode']
    element_id: str = Field(pattern=ID.pattern)
    explode: Explosion | None


class AnimationOperation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    op: Literal['set_animation']
    element_id: str = Field(pattern=ID.pattern)
    animation: CanvasAnimation | None


class SolidDirectionOperation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    op: Literal['set_solid_direction']
    element_id: str = Field(pattern=ID.pattern)
    vector: list[float] = Field(min_length=3,max_length=3)
    order: int = Field(ge=0,le=11)

    @field_validator('vector')
    @classmethod
    def bounded_vector(cls, value):
        from .isometric import explosion
        explosion(value, 0)
        return value


class SolidTranslationOperation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    op: Literal['translate_solid']
    element_id: str = Field(pattern=ID.pattern)
    vector: list[float] = Field(min_length=3, max_length=3)

    @field_validator('vector')
    @classmethod
    def bounded_vector(cls, value):
        if any(not math.isfinite(v) or abs(v) > 200 for v in value):
            raise ValueError('invalid_solid_translation')
        return value


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_revision_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")
    request_id: str = Field(min_length=1, max_length=128)
    element_ids: list[str] = Field(min_length=1, max_length=100)
    operations: list[Annotated[Operation | ExplosionOperation | AnimationOperation | SolidDirectionOperation | SolidTranslationOperation, Field(discriminator='op')]] = Field(min_length=1, max_length=100)
    selection_id: str = Field(default="", max_length=100)


class Restore(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_revision_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")
    request_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")


def enabled() -> bool:
    import os
    return os.environ.get("TCODE_INTELLIGENT_UI_ENABLED", "1").lower() not in {"0", "false", "off"}


def canvas_style() -> Literal["clay", "manual"]:
    import os
    value = os.environ.get("TCODE_INTELLIGENT_UI_STYLE", "clay").strip().lower()
    if value not in {"clay", "manual"}:
        raise CanvasError("invalid_deployment_canvas_style")
    return value


def _tag(node: ET.Element) -> str:
    return node.tag.removeprefix("{" + NS + "}")


def _attribute(name: str, value: str) -> None:
    if name not in ATTRS or len(value) > 32_000:
        raise CanvasError("unsupported_attribute")
    if name == "id":
        valid = bool(ID.fullmatch(value))
    elif name in {"fill", "stroke"}:
        valid = bool(COLORS.fullmatch(value))
    elif name == "transform":
        valid = bool(re.fullmatch(r"translate\(\s*[-+0-9.eE]+(?:[ ,]+[-+0-9.eE]+)?\s*\)", value))
        if valid:
            values = re.findall(r"[-+0-9.eE]+", value.removeprefix("translate"))
            try:
                valid = all(math.isfinite(float(v)) and abs(float(v)) <= 100_000 for v in values)
            except ValueError:
                valid = False
    elif name == "d":
        valid = bool(re.fullmatch(r"[MmLlHhVvCcSsQqTtAaZz0-9eE+.,\s-]+", value))
    elif name in {"font-family", "font-weight", "text-anchor", "dominant-baseline", "stroke-linecap", "stroke-linejoin", "fill-rule"}:
        valid = bool(re.fullmatch(r"[A-Za-z0-9 ,_-]{1,100}", value))
    else:
        valid = bool(NUMBERS.fullmatch(value))
        if valid:
            try:
                valid = all(math.isfinite(float(v)) and abs(float(v)) <= 100_000
                            for v in re.split(r"[\s,]+", value.strip()) if v)
            except ValueError:
                valid = False
    if not valid:
        raise CanvasError("invalid_attribute_value")


def validate_svg(source: str) -> tuple[str, ET.Element]:
    if not isinstance(source, str) or not source or len(source.encode()) > 1_048_576:
        raise CanvasError("source_size_limit", 413)
    # Reject DTD/entities before the stdlib parser; all external and active SVG
    # features are rejected rather than silently altering generated content.
    if "<!" in source or re.search(r"<\?(?!xml\s)", source, re.I):
        raise CanvasError("unsupported_xml_declaration")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise CanvasError("invalid_svg") from exc
    if root.tag != "{" + NS + "}svg":
        raise CanvasError("svg_namespace_required")
    seen: set[str] = set()
    nodes = [(root, 0)]
    count = 0
    while nodes:
        node, depth = nodes.pop()
        count += 1
        if count > 2000 or depth > 32:
            raise CanvasError("svg_complexity_limit", 413)
        if node.tag not in {"{" + NS + "}" + name for name in TAGS}:
            raise CanvasError("unsupported_element")
        for name, value in node.attrib.items():
            _attribute(name, value)
        existing = node.get("id")
        if existing:
            if existing in seen:
                raise CanvasError("duplicate_element_id")
            seen.add(existing)
        nodes.extend((child, depth + 1) for child in node)
    # Stable IDs are added once at creation, and preserved by every edit.
    index = 0
    for node in root.iter():
        if not node.get("id"):
            while f"element_{index}" in seen:
                index += 1
            node.set("id", f"element_{index}")
            seen.add(f"element_{index}")
            index += 1
    return ET.tostring(root, encoding="unicode"), root


def canvas_block(value: dict) -> dict:
    return {"type": "canvas", "schema": "tcode.canvas.v1", "kind": "svg",
            "canvas_id": value["canvas_id"], "revision_id": value["revision_id"],
            "session_id": value["session_id"], "title": value["title"],
            "fallback": "可交互 SVG：" + value["title"]}


def normalize_canvas_block(value: dict) -> dict | None:
    if (value.get("schema") != "tcode.canvas.v1" or value.get("kind") != "svg"
        or not re.fullmatch(r"canvas_[0-9a-f]{32}", str(value.get("canvas_id", "")))
        or not re.fullmatch(r"rev_[0-9a-f]{32}", str(value.get("revision_id", "")))
        or not isinstance(value.get("session_id"), str) or not 0 < len(value["session_id"]) <= 200
        or not isinstance(value.get("title"), str) or not 0 < len(value["title"]) <= 256):
        return None
    return canvas_block(value)


def validate_solid_bindings(root, parts):
    from .isometric import IsometricScene, render_scene
    for part in parts:
        if part.solid_scene is None:
            continue
        scene = IsometricScene.model_validate(part.solid_scene)
        expected, expected_parts = render_scene(scene)
        # Metadata is executable geometry input. Only exact canonical kit SVG is
        # eligible for 3D; arbitrary SVG never gains a misleading solid model.
        if part.element_id != scene.id or part.illustration != 'isometric' or validate_svg(expected)[0] != validate_svg(ET.tostring(root, encoding='unicode'))[0]:
            raise CanvasError('solid_scene_source_mismatch')
        actual = {p.element_id:p for p in parts}
        for canonical in expected_parts:
            match = actual.get(canonical.element_id)
            if match is None or match.explode != canonical.explode or match.animation != canonical.animation or match.label != canonical.label or match.description != canonical.description:
                raise CanvasError('solid_scene_part_mismatch')


def validate_explosions(root: ET.Element, parts: list[Part]):
    """A direct sibling set uses either a complete explicit plan or legacy layout."""
    if not any(p.explode is not None or p.animation is not None for p in parts):
        validate_solid_bindings(root, parts)
        return
    parents = {child: parent for parent in root.iter() for child in parent}
    registered = {p.element_id for p in parts}
    siblings: dict[str, list[Part]] = {}
    indexed = {n.get('id'): n for n in root.iter()}
    for part in parts:
        node = indexed.get(part.element_id)
        if node is None:
            raise CanvasError('invalid_part_binding')
        parent = parents.get(node)
        while parent is not None and parent.get('id') not in registered:
            parent = parents.get(parent)
        key = parent.get('id') if parent is not None else ''
        siblings.setdefault(key, []).append(part)
    for group in siblings.values():
        planned = [p for p in group if p.explode is not None]
        if planned and len(planned) != len(group):
            raise CanvasError('incomplete_explosion_plan')
    for part in parts:
        if part.animation is not None:
            children = siblings.get(part.element_id, [])
            if part.illustration != 'isometric' or not 2 <= len(children) <= 12 or any(p.explode is None for p in children):
                raise CanvasError('animation_requires_isometric_explosion_plan')
    validate_solid_bindings(root, parts)


class CanvasStore:
    """Source and metadata in ONE SQLite transaction, no orphan-file recovery."""
    def __init__(self, workdir: str, *, tenant_id: str, user_id: str, workspace_id: str):
        if not workdir or not all((tenant_id, user_id, workspace_id)):
            raise CanvasError("owner_context_required", 403)
        from ..workspace_lifecycle import assert_managed_workspace_creation_allowed
        assert_managed_workspace_creation_allowed(workdir)
        root = Path(workdir).resolve(strict=True)
        if not root.is_dir():
            raise CanvasError("workspace_unavailable", 404)
        directory = root / ".tcode" / "intelligent-ui"
        for path in (root / ".tcode", directory, *(directory / name for name in
                     ("canvases.sqlite", "canvases.sqlite-journal", "canvases.sqlite-wal", "canvases.sqlite-shm"))):
            if path.is_symlink():
                raise CanvasError("unsafe_storage_path", 403)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "canvases.sqlite"
        self.owner = (tenant_id, user_id, workspace_id)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS canvases (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, user TEXT NOT NULL,
                    workspace TEXT NOT NULL, session TEXT NOT NULL, title TEXT NOT NULL,
                    current TEXT NOT NULL, parts TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS canvas_revisions (
                    id TEXT PRIMARY KEY, canvas TEXT NOT NULL, parent TEXT,
                    source TEXT NOT NULL, digest TEXT NOT NULL, created TEXT NOT NULL,
                    actor TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS canvas_revisions_canvas ON canvas_revisions(canvas);
                CREATE TABLE IF NOT EXISTS canvas_requests (
                    canvas TEXT NOT NULL, request TEXT NOT NULL, payload TEXT NOT NULL,
                    result TEXT NOT NULL, PRIMARY KEY(canvas, request)
                );
                CREATE TABLE IF NOT EXISTS canvas_selections (
                    id TEXT PRIMARY KEY, canvas TEXT NOT NULL, session TEXT NOT NULL,
                    revision TEXT NOT NULL, elements TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intelligent_ui_revision_trash (
                    kind TEXT NOT NULL, object_id TEXT NOT NULL, revision TEXT NOT NULL,
                    deleted_at TEXT NOT NULL, PRIMARY KEY(kind, object_id, revision)
                );
            """)
            # Existing canvases had one immutable parts list. Snapshot it into
            # old revisions before allowing versioned description edits.
            db.execute("BEGIN IMMEDIATE")
            if "parts" not in {r["name"] for r in db.execute("PRAGMA table_info(canvas_revisions)")}:
                db.execute("ALTER TABLE canvas_revisions ADD COLUMN parts TEXT")
                db.execute("UPDATE canvas_revisions SET parts=(SELECT parts FROM canvases WHERE id=canvas)")
            if "style" not in {r["name"] for r in db.execute("PRAGMA table_info(canvases)")}:
                db.execute("ALTER TABLE canvases ADD COLUMN style TEXT NOT NULL DEFAULT 'manual'")

    def _connect(self):
        db = connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _owned(self, db, canvas_id):
        row = db.execute("SELECT * FROM canvases WHERE id=? AND tenant=? AND user=? AND workspace=?",
                         (canvas_id, *self.owner)).fetchone()
        if row is None:
            raise CanvasError("canvas_not_found", 404)
        return row

    @staticmethod
    def _revision(db, canvas_id, revision_id):
        row = db.execute("SELECT * FROM canvas_revisions WHERE id=? AND canvas=?", (revision_id, canvas_id)).fetchone()
        if row is None:
            raise CanvasError("revision_not_found", 404)
        return row

    @staticmethod
    def _insert_revision(db, canvas_id, parent, source, actor, parts):
        revision = "rev_" + uuid.uuid4().hex
        digest = hashlib.sha256(source.encode()).hexdigest()
        db.execute("INSERT INTO canvas_revisions(id,canvas,parent,source,digest,created,actor,parts) VALUES(?,?,?,?,?,?,?,?)", (
            revision, canvas_id, parent, source, digest, datetime.now(timezone.utc).isoformat(), actor, parts))
        return revision, digest

    def create(self, *, session_id: str, title: str, source: str, parts: list[Part], request_id: str,
               style: Literal["clay", "manual"] | None = None) -> dict:
        style = style or canvas_style()
        source, root = validate_svg(source)
        if not title.strip() or len(title) > 256 or not session_id or len(parts) > 100:
            raise CanvasError("invalid_canvas_metadata")
        elements = {node.get("id") for node in root.iter()}
        if any(part.element_id not in elements for part in parts) or len({p.element_id for p in parts}) != len(parts):
            raise CanvasError("invalid_part_binding")
        validate_explosions(root, parts)
        if style not in {"clay", "manual"}:
            raise CanvasError("invalid_canvas_style")
        from .diagram_design import check_layout
        quality = check_layout(root, parts)
        if quality["counts"]["error"]:
            raise CanvasError("canvas_layout_invalid", details=quality)
        payload = json.dumps([session_id, title, source, [p.model_dump(exclude_none=True) for p in parts], style], ensure_ascii=False)
        scope = "create:" + hashlib.sha256(json.dumps([*self.owner, session_id]).encode()).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._replay(db, scope, request_id, payload)
            if previous:
                return previous
            canvas_id = "canvas_" + uuid.uuid4().hex
            serialized_parts = json.dumps([p.model_dump(exclude_none=True) for p in parts], ensure_ascii=False)
            revision, _ = self._insert_revision(db, canvas_id, None, source, "agent", serialized_parts)
            db.execute("INSERT INTO canvases(id,tenant,user,workspace,session,title,current,parts,style) VALUES(?,?,?,?,?,?,?,?,?)", (
                canvas_id, *self.owner, session_id, title, revision,
                serialized_parts, style))
            result = {"canvas_id": canvas_id, "revision_id": revision, "title": title, "session_id": session_id,
                      "layout_quality": quality}
            self._record(db, scope, request_id, payload, result)
            return result

    def read(self, canvas_id: str, revision_id: str = "") -> dict:
        with closing(self._connect()) as db:
            canvas = self._owned(db, canvas_id)
            revision = self._revision(db, canvas_id, revision_id or canvas["current"])
            root = ET.fromstring(revision["source"])
            parents = {child.get("id"): parent.get("id") for parent in root.iter() for child in parent}
            from .diagram_design import check_layout
            return {"canvas_id": canvas_id, "title": canvas["title"], "session_id": canvas["session"],
                    "layout_quality": check_layout(root, json.loads(revision["parts"])),
                    "style": canvas["style"], "display_style": canvas_style(),
                    "revision_id": revision["id"], "current_revision_id": canvas["current"],
                    "parent_revision_id": self._visible_parent(db, 'canvas', canvas_id, revision["parent"]),
                    "is_deleted": self._is_deleted(db, 'canvas', canvas_id, revision['id']), "source": revision["source"],
                    "source_digest": revision["digest"], "parts": json.loads(revision["parts"]),
                    "elements": [{"element_id": n.get("id"), "tag": _tag(n),
                                  "parent_id": parents.get(n.get("id"), ""),
                                  "text": n.text or "", "fill": n.get("fill", ""),
                                  "transform": n.get("transform", "")}
                                 for n in root.iter() if _tag(n) not in {"svg", "title", "desc", "tspan"}]}

    @staticmethod
    def _is_deleted(db, kind, object_id, revision):
        return db.execute('SELECT 1 FROM intelligent_ui_revision_trash WHERE kind=? AND object_id=? AND revision=?', (kind, object_id, revision)).fetchone() is not None

    def _visible_parent(self, db, kind, object_id, parent):
        table, column = ('canvas_revisions', 'canvas') if kind == 'canvas' else ('intelligent_page_revisions', 'page')
        while parent and self._is_deleted(db, kind, object_id, parent):
            row = db.execute(f'SELECT parent FROM {table} WHERE {column}=? AND id=?', (object_id, parent)).fetchone()
            parent = row['parent'] if row else None
        return parent

    def set_revision_deleted(self, object_id: str, *, session_id: str, revision_id: str, base_revision_id: str, deleted: bool, kind: str = 'canvas') -> dict:
        if kind not in {'canvas', 'page'}:
            raise CanvasError('invalid_revision_kind')
        table, column = ('canvas_revisions', 'canvas') if kind == 'canvas' else ('intelligent_page_revisions', 'page')
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            obj = self._owned(db, object_id) if kind == 'canvas' else self._page(db, object_id)
            if obj['session'] != session_id:
                raise CanvasError('版本不属于当前会话', 403)
            if obj['current'] != base_revision_id:
                raise CanvasError('revision_conflict', 409)
            target = db.execute(f'SELECT rowid AS position FROM {table} WHERE {column}=? AND id=?', (object_id, revision_id)).fetchone()
            if target is None:
                raise CanvasError('revision_not_found', 404)
            if deleted and obj['current'] == revision_id:
                raise CanvasError('当前版本不能删除，请先恢复其他版本。', 409)
            if deleted:
                db.execute('INSERT OR IGNORE INTO intelligent_ui_revision_trash VALUES(?,?,?,?)', (kind, object_id, revision_id, datetime.now(timezone.utc).isoformat()))
            else:
                db.execute('DELETE FROM intelligent_ui_revision_trash WHERE kind=? AND object_id=? AND revision=?', (kind, object_id, revision_id))
            adjacent = db.execute(f'''SELECT r.id FROM {table} r WHERE r.{column}=? AND r.id!=?
                AND NOT EXISTS(SELECT 1 FROM intelligent_ui_revision_trash t WHERE t.kind=? AND t.object_id=? AND t.revision=r.id)
                ORDER BY CASE WHEN r.rowid<? THEN 0 ELSE 1 END, ABS(r.rowid-?) LIMIT 1''',
                (object_id, revision_id, kind, object_id, target['position'], target['position'])).fetchone()
            return {'revision_id': revision_id, 'deleted': deleted,
                    'adjacent_revision_id': adjacent['id'] if adjacent else obj['current']}

    def revisions(self, canvas_id: str, before: str = "", deleted: bool = False) -> dict:
        with closing(self._connect()) as db:
            canvas = self._owned(db, canvas_id)
            boundary = 9223372036854775807
            if before:
                self._revision(db, canvas_id, before)
                boundary = db.execute("SELECT rowid FROM canvas_revisions WHERE id=? AND canvas=?", (before, canvas_id)).fetchone()[0]
            rows = db.execute("SELECT id AS revision_id,parent AS parent_revision_id,created,actor FROM canvas_revisions r WHERE canvas=? AND rowid<? AND EXISTS(SELECT 1 FROM intelligent_ui_revision_trash t WHERE t.kind='canvas' AND t.object_id=r.canvas AND t.revision=r.id)=? ORDER BY rowid DESC LIMIT 51", (canvas_id, boundary, int(deleted))).fetchall()
            return {"current_revision_id": canvas["current"], "items": [dict(row) | {'parent_revision_id': self._visible_parent(db, 'canvas', canvas_id, row['parent_revision_id'])} for row in rows[:50]],
                    "next_before": rows[49]["revision_id"] if len(rows) > 50 else ""}

    def purge_revision(self, object_id: str, *, session_id: str, revision_id: str, base_revision_id: str, kind: str = 'canvas') -> dict:
        if kind not in {'canvas', 'page'}:
            raise CanvasError('invalid_revision_kind')
        table, column = ('canvas_revisions', 'canvas') if kind == 'canvas' else ('intelligent_page_revisions', 'page')
        with closing(self._connect()) as db, db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            obj = self._owned(db, object_id) if kind == 'canvas' else self._page(db, object_id)
            if obj['session'] != session_id:
                raise CanvasError('版本不属于当前会话', 403)
            if obj['current'] != base_revision_id:
                raise CanvasError('revision_conflict', 409)
            target = db.execute(f'SELECT parent FROM {table} WHERE {column}=? AND id=?', (object_id, revision_id)).fetchone()
            if target is None:
                raise CanvasError('revision_not_found', 404)
            if revision_id == obj['current'] or not self._is_deleted(db, kind, object_id, revision_id):
                raise CanvasError('请先将历史版本移入已删除版本，再彻底删除。', 409)
            # Reconnect children before removing the stored content and replay copies.
            db.execute(f'UPDATE {table} SET parent=? WHERE {column}=? AND parent=?', (target['parent'], object_id, revision_id))
            db.execute(f'DELETE FROM {table} WHERE {column}=? AND id=?', (object_id, revision_id))
            db.execute('DELETE FROM intelligent_ui_revision_trash WHERE kind=? AND object_id=? AND revision=?', (kind, object_id, revision_id))
            db.execute("DELETE FROM canvas_requests WHERE json_extract(result,'$.revision_id')=? OR json_extract(result,'$.previous_revision_id')=?", (revision_id, revision_id))
            if kind == 'canvas':
                db.execute('DELETE FROM canvas_selections WHERE canvas=? AND revision=?', (object_id, revision_id))
                db.execute('UPDATE canvases SET parts=(SELECT parts FROM canvas_revisions WHERE id=canvases.current AND canvas=canvases.id) WHERE id=?', (object_id,))
            return {'revision_id': revision_id, 'purged': True}

    def select(self, canvas_id: str, *, session_id: str, base_revision_id: str, element_ids: list[str]) -> dict:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            canvas = self._owned(db, canvas_id)
            if canvas["current"] != base_revision_id:
                raise CanvasError("revision_conflict", 409)
            source = self._revision(db, canvas_id, base_revision_id)["source"]
            indexed = {n.get("id"): n for n in ET.fromstring(source).iter()}
            if (not 1 <= len(element_ids) <= 100 or len(set(element_ids)) != len(element_ids)
                or any(i not in indexed or _tag(indexed[i]) in {"svg", "title", "desc"} for i in element_ids)):
                raise CanvasError("invalid_selection")
            selection_id = "selection_" + uuid.uuid4().hex
            expires = time.time() + 3600
            db.execute("DELETE FROM canvas_selections WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO canvas_selections VALUES(?,?,?,?,?,?)", (
                selection_id, canvas_id, session_id, base_revision_id, json.dumps(element_ids), expires))
            return {"selection_id": selection_id, "canvas_id": canvas_id,
                    "base_revision_id": base_revision_id, "element_ids": element_ids}

    @staticmethod
    def _replay(db, scope, request_id, payload):
        if not request_id or len(request_id) > 128:
            raise CanvasError("invalid_request_id")
        previous = db.execute("SELECT * FROM canvas_requests WHERE canvas=? AND request=?", (scope, request_id)).fetchone()
        if previous:
            if previous["payload"] != payload:
                raise CanvasError("request_id_reused", 409)
            return json.loads(previous["result"])

    @staticmethod
    def _record(db, scope, request, payload, result):
        db.execute("INSERT INTO canvas_requests VALUES(?,?,?,?)", (scope, request, payload, json.dumps(result)))

    def edit(self, canvas_id: str, edit: Edit | Restore, *, actor: str = "user", session_id: str = "") -> dict:
        payload = json.dumps([type(edit).__name__, edit.model_dump(), actor, session_id], sort_keys=True)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            canvas = self._owned(db, canvas_id)
            previous = self._replay(db, canvas_id, edit.request_id, payload)
            if previous:
                return previous
            if canvas["current"] != edit.base_revision_id:
                raise CanvasError("revision_conflict", 409)
            old = self._revision(db, canvas_id, canvas["current"])
            parts = json.loads(old["parts"])
            if isinstance(edit, Restore):
                restored = self._revision(db, canvas_id, edit.revision_id)
                source = restored["source"]
                parts = json.loads(restored["parts"])
                changed_ids = []
            else:
                root = ET.fromstring(old["source"])
                indexed = {n.get("id"): n for n in root.iter()}
                if len(set(edit.element_ids)) != len(edit.element_ids) or any(i not in indexed for i in edit.element_ids):
                    raise CanvasError("target_not_found")
                allowed = {n.get("id") for i in edit.element_ids for n in indexed[i].iter()}
                if actor == "agent":
                    selection = db.execute("SELECT * FROM canvas_selections WHERE id=? AND canvas=? AND session=?",
                                           (edit.selection_id, canvas_id, session_id)).fetchone()
                    if selection is None or selection["expires"] < time.time():
                        raise CanvasError("user_selection_required", 403)
                    if selection["revision"] != edit.base_revision_id:
                        raise CanvasError("selection_version_conflict", 409)
                    trusted = {n.get("id") for i in json.loads(selection["elements"]) for n in indexed[i].iter()}
                    if not allowed.issubset(trusted):
                        raise CanvasError("out_of_user_selection", 403)
                changed_ids = []
                for op in edit.operations:
                    if op.element_id not in allowed:
                        raise CanvasError("out_of_scope")
                    target = indexed[op.element_id]
                    if _tag(target) in {"svg", "title", "desc"}:
                        raise CanvasError("unsupported_edit_target")
                    solid_part = next((p for p in parts if p.get('solid_scene')), None)
                    if solid_part and (op.op in {'translate_solid', 'set_solid_direction', 'set_animation', 'set_label', 'set_description'} or (op.op == 'set_attribute' and op.name == 'fill')):
                        from .isometric import IsometricScene, render_scene
                        scene = IsometricScene.model_validate(solid_part['solid_scene'])
                        entities = [scene, *scene.assemblies, *(p for g in scene.assemblies for p in g.pieces)]
                        entity = next((e for e in entities if e.id == op.element_id), None)
                        if entity is None:
                            raise CanvasError('part_not_found')
                        if op.op == 'translate_solid':
                            groups = scene.assemblies if entity is scene else [entity] if hasattr(entity, 'pieces') else []
                            affected = [p for g in groups for p in g.pieces] if groups else [entity]
                            for piece in affected:
                                piece.position = [a+b for a,b in zip(piece.position, op.vector)]
                        elif op.op == 'set_solid_direction':
                            if entity is scene or not hasattr(entity, 'explode'):
                                raise CanvasError('invalid_solid_direction_target')
                            entity.explode, entity.order = op.vector, op.order
                        elif op.op == 'set_animation':
                            if entity is not scene or op.animation is None:
                                raise CanvasError('invalid_solid_animation')
                            scene.animation = op.animation
                        elif op.op in {'set_label', 'set_description'}:
                            setattr(entity, 'title' if entity is scene and op.op == 'set_label' else ('label' if op.op == 'set_label' else 'description'), op.value)
                        else:
                            _attribute(op.name, op.value)
                            groups = scene.assemblies if entity is scene else [entity] if hasattr(entity, 'pieces') else []
                            affected = [p for g in groups for p in g.pieces] if groups else [entity]
                            for piece in affected: piece.color = op.value
                        scene = IsometricScene.model_validate(scene.model_dump())
                        source, normalized = render_scene(scene)
                        _, root = validate_svg(source)
                        indexed = {n.get('id'): n for n in root.iter() if n.get('id')}
                        parts = [p.model_dump(exclude_none=True) for p in normalized]
                        changed_ids.append(op.element_id)
                        continue
                    if op.op in {'translate_solid', 'set_solid_direction'}:
                        raise CanvasError('solid_scene_required')
                    if solid_part:
                        raise CanvasError('此操作无法同步三维模型，已保留原版本。请选择部件修改名称、说明、填充颜色或三维位置。')
                    if op.op in {'set_explode', 'set_animation'}:
                        part = next((p for p in parts if p['element_id'] == op.element_id), None)
                        if part is None:
                            raise CanvasError('part_not_found')
                        field = 'explode' if op.op == 'set_explode' else 'animation'
                        definition = getattr(op, field)
                        if definition is not None:
                            part[field] = definition.model_dump()
                        else:
                            part.pop(field, None)
                        changed_ids.append(op.element_id)
                    elif op.op in {"set_description", "set_label"}:
                        part = next((p for p in parts if p["element_id"] == op.element_id), None)
                        if part is None:
                            raise CanvasError("part_not_found")
                        field = "description" if op.op == "set_description" else "label"
                        if field == "label" and (not op.value.strip() or len(op.value) > 256):
                            raise CanvasError("invalid_part_label")
                        updated = Part(**(part | {field: op.value})).model_dump(exclude_none=True)
                        part.update(updated)
                        changed_ids.append(op.element_id)
                    elif op.op == "set_text":
                        if _tag(target) not in {"text", "tspan"} or len(target):
                            raise CanvasError("text_edit_requires_leaf")
                        target.text = op.value
                    else:
                        if op.name not in {"fill", "stroke", "stroke-width", "opacity", "font-size", "transform"}:
                            raise CanvasError("unsupported_edit_attribute")
                        _attribute(op.name, op.value)
                        # A group color applies to its drawable descendants too,
                        # avoiding misleading success when children override fill.
                        targets = [n for n in target.iter() if _tag(n) not in {"g", "text", "tspan", "title", "desc"}] \
                            if op.name in {"fill", "stroke"} and _tag(target) == "g" else [target]
                        for item in targets:
                            if _tag(item) not in {"title", "desc"}:
                                item.set(op.name, op.value)
                                changed_ids.append(item.get("id"))
                    if op.op == "set_text":
                        changed_ids.append(op.element_id)
                validate_explosions(root, [Part(**p) for p in parts])
                source, _ = validate_svg(ET.tostring(root, encoding="unicode"))
            serialized_parts = json.dumps(parts, ensure_ascii=False)
            changed = source != old["source"] or parts != json.loads(old["parts"])
            revision, digest = (self._insert_revision(db, canvas_id, canvas["current"], source, actor, serialized_parts)
                                if changed else (old["id"], old["digest"]))
            db.execute("UPDATE canvases SET current=? WHERE id=?", (revision, canvas_id))
            result = {"schema": "tcode.canvas-edit-receipt.v1", "canvas_id": canvas_id,
                      "request_id": edit.request_id, "previous_revision_id": old["id"],
                      "revision_id": revision, "source_digest": digest, "changed": changed,
                      "changed_element_ids": sorted(set(changed_ids)), "status": "committed"}
            if actor == 'agent' and isinstance(edit, Edit):
                # Continue only this validated selection, at this committed
                # revision. Never renew its expiry or broaden its scope.
                next_id = 'selection_' + uuid.uuid4().hex if changed else edit.selection_id
                if changed:
                    db.execute('INSERT INTO canvas_selections VALUES(?,?,?,?,?,?)', (
                        next_id, canvas_id, session_id, revision,
                        json.dumps(edit.element_ids), selection['expires']))
                result['next_selection'] = {'selection_id': next_id, 'canvas_id': canvas_id,
                    'base_revision_id': revision, 'element_ids': edit.element_ids}
            self._record(db, canvas_id, edit.request_id, payload, result)
            return result
