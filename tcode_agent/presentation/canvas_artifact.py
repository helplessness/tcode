"""Bounded workspace draft import. Imported SVG remains data, never executable."""
import hashlib
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .canvas import CanvasError, Part
from .isometric import IsometricScene, render_scene


class CanvasDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=1_048_576)
    parts: list[Part] = Field(max_length=100)
    style: Literal['clay', 'manual'] | None = None
    scene: IsometricScene | None = None


def read_canvas_draft(workdir: str, path: Path, expected_digest: str = ''):
    root = Path(workdir).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise CanvasError('canvas_artifact_outside_workspace', 403)
    if not relative.parts or '..' in relative.parts:
        raise CanvasError('invalid_canvas_artifact_path', 403)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CanvasError('invalid_canvas_artifact_file')
            raw = stream.read(1_500_001)
        if len(raw) > 1_500_000:
            raise CanvasError('canvas_artifact_too_large', 413)
        digest = hashlib.sha256(raw).hexdigest()
        if expected_digest and digest != expected_digest:
            raise CanvasError('canvas_artifact_changed', 409)
        draft = CanvasDraft.model_validate_json(raw)
        if draft.scene is not None:
            # Geometry is authoritative in this explicit mode. Never attach a
            # spatial model to unrelated, model-supplied SVG.
            if draft.title != draft.scene.title:
                raise CanvasError('canvas_artifact_scene_title_mismatch')
            draft.source, draft.parts = render_scene(draft.scene, draft.style or 'clay')
        return draft, digest
    except OSError as exc:
        raise CanvasError('canvas_artifact_unreadable') from exc
    finally:
        os.close(descriptor)
