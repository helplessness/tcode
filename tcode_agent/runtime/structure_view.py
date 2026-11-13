"""Ephemeral, owner-scoped command/receipt bridge to the current browser viewer."""
import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Selector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["all", "selection", "residues", "chain", "ligand"]
    chain: str | None = Field(default=None, max_length=32, description="Author chain ID, case sensitive")
    residues: list[int] = Field(default_factory=list, max_length=500)
    insertion_code: str | None = Field(default=None, max_length=8)
    model_index: int | None = Field(default=None, ge=0)
    ligand_name: str | None = Field(default=None, max_length=16)
    radius: float | None = Field(default=None, ge=1, le=20, description="Expand to whole residues within this many Angstroms")

    @model_validator(mode="after")
    def required_fields(self):
        if self.target in {"chain", "residues"} and self.chain is None:
            raise ValueError("chain_required")
        if self.target == "residues" and not self.residues:
            raise ValueError("residues_required")
        return self


class MeasurementAtom(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chain: str = Field(max_length=32, description="Author chain ID")
    residue_number: int = Field(description="Author residue number; one residue per endpoint")
    atom_name: str = Field(default="CA", min_length=1, max_length=16, description="Atom name, CA for protein C-alpha")
    model_index: int = Field(default=0, ge=0)
    insertion_code: str | None = Field(default=None, max_length=8)
    alt_id: str | None = Field(default=None, max_length=8, description="Alternate conformation ID if ambiguous")
    operator: str | None = Field(default=None, max_length=80, description="Assembly operator if ambiguous")


class ViewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["inspect", "open", "select", "focus", "representation", "color", "rotate", "zoom", "reset", "clear", "undo", "measure", "clear_measurements"]
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$", description="Use SHA from inspect for every operation on an existing structure")
    path: str | None = Field(default=None, max_length=1024, description="Workspace relative structure path; only for open")
    selector: Selector | None = None
    representation: Literal["cartoon", "ball-and-stick", "molecular-surface", "spacefill", "line"] | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    axis: Literal["x", "y", "z"] | None = None
    degrees: float | None = Field(default=None, ge=-360, le=360)
    factor: float | None = Field(default=None, ge=0.2, le=5, description="Zoom >1 moves closer, <1 moves away")
    atoms: list[MeasurementAtom] | None = Field(default=None, min_length=2, max_length=2, description="measure only: exactly TWO atom endpoints; creates a native distance line and returns Angstrom distance in state.measurements")

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "open":
            from pathlib import PurePosixPath
            path = PurePosixPath(self.path or "")
            if not self.path or path.is_absolute() or ".." in path.parts or "\\" in self.path or ":" in self.path or path.suffix.lower() not in {".pdb", ".cif", ".mmcif", ".sdf", ".mol2"}:
                raise ValueError("workspace_structure_path_required")
        elif self.action != "inspect" and self.expected_sha256 is None:
            raise ValueError("inspect_first_and_provide_expected_sha256")
        if self.action in {"focus", "select", "color", "representation"} and self.selector is None:
            raise ValueError("selector_required")
        if self.action == "representation" and self.representation is None:
            raise ValueError("representation_required")
        if self.action == "color" and self.color is None:
            raise ValueError("color_required")
        if self.action == "rotate" and (self.axis is None or self.degrees is None):
            raise ValueError("axis_and_degrees_required")
        if self.action == "zoom" and self.factor is None:
            raise ValueError("factor_required")
        if self.action == "measure" and self.atoms is None:
            raise ValueError("two_atom_endpoints_required")
        if self.action != "measure" and self.atoms is not None:
            raise ValueError("atoms_only_for_measure")
        return self


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(max_length=64)
    ok: bool
    error: str = Field(default="", max_length=500)


class Exchange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    viewer_id: str = Field(min_length=1, max_length=64)
    state: dict[str, Any] = Field(default_factory=dict)
    receipt: Receipt | None = None
    takeover: bool = False
    enabled: bool = True
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def bounded_state(self):
        if len(json.dumps(self.state, allow_nan=False)) > 64000:
            raise ValueError("viewer_state_too_large")
        return self


@dataclass
class Connection:
    viewer_id: str
    seen: float
    state: dict
    pending: dict | None = None
    future: asyncio.Future | None = None
    enabled: bool = True
    revision: int = 0


class StructureBridge:
    # ponytail: single-process ephemeral UI bridge, not a durable job queue.
    # Multi-worker deployments need a shared broker before enabling this route.
    lease_seconds = 10

    def __init__(self):
        self.connections: dict[tuple[str, ...], Connection] = {}

    def prune(self):
        for owner, connection in list(self.connections.items()):
            if time.monotonic() - connection.seen > self.lease_seconds and not (connection.future and not connection.future.done()):
                self.disconnect(owner, connection.viewer_id)

    def disconnect(self, owner, viewer_id):
        connection = self.connections.get(owner)
        if connection and connection.viewer_id == viewer_id:
            if connection.future and not connection.future.done():
                connection.future.set_result({"ok": False, "error": "viewer_disconnected"})
            del self.connections[owner]

    def current_context(self, owner) -> dict[str, Any] | None:
        """Return the live viewer's file identity and bounded selection for this session."""
        self.prune()
        connection = self.connections.get(owner)
        state = connection.state if connection and connection.enabled else {}
        path, digest = state.get("path"), state.get("sha256")
        if not state.get("ready") or not isinstance(path, str) or not path or not isinstance(digest, str) or len(digest) != 64:
            return None
        return {
            "path": path[:1024],
            "sha256": digest,
            "chains": state.get("chains", [])[:200] if isinstance(state.get("chains"), list) else [],
            "selection": state.get("selection", [])[:20] if isinstance(state.get("selection"), list) else [],
            "selected_atoms": state.get("selected_atoms", 0),
        }

    def exchange(self, owner, payload: Exchange):
        self.prune()
        connection = self.connections.get(owner)
        if connection and connection.viewer_id != payload.viewer_id:
            if not payload.takeover:
                raise ValueError("viewer_active_in_another_tab")
            self.disconnect(owner, connection.viewer_id)
            connection = None
        if connection is None:
            connection = Connection(payload.viewer_id, time.monotonic(), {})
            self.connections[owner] = connection
        if payload.revision < connection.revision:
            return {"command": None}
        connection.revision = payload.revision
        if not payload.enabled:
            connection.seen, connection.enabled, connection.state = time.monotonic(), False, {}
            if connection.future and not connection.future.done():
                connection.future.set_result({"ok": False, "error": "viewer_disabled"})
            connection.pending = None
            return {"command": None}
        connection.enabled = True
        connection.seen, connection.state = time.monotonic(), payload.state
        receipt = payload.receipt
        if receipt and connection.pending and receipt.command_id == connection.pending["id"]:
            if connection.future and not connection.future.done():
                connection.future.set_result({**receipt.model_dump(), "state": payload.state})
            connection.pending = None
        return {"command": connection.pending}

    async def execute(self, owner, command: ViewCommand, abort=None, timeout=30):
        if abort is not None and abort.is_set():
            return {"ok": False, "error": "view_command_cancelled"}
        self.prune()
        connection = self.connections.get(owner)
        if connection is None or not connection.enabled:
            return {"ok": False, "error": "viewer_not_connected", "hint": "Open this session in the web app, and enable Agent structure control."}
        if connection.future and not connection.future.done():
            return {"ok": False, "error": "viewer_busy_retry_after_previous_tool"}
        if command.action not in {"open", "inspect"} and connection.state.get("sha256") != command.expected_sha256:
            return {"ok": False, "error": "structure_changed_inspect_again"}
        future = asyncio.get_running_loop().create_future()
        connection.future = future
        connection.pending = {"id": uuid.uuid4().hex, "expires_at": int((time.time() + timeout) * 1000), **command.model_dump(exclude_none=True)}
        cancel_task = asyncio.create_task(abort.wait()) if abort is not None else None
        try:
            done, _ = await asyncio.wait([future] + ([cancel_task] if cancel_task else []), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if future in done:
                return future.result()
            return {"ok": False, "error": "view_command_cancelled" if cancel_task in done else "view_receipt_timeout", "hint": "Display may have changed; inspect before retrying. Never claim execution succeeded without a receipt."}
        finally:
            connection.pending, connection.future = None, None
            if not future.done():
                future.cancel()
            if cancel_task:
                cancel_task.cancel()


bridge = StructureBridge()
