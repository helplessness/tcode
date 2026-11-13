"""Authenticated private ontology governance. No public mutations in this stage."""
from __future__ import annotations
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..ontology_host import (open_store, load_core, platform_knowledge_root, plugin_root, ontology_model_settings,
                             ontology_model_options, ontology_model_runtime, ontology_source_policy, update_ontology_source_policy)
from ..ontology_core.extraction import ModelRequestError
from .auth import require_current_user
from .workspace_scope import (
    _workspace_context_from_request,
)
from .files_api import _resolve_workspace_file_path
from .state import ServerState
from .workdirs import assert_workspace_active

router = APIRouter(prefix='/api/ontology', tags=['ontology'])
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ontology-intake')


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ImportBody(StrictBody):
    upload_path: str
    extractor: Literal['mentions', 'model'] = 'mentions'
    source_id: str | None = None
    base_revision: int | None = None
    idempotency_key: str | None = None
    model_ref: str | None = Field(default=None, max_length=300)


class DecisionBody(StrictBody):
    expected_revision: int = Field(ge=0)
    payload_hash: str
    selected_ids: list[str]
    identity_choices: dict[str, str] = Field(default_factory=dict)
    accept_conflicts: bool = False
    decision: Literal['accepted', 'rejected'] = 'accepted'


class RevisionBody(StrictBody):
    expected_revision: int = Field(ge=0)


class RebaseBody(StrictBody):
    apply: bool = False


class DraftEdit(StrictBody):
    operation_id: str
    changes: dict = Field(default_factory=dict)
    remove: bool = False


class DraftRevisionBody(StrictBody):
    expected_revision: int = Field(ge=0)
    payload_hash: str
    edits: list[DraftEdit] = Field(min_length=1, max_length=200)


class Statement(StrictBody):
    subject_id: str = Field(min_length=1, max_length=500)
    predicate: str = Field(min_length=1, max_length=500)
    object_id: str = Field(min_length=1, max_length=500)
    polarity: Literal['supports', 'opposes'] = 'supports'
    qualifiers: dict = Field(default_factory=dict)
    kind: Literal['reported', 'hypothesis'] = 'reported'


class Claim(StrictBody):
    text: str = Field(min_length=1, max_length=4000)
    assertion_ids: list[str] = Field(min_length=1, max_length=20)
    statement: Statement | None = None
    required_evidence_basis: Literal['any', 'experimental', 'computational', 'literature_report', 'hypothesis'] = 'any'


class ReportBody(StrictBody):
    claims: list[Claim] = Field(min_length=1, max_length=100)


class SchemaClass(StrictBody):
    id: str
    label: str = ''
    description: str = ''


class SchemaPredicate(StrictBody):
    id: str
    label: str = ''
    description: str = ''
    domains: list[str] = Field(min_length=1, max_length=50)
    ranges: list[str] = Field(min_length=1, max_length=50)


class SchemaRevisionBody(StrictBody):
    expected_version: int = Field(ge=0)
    classes: list[SchemaClass] = Field(max_length=200)
    predicates: list[SchemaPredicate] = Field(max_length=500)


class SourcePolicyBody(StrictBody):
    public: bool
    private: bool


class ManualEntity(StrictBody):
    key: str
    type: str
    label: str


class ManualAssertion(StrictBody):
    subject_key: str
    predicate: str
    object_key: str
    polarity: Literal['supports', 'opposes'] = 'supports'
    kind: Literal['reported', 'annotation', 'analogy', 'hypothesis'] = 'annotation'
    qualifiers: dict = Field(default_factory=dict)
    evidence: str = Field(min_length=1, max_length=20000)


class ManualFactsBody(StrictBody):
    expected_schema_version: int = Field(ge=0)
    source_id: str = Field(default='workspace-manual', min_length=1, max_length=200)
    entities: list[ManualEntity] = Field(min_length=1, max_length=200)
    assertions: list[ManualAssertion] = Field(max_length=500)


class ConflictDecisionBody(StrictBody):
    left_id: str = Field(min_length=1, max_length=500)
    right_id: str = Field(min_length=1, max_length=500)
    action: Literal['different_conditions', 'insufficient_information', 'retain_dispute', 'withdraw_incorrect', 'reopen']
    reason: str = Field(min_length=1, max_length=4000)
    expected_snapshot: str = Field(min_length=64, max_length=64)
    expected_version: int = Field(ge=0)
    withdraw_id: str = Field(default='', max_length=500)


def authorize(request):
    user = require_current_user(request)
    if request.method != 'GET':
        origin = request.headers.get('origin')
        if origin and urlparse(origin).netloc != request.url.netloc:
            raise HTTPException(403, 'Cross-origin governance request refused')
    context = _workspace_context_from_request(request)
    assert_workspace_active(ServerState.get_workdir(), context)
    return context, user


@contextmanager
def store_for(request):
    context, user = authorize(request)
    store = None
    try:
        store = open_store(ServerState.get_workdir(), (context.tenant_id, context.user_id, context.workspace_id))
        yield store, context, user
    except ValueError as exc:
        _, conflict = load_core()
        raise HTTPException(409 if isinstance(exc, conflict) else 400, str(exc)) from exc
    finally:
        if store:
            store.close()


def run_job(server_root, owner, job_id, retry=False, glean_only=False):
    store = open_store(server_root, owner)
    try:
        row = store.intake.db.execute('SELECT config FROM job WHERE id=?', (job_id,)).fetchone()
        config = __import__('json').loads(row['config']) if row else {}
        def execute(runtime=None):
            if glean_only:
                store.intake.retry_failed_gleaning(job_id, runtime_config=runtime)
            else:
                store.intake.run(job_id, retry=retry, runtime_config=runtime)
        if config.get('mode') == 'model':
            current = ontology_model_settings(server_root, config.get('model_ref'))
            if current['base_url'] != config.get('base_url') or current['model'] != config.get('model'):
                raise ValueError('模型配置已变更，请重新提交导入任务。')
            with ontology_model_runtime(server_root, current) as runtime:
                execute(runtime)
        else:
            execute()
    except Exception as exc:
        error = str(exc)[:200] if isinstance(exc, (ValueError, ModelRequestError)) else '模型请求失败，请检查模型设置后重试。'
        if glean_only:
            with store.intake.db:
                store.intake.db.execute("UPDATE item SET error=? WHERE job_id=? AND state='succeeded'", ('补抽失败：' + error, job_id))
            return
        with store.intake.db:
            store.intake.db.execute("UPDATE job SET state='failed' WHERE id=?", (job_id,))
            store.intake.db.execute("UPDATE item SET state='failed',error=? WHERE job_id=? AND state IN ('queued','running')", (error, job_id))
    finally:
        store.close()


@router.post('/imports', status_code=202)
def create_import(body: ImportBody, request: Request):
    with store_for(request) as (store, context, _):
        base, file = _resolve_workspace_file_path(body.upload_path, context)
        upload_root = base / 'upload'
        if not file.is_relative_to(upload_root) or not file.is_file() or file.is_symlink():
            raise HTTPException(403, 'Only files uploaded to this workspace may be imported')
        settings = {}
        if body.extractor == 'model':
            try:
                settings = ontology_model_settings(ServerState.get_workdir(), body.model_ref)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        settings.pop('model_api_key', None)
        job_id = store.intake.submit([file], knowledge_root=platform_knowledge_root(ServerState.get_workdir()), mode=body.extractor,
                                    **settings,
                                    source_id=body.source_id, base_revision=body.base_revision, idempotency_key=body.idempotency_key)
        _worker.submit(run_job, ServerState.get_workdir(), (context.tenant_id, context.user_id, context.workspace_id), job_id)
        return {'job_id': job_id}


@router.get('/jobs')
def list_jobs(request: Request, include_deleted: bool = False):
    with store_for(request) as (store, _, __):
        return {'items': store.intake.list_jobs(include_deleted=include_deleted)}


@router.get('/models')
def list_models(request: Request):
    authorize(request)
    try:
        default = ontology_model_settings(ServerState.get_workdir())['model_ref']
        error = None
    except ValueError as exc:
        default, error = None, str(exc)
    return {'items': ontology_model_options(ServerState.get_workdir()), 'default_model_ref': default, 'error': error}


@router.get('/jobs/{job_id}')
def get_job(job_id: str, request: Request):
    with store_for(request) as (store, _, __):
        return store.intake.inspect(job_id)


@router.delete('/jobs/{job_id}')
def delete_job(job_id: str, request: Request):
    with store_for(request) as (store, _, user):
        try:
            return store.intake.soft_delete(job_id, actor=user.user_id)
        except ValueError as exc:
            if str(exc) == 'active_job_must_be_cancelled_before_delete':
                raise HTTPException(409, str(exc)) from exc
            raise


@router.post('/jobs/{job_id}/restore')
def restore_job(job_id: str, request: Request):
    with store_for(request) as (store, _, user):
        return store.intake.restore(job_id, actor=user.user_id)


@router.post('/jobs/{job_id}/cancel')
def cancel_job(job_id: str, request: Request):
    with store_for(request) as (store, _, __):
        return store.intake.cancel(job_id)


@router.post('/jobs/{job_id}/retry', status_code=202)
def retry_job(job_id: str, request: Request):
    with store_for(request) as (store, context, _):
        store.intake.request_retry(job_id)
        _worker.submit(run_job, ServerState.get_workdir(), (context.tenant_id, context.user_id, context.workspace_id), job_id, True)
        return {'job_id': job_id}


@router.post('/jobs/{job_id}/retry-gleaning', status_code=202)
def retry_gleaning(job_id: str, request: Request):
    with store_for(request) as (store, context, _):
        job = store.intake.inspect(job_id)
        if job['state'] != 'succeeded':
            raise HTTPException(409, 'completed job required')
        _worker.submit(run_job, ServerState.get_workdir(), (context.tenant_id, context.user_id, context.workspace_id), job_id, False, True)
        return {'job_id': job_id, 'scope': 'failed_gleaning_only'}


@router.get('/reviews')
def reviews(request: Request, after: str = '', limit: int = 50):
    with store_for(request) as (store, _, __):
        return store.list_reviews(after=after, limit=limit)


@router.get('/schema')
def schema(request: Request, version: int | None = None):
    with store_for(request) as (store, _, __):
        return store.schema(version)


@router.get('/source-policy')
def source_policy(request: Request):
    authorize(request)
    context = _workspace_context_from_request(request)
    return ontology_source_policy(ServerState.get_workdir(), (context.tenant_id, context.user_id, context.workspace_id))


@router.post('/source-policy')
def set_source_policy(body: SourcePolicyBody, request: Request):
    authorize(request)
    context = _workspace_context_from_request(request)
    return update_ontology_source_policy(ServerState.get_workdir(),
        (context.tenant_id, context.user_id, context.workspace_id), **body.model_dump())


@router.post('/schema/revisions')
def revise_schema(body: SchemaRevisionBody, request: Request):
    with store_for(request) as (store, _, user):
        return store.revise_schema(actor=user.user_id, expected_version=body.expected_version,
                                   classes=[row.model_dump() for row in body.classes],
                                   predicates=[row.model_dump() for row in body.predicates])


@router.post('/facts')
def create_facts(body: ManualFactsBody, request: Request):
    with store_for(request) as (store, _, user):
        return store.create_facts(actor=user.user_id, expected_schema_version=body.expected_schema_version,
                                  entities=[row.model_dump() for row in body.entities],
                                  assertions=[row.model_dump() for row in body.assertions], source_id=body.source_id)


@router.get('/reviews/{item_id}')
def review(item_id: str, request: Request):
    with store_for(request) as (store, _, __):
        return store.review(item_id)


@router.post('/reviews/{item_id}/rebase')
def rebase(item_id: str, body: RebaseBody, request: Request):
    with store_for(request) as (store, _, user):
        return store.rebase(item_id, actor=user.user_id, apply=body.apply)


@router.post('/reviews/{item_id}/decisions')
def decide(item_id: str, body: DecisionBody, request: Request):
    with store_for(request) as (store, _, user):
        return store.decide(item_id, actor=user.user_id, **body.model_dump())


@router.post('/reviews/{item_id}/revisions')
def revise(item_id: str, body: DraftRevisionBody, request: Request):
    with store_for(request) as (store, _, user):
        return store.revise(item_id, actor=user.user_id, expected_revision=body.expected_revision,
                            payload_hash=body.payload_hash, edits=[edit.model_dump() for edit in body.edits])


@router.post('/changesets/{item_id}/publish')
def publish(item_id: str, body: RevisionBody, request: Request):
    with store_for(request) as (store, _, user):
        return store.publish(item_id, actor=user.user_id, expected_revision=body.expected_revision)


@router.post('/assertions/{assertion_id}/withdraw')
def withdraw(assertion_id: str, body: RevisionBody, request: Request):
    with store_for(request) as (store, _, user):
        return store.withdraw(assertion_id, actor=user.user_id, expected_revision=body.expected_revision)


@router.post('/sources/{source_id}/withdraw')
def withdraw_source(source_id: str, body: RevisionBody, request: Request):
    with store_for(request) as (store, _, user):
        return store.withdraw_source(source_id, actor=user.user_id, expected_revision=body.expected_revision)


@router.get('/query')
def query(request: Request, entity_id: str = '', text: str = '', predicate: str = '', scope: str = 'both', after: str = '', limit: int = 50,
          evidence_basis: str = '', polarity: str = '', qualifiers: str = '{}'):
    with store_for(request) as (store, _, __):
        if len(qualifiers) > 8000:
            raise ValueError('qualifier filters too long')
        return store.query(entity_id=entity_id, text=text, predicate=predicate, scope=scope, after=after, limit=limit,
                           evidence_basis=evidence_basis, polarity=polarity, qualifiers=json.loads(qualifiers))


@router.get('/graph')
def graph(request: Request, text: str = '', entity_id: str = '', scope: str = 'both', limit: int = 30, depth: int = 2, ranking: str = 'coverage'):
    with store_for(request) as (store, _, __):
        return store.graph(text=text, entity_id=entity_id, scope=scope, limit=limit, depth=depth, ranking=ranking)


@router.post('/reports/validate')
def validate_report(body: ReportBody, request: Request):
    with store_for(request) as (store, _, user):
        return store.validate_report([c.model_dump() for c in body.claims], actor=user.user_id)


@router.get('/conflicts')
def conflict_cases(request: Request, after: str = '', limit: int = 30):
    with store_for(request) as (store, _, __):
        return store.list_conflict_cases(after=after, limit=limit)


@router.get('/conflicts/pair')
def conflict_pair(request: Request, left_id: str, right_id: str, after: str = ''):
    with store_for(request) as (store, _, __):
        return store.conflict_case(left_id, right_id, after=after)


@router.post('/conflicts/decisions')
def conflict_decision(body: ConflictDecisionBody, request: Request):
    with store_for(request) as (store, _, user):
        return store.record_conflict_decision(actor=user.user_id, **body.model_dump())


@router.post('/reports/{report_id}/recheck')
def recheck_report(report_id: str, request: Request):
    with store_for(request) as (store, _, user):
        return store.recheck_report(report_id, actor=user.user_id)


@router.get('/reports/{report_id}')
def deliver_report(report_id: str, request: Request):
    with store_for(request) as (store, _, __):
        return store.validate_report(None, report_id=report_id)
