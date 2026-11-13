"""Owner-scoped evidence retrieval and image endpoints.

Routes retain their original paths and are registered by app.py.
"""

import mimetypes
import json
import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..session.manager import SessionManager
from .workspace_scope import (
    _resolve_workdir_from_name_or_path,
    _runtime_session_matches_context,
    _session_owner_kwargs,
    _workspace_context_from_request,
)
from .state import ServerState

router = APIRouter()


def _evidence_details(record, workdir: str) -> dict:
    from ..evidence import EvidenceStore
    from ..evidence.presentation import present_evidence_graph, present_evidence_record
    from ..evidence.maintenance import public_status

    return {
        "evidence": present_evidence_record(record),
        "graph": present_evidence_graph(EvidenceStore.graph_neighborhood(record.evidence_id, workdir=workdir, depth=2, limit=48)),
        "doctor": EvidenceStore.doctor(workdir=workdir, evidence_id=record.evidence_id, persist=False),
        "maintenance": public_status(workdir),
    }


class CitationResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["evidence", "ontology_assertion", "ontology_evidence", "knowledge"]
    id: str = Field(min_length=1, max_length=2400)
    workspace_id: str = Field(min_length=1, max_length=300)
    session_id: str = Field(default="", max_length=300)

    @field_validator('id')
    @classmethod
    def bounded_reference(cls, value, info):
        if info.data.get('kind') != 'knowledge' and len(value) > 300:
            raise ValueError('引用编号过长')
        return value


class EvidenceMaintenanceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=300)
    rebuild: bool = True
    verify_assets: bool = False


async def _resolve_evidence_workdir(
    request: Request,
    *,
    session_id: str = "",
    workdir_name: str = "",
    workdir_path: str = "",
) -> str:
    workspace_context = _workspace_context_from_request(
        request,
        workdir_name=str(workdir_name or "").strip(),
    )
    requested_name = str(workdir_name or "").strip()
    requested_path = str(workdir_path or "").strip()
    if requested_name or requested_path:
        return _resolve_workdir_from_name_or_path(
            workdir_name=requested_name,
            workdir_path=requested_path,
            context=workspace_context,
        )

    session_text = str(session_id or "").strip()
    if session_text:
        runtime = ServerState.sessions.get(session_text)
        if isinstance(runtime, dict) and _runtime_session_matches_context(
            runtime,
            workspace_context,
        ):
            runtime_workdir = str(runtime.get("workdir") or "").strip()
            if runtime_workdir:
                return runtime_workdir

        persisted = await SessionManager.get(
            session_text,
            **_session_owner_kwargs(workspace_context),
        )
        persisted_workdir = str(getattr(persisted, "workdir", "") or "").strip()
        if persisted_workdir:
            return persisted_workdir

    return ServerState.get_workdir()


@router.get('/api/evidence/maintenance/status')
async def evidence_maintenance_status(request: Request, workdir_name: str = Query(min_length=1, max_length=300)):
    from ..evidence.maintenance import public_status
    workdir = await _resolve_evidence_workdir(request, workdir_name=workdir_name)
    return await asyncio.to_thread(public_status, workdir)


@router.post('/api/evidence/maintenance')
async def start_evidence_maintenance(body: EvidenceMaintenanceBody, request: Request):
    from ..evidence.maintenance import request as request_maintenance
    workdir = await _resolve_evidence_workdir(request, workdir_name=body.workspace_id)
    try:
        return await asyncio.to_thread(request_maintenance, workdir=workdir, rebuild=body.rebuild, verify_assets=body.verify_assets)
    except ValueError:
        raise HTTPException(409, '当前维护尚未完成，请稍后重试。')


@router.get("/api/evidence/{evidence_id}")
async def get_evidence(
    evidence_id: str,
    request: Request,
    session_id: str = Query(default=""),
    workdir_name: str = Query(default=""),
    workdir_path: str = Query(default=""),
):
    from ..evidence import EvidenceStore

    normalized_id = str(evidence_id or "").strip()
    if not normalized_id:
        raise HTTPException(400, "evidence_id is required")
    workdir = await _resolve_evidence_workdir(
        request,
        session_id=session_id,
        workdir_name=workdir_name,
        workdir_path=workdir_path,
    )
    record = await asyncio.to_thread(EvidenceStore.get, normalized_id, workdir=workdir)
    if record is None:
        raise HTTPException(404, "Evidence not found")
    return await asyncio.to_thread(_evidence_details, record, workdir)


@router.post("/api/evidence/resolve")
async def resolve_evidence(body: CitationResolveBody, request: Request):
    """Resolve a typed citation without treating ontology IDs as EvidenceStore IDs."""

    normalized_id = body.id.strip()
    if body.kind == 'knowledge':
        from ..research.knowledge_search import KnowledgeSearch
        context = _workspace_context_from_request(request, workspace_id=body.workspace_id)
        workdir = await _resolve_evidence_workdir(request, workdir_name=body.workspace_id)
        try:
            result = await asyncio.to_thread(KnowledgeSearch(
                tenant_id=context.tenant_id, user_id=context.user_id, workspace_id=context.workspace_id,
                workdir=workdir).read, normalized_id)
        except ValueError:
            return {'state': 'forbidden'}
        except LookupError:
            return {'state': 'missing'}
        return {'state': result['state'],
                'title': result['title'], 'content': result['content'] + '\n\n' + result['notice'],
                'kind': result['kind'], 'content_hash': result['content_hash'], 'locator': result.get('locator') or '',
                'citations': [s.get('title', '关联依据') for s in result['supporting_sources']],
                 'technical': {'source_checked': True, 'changed': result['changed'], 'usable': result['usable'],
                               'supporting_sources_truncated': result['supporting_sources_truncated']}}
    if body.kind == "evidence":
        from ..evidence import EvidenceStore

        workdir = await _resolve_evidence_workdir(
            request, workdir_name=body.workspace_id
        )
        record = await asyncio.to_thread(EvidenceStore.get, normalized_id, workdir=workdir)
        if record is None:
            return {"state": "missing"}
        details = await asyncio.to_thread(_evidence_details, record, workdir)
        payload = {**details["evidence"], "graph": details["graph"], "doctor": details["doctor"], "maintenance": details['maintenance']}
        from ..research.hypothesis_map import HypothesisMap
        context = _workspace_context_from_request(request, workspace_id=body.workspace_id)
        source = await asyncio.to_thread(HypothesisMap(
            tenant_id=context.tenant_id, user_id=context.user_id, workspace_id=context.workspace_id,
            workdir=workdir).source, 'evidence', normalized_id)
        status = source['status']
        payload["state"] = ('withdrawn' if status == 'retracted' else 'missing' if status == 'missing'
                            else 'historical' if status in {'stale', 'superseded'} else 'available')
        payload.update(title=source['title'], content=source['content'], status=status,
                       content_hash=source['content_hash'], content_truncated=source.get('content_truncated', False))
        if source.get('source_notice'):
            payload['content'] = str(payload.get('content') or '') + '\n\n' + source['source_notice']
        payload["technical"] = {"evidence_id": record.evidence_id, 'original_available': source.get('original_available')}
        return payload

    from ..ontology_host import open_store, ontology_source_policy

    context = _workspace_context_from_request(request, workspace_id=body.workspace_id)
    owner = (context.tenant_id, context.user_id, context.workspace_id)
    store = open_store(ServerState.get_workdir(), owner)
    try:
        policy = ontology_source_policy(ServerState.get_workdir(), owner)
        if normalized_id.startswith("private-"):
            assertion_id = normalized_id.replace("private-evidence:", "private-assertion:", 1)
            row = store.db.execute(
                "SELECT payload,active,revision FROM assertion WHERE id=?", (assertion_id,)
            ).fetchone()
            if not row:
                return {"state": "missing"}
            fact = json.loads(row["payload"])
            state = "withdrawn" if not row["active"] else ("disabled" if not policy["private"] else "available")
            return {
                "state": state,
                "title": "Workspace 私有本体证据",
                "quote": fact.get("quote", ""),
                "locator": fact.get("locator") or {},
                "source_id": fact.get("source_id", ""),
                "source_revision": fact.get("source_revision"),
                "content_hash": fact.get("object_sha256", ""),
                "technical": {"assertion_id": assertion_id, "scope": "private", "revision": row["revision"]},
            }

        evidence_id = normalized_id
        if body.kind == "ontology_assertion":
            assertion = store.public.db.execute(
                "SELECT payload,state FROM assertion WHERE id=?", (normalized_id,)
            ).fetchone()
            if not assertion:
                return {"state": "missing"}
            assertion_payload = json.loads(assertion["payload"])
            evidence_ids = list(assertion_payload.get("evidence_ids") or [])
            evidence_id = str(evidence_ids[0] if evidence_ids else "")
            if not evidence_id:
                return {"state": "missing"}
        row = store.public.db.execute(
            "SELECT payload FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone()
        if not row:
            from ..ontology_core.extraction import LocalKnowledge
            from ..ontology_registry import registered_ontology_packages
            matches = []
            for package_id, root in registered_ontology_packages().items():
                package = LocalKnowledge(root)
                try:
                    legacy = package.db.execute("SELECT payload FROM evidence WHERE id=?", (evidence_id,)).fetchone()
                    if legacy:
                        matches.append((package_id, json.loads(legacy["payload"])))
                finally:
                    package.close()
            if len(matches) != 1:
                return {"state": "missing"}
            package_id, evidence = matches[0]
            return {
                "state": "historical",
                "title": evidence.get("title") or "历史领域本体证据",
                "quote": evidence.get("quote", ""),
                "content": evidence.get("text", ""),
                "locator": evidence.get("locator") or {
                    "line_start": evidence.get("line_start"), "line_end": evidence.get("line_end")
                },
                "source_id": evidence.get("card_id", ""),
                "content_hash": evidence.get("quote_sha256", ""),
                "technical": {"evidence_id": evidence_id, "package_id": package_id, "scope": "historical_package"},
            }
        evidence = json.loads(row["payload"])
        return {
            "state": "disabled" if not policy["public"] else "available",
            "title": evidence.get("title") or "公共本体证据",
            "quote": evidence.get("quote", ""),
            "content": evidence.get("text", ""),
            "locator": evidence.get("locator") or {},
            "source_id": (evidence.get("source_citations") or [{}])[0].get("source_id", ""),
            "content_hash": evidence.get("quote_sha256", ""),
            "technical": {"evidence_id": evidence_id, "scope": "public"},
        }
    finally:
        store.close()


@router.get("/api/evidence/{evidence_id}/images/{image_index}")
async def get_evidence_image(
    evidence_id: str,
    image_index: int,
    request: Request,
    session_id: str = Query(default=""),
    workdir_name: str = Query(default=""),
    workdir_path: str = Query(default=""),
):
    from ..evidence import EvidenceStore
    from ..evidence.presentation import resolve_evidence_image

    normalized_id = str(evidence_id or "").strip()
    if not normalized_id:
        raise HTTPException(400, "evidence_id is required")
    workdir = await _resolve_evidence_workdir(
        request,
        session_id=session_id,
        workdir_name=workdir_name,
        workdir_path=workdir_path,
    )
    record = await asyncio.to_thread(EvidenceStore.get, normalized_id, workdir=workdir)
    if record is None:
        raise HTTPException(404, "Evidence not found")
    try:
        target = resolve_evidence_image(record, image_index, workdir=workdir)
    except (IndexError, FileNotFoundError):
        raise HTTPException(404, "Evidence image not found")
    except PermissionError:
        raise HTTPException(403, "Evidence image is outside the workspace")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    response = FileResponse(
        target,
        media_type=media_type,
        filename=target.name,
        content_disposition_type="inline",
    )
    response.headers["Cache-Control"] = "private, max-age=300"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
