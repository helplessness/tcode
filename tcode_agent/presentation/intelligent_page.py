"""Versioned pages: structured sections and isolated HTML fragments."""
from __future__ import annotations

import json
import math
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

from .canvas import CanvasError, CanvasStore, ID, canvas_style
from .html_page import validate_html, validate_css
from .vector_graphic import vector_source, vector_tree, path_tokens


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Item(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class Point(Item):
    value: float = Field(allow_inf_nan=False, ge=-1e12, le=1e12)


class Section(StrictModel):
    id: str = Field(pattern=ID.pattern)
    title: str = Field(min_length=1, max_length=200)
    hidden: bool = False


class Card(Section):
    type: Literal["card"]
    text: str = Field(default="", max_length=6000)
    items: list[Item] = Field(default_factory=list, max_length=20)


class Chart(Section):
    type: Literal["chart"]
    chart_type: Literal["bar", "line"] = "bar"
    unit: str = Field(default="", max_length=40)
    description: str = Field(default="", max_length=2000)
    points: list[Point] = Field(min_length=1, max_length=50)


class Steps(Section):
    type: Literal["steps"]
    items: list[Item] = Field(min_length=1, max_length=20)


class Parameter(StrictModel):
    id: str = Field(pattern=ID.pattern)
    label: str = Field(min_length=1, max_length=100)
    unit: str = Field(default="", max_length=40)
    min: float = Field(allow_inf_nan=False, ge=-1e6, le=1e6)
    max: float = Field(allow_inf_nan=False, ge=-1e6, le=1e6)
    step: float = Field(allow_inf_nan=False, gt=0, le=2e6)
    value: float = Field(allow_inf_nan=False, ge=-1e6, le=1e6)

    @model_validator(mode="after")
    def bounds(self):
        if not self.min < self.max or not self.min <= self.value <= self.max:
            raise ValueError("invalid_parameter_range")
        if not self.step <= self.max - self.min <= self.step * 1e6:
            raise ValueError("invalid_parameter_step")
        return self


class Threshold(StrictModel):
    value: float = Field(allow_inf_nan=False, ge=-1e12, le=1e12)
    below: Item
    at_or_above: Item


class Formula(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    unit: str = Field(default="", max_length=40)
    operation: Literal["sum", "difference", "product", "ratio", "linear"]
    args: list[str | float] = Field(min_length=2, max_length=3)
    min: float = Field(allow_inf_nan=False, ge=-1e12, le=1e12)
    max: float = Field(allow_inf_nan=False, ge=-1e12, le=1e12)
    description: str = Field(default="", max_length=2000)
    threshold: Threshold | None = None

    @model_validator(mode="after")
    def bounds(self):
        if self.min >= self.max or len(self.args) != (3 if self.operation == "linear" else 2):
            raise ValueError("invalid_formula_shape")
        if any(isinstance(a, float) and (not math.isfinite(a) or abs(a) > 1e6) for a in self.args):
            raise ValueError("invalid_formula_constant")
        return self


class GraphicBinding(StrictModel):
    output_index: int = Field(ge=0, le=3)
    min: float = Field(allow_inf_nan=False, ge=0, le=400)
    max: float = Field(allow_inf_nan=False, ge=0, le=400)


GraphicColor = Literal["accent", "positive", "warning", "danger", "muted"]


class GraphicStateColor(StrictModel):
    output_index: int = Field(ge=0, le=3)
    below: GraphicColor = "accent"
    at_or_above: GraphicColor = "warning"


class VectorBinding(StrictModel):
    element_id: str = Field(pattern=ID.pattern)
    output_index: int = Field(ge=0, le=3)
    attribute: Literal['x', 'y', 'cx', 'cy', 'r', 'rx', 'ry', 'width', 'height', 'opacity', 'stroke-width',
                       'translate_x', 'translate_y', 'rotate', 'scale', 'fill', 'stroke', 'd']
    min: float = Field(default=0, allow_inf_nan=False, ge=-100000, le=100000)
    max: float = Field(default=1, allow_inf_nan=False, ge=-100000, le=100000)
    origin_x: float = Field(default=0, allow_inf_nan=False, ge=-100000, le=100000)
    origin_y: float = Field(default=0, allow_inf_nan=False, ge=-100000, le=100000)
    below: GraphicColor = 'accent'
    at_or_above: GraphicColor = 'warning'
    start_path: str = Field(default='', max_length=16000)
    end_path: str = Field(default='', max_length=16000)

    @model_validator(mode='after')
    def safe_values(self):
        if self.attribute == 'd':
            start, end = path_tokens(self.start_path, morph=True), path_tokens(self.end_path, morph=True)
            if len(start) != len(end) or any((a != b) if a.isalpha() or b.isalpha() else False for a, b in zip(start, end)):
                raise ValueError('incompatible_path_morph')
        elif self.start_path or self.end_path:
            raise ValueError('unexpected_path_morph')
        if self.attribute in ('r', 'rx', 'ry', 'width', 'height', 'stroke-width', 'scale') and min(self.min, self.max) < 0:
            raise ValueError('negative_vector_size')
        if self.attribute == 'opacity' and not 0 <= min(self.min, self.max) <= max(self.min, self.max) <= 1:
            raise ValueError('invalid_vector_opacity')
        if self.attribute == 'rotate' and max(abs(self.min), abs(self.max)) > 360:
            raise ValueError('invalid_vector_rotation')
        return self


class ModelGraphic(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    shape: Literal["rect", "ellipse", "svg"]
    source: str = Field(default='', max_length=40000)
    bindings: list[VectorBinding] = Field(default_factory=list, max_length=32)
    x: float | GraphicBinding = 20.0
    y: float | GraphicBinding = 20.0
    width: float | GraphicBinding = 80.0
    height: float | GraphicBinding = 80.0
    color: GraphicColor | GraphicStateColor = "accent"

    @model_validator(mode="after")
    def geometry(self):
        if self.shape == 'svg':
            self.source, root = vector_source(self.source)
            nodes = {node.get('id'): node for node in root.iter()}
            targets = set()
            compatible = {'x': {'rect', 'text', 'svg'}, 'y': {'rect', 'text', 'svg'},
                          'cx': {'circle', 'ellipse'}, 'cy': {'circle', 'ellipse'}, 'r': {'circle'},
                          'rx': {'rect', 'ellipse'}, 'ry': {'rect', 'ellipse'},
                          'width': {'rect', 'svg'}, 'height': {'rect', 'svg'}, 'd': {'path'}}
            for binding in self.bindings:
                node = nodes.get(binding.element_id)
                if node is None or (binding.element_id, binding.attribute) in targets:
                    raise ValueError('invalid_vector_target')
                if node is root and binding.attribute in ('x', 'y', 'width', 'height'):
                    raise ValueError('root_viewport_binding_not_supported')
                targets.add((binding.element_id, binding.attribute))
                if binding.attribute in compatible and node.tag.rsplit('}', 1)[-1] not in compatible[binding.attribute]:
                    raise ValueError('incompatible_vector_attribute')
        elif self.source or self.bindings:
            raise ValueError('vector_fields_require_svg')
        bounds = {}
        for key in ("x", "y", "width", "height"):
            value = getattr(self, key)
            lo, hi = (min(value.min, value.max), max(value.min, value.max)) if isinstance(value, GraphicBinding) else (value, value)
            limit = 240 if key in ("y", "height") else 400
            if not math.isfinite(lo) or not math.isfinite(hi) or lo < (1 if key in ("width", "height") else 0) or hi > limit:
                raise ValueError("invalid_graphic_bounds")
            bounds[key] = hi
        if bounds["x"] + bounds["width"] > 400 or bounds["y"] + bounds["height"] > 240:
            raise ValueError("graphic_outside_viewbox")
        return self


class AnimationTrack(StrictModel):
    parameter_id: str = Field(pattern=ID.pattern)
    start: float = Field(allow_inf_nan=False, ge=-1e6, le=1e6)
    end: float = Field(allow_inf_nan=False, ge=-1e6, le=1e6)


class ModelAnimation(StrictModel):
    duration_seconds: float = Field(allow_inf_nan=False, ge=1, le=60)
    tracks: list[AnimationTrack] = Field(min_length=1, max_length=4)


class Model(Section):
    type: Literal["model"]
    description: str = Field(default="", max_length=2000)
    parameters: list[Parameter] = Field(min_length=1, max_length=4)
    outputs: list[Formula] = Field(min_length=1, max_length=4)
    graphics: list[ModelGraphic] = Field(default_factory=list, max_length=8)
    animation: ModelAnimation | None = None

    @model_validator(mode="after")
    def references(self):
        ids = {p.id for p in self.parameters}
        if len(ids) != len(self.parameters):
            raise ValueError("duplicate_parameter_id")
        if any(isinstance(a, str) and a not in ids for f in self.outputs for a in f.args):
            raise ValueError("unknown_formula_parameter")
        for graphic in self.graphics:
            for binding in graphic.bindings:
                if binding.output_index >= len(self.outputs):
                    raise ValueError('unknown_vector_output')
                if binding.attribute in ('fill', 'stroke') and self.outputs[binding.output_index].threshold is None:
                    raise ValueError('vector_color_requires_threshold')
            for key in ("x", "y", "width", "height", "color"):
                binding = getattr(graphic, key)
                if isinstance(binding, (GraphicBinding, GraphicStateColor)):
                    if binding.output_index >= len(self.outputs):
                        raise ValueError("unknown_graphic_output")
                    if isinstance(binding, GraphicStateColor) and self.outputs[binding.output_index].threshold is None:
                        raise ValueError("graphic_color_requires_threshold")
        if self.animation:
            parameters = {p.id: p for p in self.parameters}
            tracks = self.animation.tracks
            if len({t.parameter_id for t in tracks}) != len(tracks):
                raise ValueError("duplicate_animation_track")
            for track in tracks:
                parameter = parameters.get(track.parameter_id)
                if parameter is None or not parameter.min <= track.start <= parameter.max or not parameter.min <= track.end <= parameter.max:
                    raise ValueError("invalid_animation_parameter")
                if track.start != parameter.value or track.start == track.end:
                    raise ValueError("animation_start_must_match_default_and_differ_from_end")
        return self


class Button(Section):
    type: Literal["button"]
    action: Literal["show", "ask"]
    target_id: str = Field(default="", max_length=96)
    prompt: str = Field(default="", max_length=2000)


class Html(Section):
    type: Literal["html"]
    html: str = Field(min_length=1, max_length=60000)
    css: str = Field(default="", max_length=16000)

    @field_validator("html")
    @classmethod
    def fragment(cls, value):
        return validate_html(value)

    @field_validator("css")
    @classmethod
    def stylesheet(cls, value):
        return validate_css(value)


PageSection = Annotated[Card | Chart | Steps | Button | Html | Model, Field(discriminator="type")]


class PageDocument(StrictModel):
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=2000)
    sections: list[PageSection] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def bindings(self):
        indexed = {s.id: s for s in self.sections}
        if len(indexed) != len(self.sections):
            raise ValueError("duplicate_section_id")
        for section in self.sections:
            if isinstance(section, Button):
                if section.hidden:
                    raise ValueError("button_cannot_be_hidden")
                if section.action == "show":
                    target = indexed.get(section.target_id)
                    if target is None or isinstance(target, Button) or section.prompt:
                        raise ValueError("invalid_button_target")
                elif not section.prompt.strip() or section.target_id:
                    raise ValueError("invalid_button_prompt")
        if not any(not s.hidden for s in self.sections if not isinstance(s, Button)):
            raise ValueError("visible_content_required")
        return self


class PageContentBlock(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    type: Literal["intelligent_page"]
    schema_id: Literal["tcode.intelligent-page.v1"] = Field(alias="schema")
    page_id: str = Field(pattern=r"^page_[0-9a-f]{32}$")
    revision_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")
    session_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=256)
    fallback: str = Field(max_length=512)


def page_block(value: dict) -> dict:
    return {"type": "intelligent_page", "schema": "tcode.intelligent-page.v1",
            **{k: value[k] for k in ("page_id", "revision_id", "session_id", "title")},
            "fallback": "交互页面：" + value["title"]}


def normalize_page_block(value: dict) -> dict | None:
    try:
        return page_block(PageContentBlock.model_validate(value).model_dump(by_alias=True))
    except ValueError:
        return None


def parse_document(source: str) -> PageDocument:
    if len(source.encode()) > 128_000:
        raise CanvasError("page_document_size_limit", 413)
    return PageDocument.model_validate_json(source)


class PageStore(CanvasStore):
    """Reuse canvas ownership, safe storage, SQLite and idempotency helpers."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS intelligent_pages (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, user TEXT NOT NULL,
                    workspace TEXT NOT NULL, session TEXT NOT NULL, current TEXT NOT NULL,
                    style TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intelligent_page_revisions (
                    id TEXT PRIMARY KEY, page TEXT NOT NULL, parent TEXT,
                    document TEXT NOT NULL, created TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS intelligent_page_revision_page ON intelligent_page_revisions(page);
            """)

    def _page(self, db, page_id):
        row = db.execute("SELECT * FROM intelligent_pages WHERE id=? AND tenant=? AND user=? AND workspace=?",
                         (page_id, *self.owner)).fetchone()
        if row is None:
            raise CanvasError("page_not_found", 404)
        return row

    def save(self, *, session_id: str, document: PageDocument, request_id: str,
             style: str, page_id: str = "", base_revision_id: str = "") -> dict:
        if style not in {"clay", "manual"} or not session_id or len(session_id) > 200:
            raise CanvasError("invalid_page_metadata")
        source = document.model_dump_json()
        parse_document(source)
        payload = json.dumps([session_id, source, style, page_id, base_revision_id])
        scope = "page:" + json.dumps([*self.owner, session_id, page_id])
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._replay(db, scope, request_id, payload)
            if previous:
                return previous
            parent = None
            if page_id:
                page = self._page(db, page_id)
                if page["session"] != session_id:
                    raise CanvasError("page_session_mismatch", 403)
                if page["current"] != base_revision_id:
                    raise CanvasError("revision_conflict", 409)
                parent = page["current"]
            else:
                if base_revision_id:
                    raise CanvasError("unexpected_base_revision")
                page_id = "page_" + uuid.uuid4().hex
            revision_id = "rev_" + uuid.uuid4().hex
            db.execute("INSERT INTO intelligent_page_revisions VALUES(?,?,?,?,?)", (
                revision_id, page_id, parent, source, datetime.now(timezone.utc).isoformat()))
            if parent:
                db.execute("UPDATE intelligent_pages SET current=? WHERE id=?", (revision_id, page_id))
            else:
                db.execute("INSERT INTO intelligent_pages VALUES(?,?,?,?,?,?,?)", (page_id, *self.owner, session_id, revision_id, style))
            result = {"page_id": page_id, "revision_id": revision_id, "title": document.title, "session_id": session_id}
            self._record(db, scope, request_id, payload, result)
            return result

    def list_pages(self, session_id: str, title: str = "") -> dict:
        """Return a bounded title directory, never other sessions or full definitions."""
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT p.id, p.current, r.document, r.created
                FROM intelligent_pages p JOIN intelligent_page_revisions r ON r.id=p.current
                WHERE p.tenant=? AND p.user=? AND p.workspace=? AND p.session=?
                AND instr(json_extract(r.document, '$.title'), ?) > 0
                ORDER BY r.created DESC, p.id DESC LIMIT 51""", (*self.owner, session_id, title)).fetchall()
            return {"items": [{"page_id": row["id"], "revision_id": row["current"],
                    "title": json.loads(row["document"])["title"], "created": row["created"]}
                    for row in rows[:50]], "has_more": len(rows) > 50}

    def read_page(self, page_id: str, revision_id: str = "") -> dict:
        with closing(self._connect()) as db:
            page = self._page(db, page_id)
            row = db.execute("SELECT * FROM intelligent_page_revisions WHERE page=? AND id=?",
                             (page_id, revision_id or page["current"])).fetchone()
            if row is None:
                raise CanvasError("revision_not_found", 404)
            document = parse_document(row["document"])
            return {"page_id": page_id, "session_id": page["session"], "revision_id": row["id"],
                    "current_revision_id": page["current"], "parent_revision_id": self._visible_parent(db, 'page', page_id, row['parent']),
                    "is_deleted": self._is_deleted(db, 'page', page_id, row['id']),
                    "title": document.title, "document": document.model_dump(), "style": page["style"],
                    "graphic_trees": {section.id: [vector_tree(g.source) if g.shape == 'svg' else None for g in section.graphics]
                                      for section in document.sections if isinstance(section, Model) and any(g.shape == 'svg' for g in section.graphics)},
                    "display_style": canvas_style()}

    def action(self, page_id: str, *, session_id: str, revision_id: str, button_id: str,
               selections: dict[str, int]) -> dict:
        value = self.read_page(page_id, revision_id)
        if value["session_id"] != session_id:
            raise CanvasError("page_session_mismatch", 403)
        if revision_id != value["current_revision_id"]:
            raise CanvasError("revision_conflict", 409)
        document = PageDocument.model_validate(value["document"])
        button = next((s for s in document.sections if s.id == button_id), None)
        if not isinstance(button, Button) or button.action != "ask":
            raise CanvasError("invalid_page_action")
        context = []
        for section_id, index in selections.items():
            section = next((s for s in document.sections if s.id == section_id), None)
            items = section.points if isinstance(section, Chart) else section.items if isinstance(section, Steps) else []
            if not 0 <= index < len(items):
                raise CanvasError("invalid_page_selection")
            context.append({"section_id": section_id, "title": section.title, "item": items[index].model_dump()})
        return {"page_id": page_id, "revision_id": revision_id, "button_id": button_id,
                "request": button.prompt, "selections": context}

    def revisions(self, page_id: str, before: str = "", deleted: bool = False) -> dict:
        with closing(self._connect()) as db:
            page = self._page(db, page_id)
            boundary = 9223372036854775807
            if before:
                row = db.execute("SELECT rowid FROM intelligent_page_revisions WHERE page=? AND id=?", (page_id, before)).fetchone()
                if row is None:
                    raise CanvasError("revision_not_found", 404)
                boundary = row[0]
            rows = db.execute("SELECT id AS revision_id,parent AS parent_revision_id,created FROM intelligent_page_revisions r WHERE page=? AND rowid<? AND EXISTS(SELECT 1 FROM intelligent_ui_revision_trash t WHERE t.kind='page' AND t.object_id=r.page AND t.revision=r.id)=? ORDER BY rowid DESC LIMIT 51", (page_id, boundary, int(deleted))).fetchall()
            return {"current_revision_id": page["current"], "items": [dict(row) | {'parent_revision_id': self._visible_parent(db, 'page', page_id, row['parent_revision_id'])} for row in rows[:50]],
                    "next_before": rows[49]["revision_id"] if len(rows) > 50 else ""}

    def restore(self, page_id: str, *, session_id: str, base_revision_id: str, revision_id: str, request_id: str) -> dict:
        # Saving the selected definition creates a new child; it never deletes later history.
        target = self.read_page(page_id, revision_id)
        return self.save(session_id=session_id, document=PageDocument.model_validate(target["document"]),
            style=target["style"], page_id=page_id, base_revision_id=base_revision_id, request_id=request_id)
