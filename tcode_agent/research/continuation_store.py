"""Durable budgets and automatic round advancement for continuous research."""
from __future__ import annotations

import sqlite3
import threading
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from ..workflow.manager import WorkflowRunManager
from .experiment_store import ResearchExperimentStore
from .models import utc_now_iso
from .store import ResearchEventStore

TERMINAL = {"completed", "failed", "error", "cancelled", "canceled", "terminated"}
ACTIVE = {"pending", "queued", "running", "validating", "waiting_external_job", "collecting", "review_pending", "revising", "recovering"}

# ponytail: process-local lock is sufficient for the desktop runtime; replace
# with a DB lease if multiple server processes can advance one workspace.
_ADVANCE_LOCK = threading.Lock()

@dataclass(frozen=True)
class ResearchContinuation:
    continuation_id: str; tenant_id: str; user_id: str; workspace_id: str; research_id: str
    status: str; max_runs: int; max_minutes: int; max_tokens: int; max_tool_calls: int; max_failures: int; max_cost_micros: int
    runs_started: int; tokens_used: int; tool_calls_used: int; failures_used: int; cost_used_micros: int
    checkpoint_every: int; selected_hypothesis_id: str; stop_reason: str; best_experiment_id: str; final_summary: str; idempotency_key: str
    created_at: str; updated_at: str
    cost_status: str = 'unknown'
    active_runs: int | None = None
    def to_dict(self) -> dict[str, Any]: return asdict(self)

class ResearchContinuationStore:
    @staticmethod
    def _connect(workdir: str | None) -> sqlite3.Connection:
        ResearchEventStore.ensure_initialized(workdir); return ResearchEventStore._connect(workdir)

    @classmethod
    def get(cls, research_id: str, *, tenant_id: str,user_id: str,workspace_id: str,workdir: str|None=None) -> ResearchContinuation|None:
        conn=cls._connect(workdir)
        try: row=conn.execute("SELECT * FROM research_continuations WHERE research_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",(research_id,tenant_id,user_id,workspace_id)).fetchone()
        finally: conn.close()
        return ResearchContinuation(**dict(row)) if row else None

    @classmethod
    def start(cls,research_id:str,*,max_runs:int,max_minutes:int,max_tokens:int=0,max_tool_calls:int=0,max_failures:int=0,max_cost_micros:int=0,checkpoint_every:int=1,tenant_id:str,user_id:str,workspace_id:str,workdir:str|None=None)->ResearchContinuation:
        limits=(max_runs,max_minutes,max_tokens,max_tool_calls,max_failures,max_cost_micros)
        if max_runs<1 or max_minutes<1 or any(value<0 for value in limits): raise ValueError("continuous research requires hard run and time limits")
        now=utc_now_iso(); conn=cls._connect(workdir)
        try:
            project=conn.execute("SELECT status FROM research_projects WHERE research_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",(research_id,tenant_id,user_id,workspace_id)).fetchone()
            if project is None: raise LookupError("research project not found")
            if project["status"]=="archived": raise ValueError("archived research project is read-only")
            approved=conn.execute("SELECT hypothesis_id FROM research_hypotheses WHERE research_id=? AND status IN ('approved','testing') ORDER BY updated_at LIMIT 1",(research_id,)).fetchone()
            seed=conn.execute("SELECT workflow_run_id FROM research_experiments WHERE research_id=? AND workflow_run_id!='' ORDER BY created_at DESC LIMIT 1",(research_id,)).fetchone()
            if approved is None: raise ValueError("at least one approved hypothesis is required")
            key=f"{research_id}:{':'.join(map(str,limits))}:{checkpoint_every}"
            columns="continuation_id,tenant_id,user_id,workspace_id,research_id,status,max_runs,max_minutes,max_tokens,max_tool_calls,max_failures,max_cost_micros,runs_started,tokens_used,tool_calls_used,failures_used,cost_used_micros,checkpoint_every,selected_hypothesis_id,stop_reason,best_experiment_id,final_summary,idempotency_key,created_at,updated_at"
            values=(f"continuation-{uuid.uuid4().hex}",tenant_id,user_id,workspace_id,research_id,"running",*limits,0,0,0,0,0,max(1,checkpoint_every),approved["hypothesis_id"],"","","",key,now,now)
            conn.execute(f"INSERT INTO research_continuations({columns}) VALUES({','.join('?' for _ in values)}) ON CONFLICT(tenant_id,user_id,workspace_id,research_id) DO UPDATE SET status='running',max_runs=excluded.max_runs,max_minutes=excluded.max_minutes,max_tokens=excluded.max_tokens,max_tool_calls=excluded.max_tool_calls,max_failures=excluded.max_failures,max_cost_micros=excluded.max_cost_micros,runs_started=0,tokens_used=0,tool_calls_used=0,failures_used=0,cost_used_micros=0,checkpoint_every=excluded.checkpoint_every,selected_hypothesis_id=excluded.selected_hypothesis_id,stop_reason='',best_experiment_id='',final_summary='',idempotency_key=excluded.idempotency_key,created_at=excluded.created_at,updated_at=excluded.updated_at",values)
            conn.commit()
        finally: conn.close()
        if seed is not None:
            seed_run=WorkflowRunManager.get(seed["workflow_run_id"],workdir=workdir)
            if seed_run is not None:
                WorkflowRunManager.merge_input_payload(seed_run.run_id,metadata={"research_id":research_id,"tenant_id":tenant_id,"user_id":user_id,"workspace_id":workspace_id},workdir=workdir,expected_version=seed_run.version)
        return cls.reconcile(research_id,tenant_id=tenant_id,user_id=user_id,workspace_id=workspace_id,workdir=workdir)

    @classmethod
    def set_status(cls,research_id:str,status:str,*,tenant_id:str,user_id:str,workspace_id:str,workdir:str|None=None)->ResearchContinuation:
        if status not in {'running','paused'}: raise ValueError('invalid continuation status')
        conn=cls._connect(workdir)
        try:
            cur=conn.execute("UPDATE research_continuations SET status=?,stop_reason='',updated_at=? WHERE research_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",(status,utc_now_iso(),research_id,tenant_id,user_id,workspace_id))
            if cur.rowcount!=1: raise LookupError('continuous research policy not found')
            conn.commit()
        finally: conn.close()
        return cls.reconcile(research_id,tenant_id=tenant_id,user_id=user_id,workspace_id=workspace_id,workdir=workdir)

    @staticmethod
    def _usage(run: Any) -> tuple[int,int,int,int,bool,bool]:
        """Tokens, calls, failures, reported cost, complete pricing, any pricing.

        Completeness describes observed usage events, not an external invoice.
        """
        tokens=tools=cost=0
        priced = False
        missing = False
        for event in list(getattr(run,"events",[]) or []):
            payload=dict(getattr(event,"payload",{}) or {})
            usage=payload.get("usage") if isinstance(payload.get("usage"),dict) else {}
            tokens += int(usage.get("total_tokens") or payload.get("token_count") or 0)
            amount = next((value for value in (usage.get('cost_micros'), payload.get('cost_micros'),
                           usage.get('cost_usd')) if value is not None), None)
            valid = type(amount) in (int, float) and math.isfinite(amount) and amount >= 0
            if valid:
                cost += round(amount * 1_000_000) if usage.get('cost_micros') is None and payload.get('cost_micros') is None else round(amount)
                priced = True
            elif usage or payload.get('token_count') or amount is not None:
                missing = True
            kind=str(getattr(event,"kind","") or "").lower()
            tools += int("tool" in kind and any(word in kind for word in ("completed","failed","result")))
        failures=int(str(getattr(run,"status","")).lower() in {"failed","error"})
        return tokens,tools,failures,cost,priced and not missing,priced

    @classmethod
    def reconcile(cls,research_id:str,*,tenant_id:str,user_id:str,workspace_id:str,workdir:str|None=None)->ResearchContinuation:
        conn=cls._connect(workdir)
        try:
            row=conn.execute("SELECT * FROM research_continuations WHERE research_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",(research_id,tenant_id,user_id,workspace_id)).fetchone()
            if row is None: raise LookupError('continuous research policy not found')
            ids=[str(item[0]) for item in conn.execute("SELECT DISTINCT workflow_run_id FROM research_experiments WHERE research_id=? AND workflow_run_id!='' AND created_at>=?",(research_id,row['created_at'])).fetchall()]
            all_ids=[str(item[0]) for item in conn.execute("SELECT DISTINCT workflow_run_id FROM research_experiments WHERE research_id=? AND workflow_run_id!=''",(research_id,)).fetchall()]
            all_runs={run_id: WorkflowRunManager.get(run_id,workdir=workdir) for run_id in all_ids}
            loaded=[all_runs[run_id] for run_id in ids]
            usages=[cls._usage(run) for run in loaded if run is not None]
            cost_status = 'known' if len(usages)==len(ids) and all(x[4] for x in usages) else ('partial' if any(x[5] for x in usages) else 'unknown')
            active_runs = sum(str(run.status).lower() not in TERMINAL for run in all_runs.values()) if all(run is not None for run in all_runs.values()) else None
            runs=len(ids); tokens=sum(x[0] for x in usages); tools=sum(x[1] for x in usages); failures=sum(x[2] for x in usages); cost=sum(x[3] for x in usages)
            elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(str(row['created_at']).replace('Z','+00:00'))).total_seconds()/60
            status=str(row['status']); reason=''
            checks=((runs>=row['max_runs'],'run_limit_reached'),(elapsed>=row['max_minutes'],'time_limit_reached'),(row['max_tokens']>0 and tokens>=row['max_tokens'],'token_limit_reached'),(row['max_tool_calls']>0 and tools>=row['max_tool_calls'],'tool_call_limit_reached'),(row['max_failures']>0 and failures>=row['max_failures'],'failure_limit_reached'),(row['max_cost_micros']>0 and cost>=row['max_cost_micros'],'cost_limit_reached'))
            for reached,code in checks:
                if reached: status,reason='stopped',code; break
            best_id=str(row['best_experiment_id']); final=str(row['final_summary'])
            if status=='stopped':
                best=conn.execute("SELECT experiment_id,title,result_summary,decision FROM research_experiments WHERE research_id=? AND frozen_at!='' ORDER BY CASE decision WHEN 'recommended' THEN 0 WHEN 'retained' THEN 1 ELSE 2 END,updated_at DESC LIMIT 1",(research_id,)).fetchone()
                if best is not None: best_id=str(best['experiment_id']); final=str(best['result_summary'] or best['title'])
            conn.execute("UPDATE research_continuations SET status=?,runs_started=?,tokens_used=?,tool_calls_used=?,failures_used=?,cost_used_micros=?,stop_reason=?,best_experiment_id=?,final_summary=?,updated_at=? WHERE continuation_id=?",(status,runs,tokens,tools,failures,cost,reason,best_id,final,utc_now_iso(),row['continuation_id']))
            conn.commit(); current=conn.execute("SELECT * FROM research_continuations WHERE continuation_id=?",(row['continuation_id'],)).fetchone()
        finally: conn.close()
        return ResearchContinuation(**dict(current), cost_status=cost_status, active_runs=active_runs)

    @classmethod
    def advance(cls,research_id:str,*,tenant_id:str,user_id:str,workspace_id:str,workdir:str|None=None):
        with _ADVANCE_LOCK:
            return cls._advance_locked(research_id,tenant_id=tenant_id,user_id=user_id,workspace_id=workspace_id,workdir=workdir)

    @classmethod
    def _advance_locked(cls,research_id:str,*,tenant_id:str,user_id:str,workspace_id:str,workdir:str|None=None):
        policy=cls.reconcile(research_id,tenant_id=tenant_id,user_id=user_id,workspace_id=workspace_id,workdir=workdir)
        if policy.status!="running": return None
        conn=cls._connect(workdir)
        try:
            rows=conn.execute("SELECT * FROM research_experiments WHERE research_id=? AND workflow_run_id!='' ORDER BY created_at",(research_id,)).fetchall()
            runs=[(row,WorkflowRunManager.get(str(row['workflow_run_id']),workdir=workdir)) for row in rows]
            active=[run for _,run in runs if run is not None and str(run.status).lower() in ACTIVE]
            if active: return active[-1]
            if not runs or runs[-1][1] is None: return None
            parent,seed=runs[-1]
            if str(seed.status).lower() not in TERMINAL: return None
            snapshot = json.loads(str(parent["run_snapshot_json"] or "{}"))
            current_snapshot = ResearchExperimentStore.workflow_run_snapshot(seed.run_id, workdir)
            if snapshot.get("template_digest") and current_snapshot.get("template_digest") != snapshot.get("template_digest"):
                conn.execute("UPDATE research_continuations SET status='paused',stop_reason='workflow_template_changed',updated_at=? WHERE research_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",(utc_now_iso(),research_id,tenant_id,user_id,workspace_id))
                conn.commit()
                return None
            expected_environment = dict(snapshot.get("environment") or {})
            current_environment = ResearchExperimentStore.workspace_git_state(workdir)
            if str(parent["execution_mode"] or "shared_workspace") == "shared_workspace":
                for field in ("git_commit", "workspace_digest"):
                    if expected_environment.get(field) and current_environment.get(field) != expected_environment.get(field):
                        conn.execute("UPDATE research_continuations SET status='paused',stop_reason='execution_environment_changed',updated_at=? WHERE research_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",(utc_now_iso(),research_id,tenant_id,user_id,workspace_id))
                        conn.commit()
                        return None
            existing=conn.execute("SELECT workflow_run_id FROM research_experiments WHERE parent_experiment_id=? AND workflow_run_id!='' LIMIT 1",(parent['experiment_id'],)).fetchone()
            if existing: return WorkflowRunManager.get(existing['workflow_run_id'],workdir=workdir)
            hypothesis=conn.execute("SELECT h.*,COUNT(e.experiment_id) AS uses FROM research_hypotheses h LEFT JOIN research_experiments e ON e.hypothesis_id=h.hypothesis_id WHERE h.research_id=? AND h.status IN ('approved','testing') GROUP BY h.hypothesis_id ORDER BY uses,h.updated_at LIMIT 1",(research_id,)).fetchone()
            if hypothesis is None: return None
        finally: conn.close()
        hypothesis_context = {
            "id": str(hypothesis["hypothesis_id"]),
            "revision": int(hypothesis["revision"]),
            "statement": str(hypothesis["statement"]),
            "rationale": str(hypothesis["rationale"]),
            "test_plan": str(hypothesis["test_plan"]),
            "falsification_condition": str(hypothesis["falsification_condition"]),
        }
        inputs = {**dict(seed.input_payload or {}), "research_hypothesis": hypothesis_context}
        instruction = (
            f"\n\n[研究实验约束]\n假设：{hypothesis_context['statement']}\n"
            f"验证计划：{hypothesis_context['test_plan']}\n"
            f"证伪条件：{hypothesis_context['falsification_condition']}\n"
            "本轮必须围绕该假设执行，并分别报告支持证据、反证和验收结果。"
        )
        for field in ("request", "goal", "prompt", "query", "task", "user_request"):
            if isinstance(inputs.get(field), str) and inputs[field].strip():
                inputs[field] = inputs[field].rstrip() + instruction
                break
        else:
            inputs["research_instruction"] = instruction.strip()
        run=WorkflowRunManager.create_run(template_key=seed.template_key,session_id=seed.session_id,input_payload=inputs,metadata={**dict(seed.metadata or {}),"created_from":"continuous_research","research_id":research_id,"research_hypothesis_id":hypothesis['hypothesis_id'],"parent_workflow_run_id":seed.run_id},workdir=workdir)
        ResearchExperimentStore.create_experiment(research_id=research_id,hypothesis_id=hypothesis['hypothesis_id'],title=f"持续研究第 {policy.runs_started + 1} 轮",reason="持续研究控制器在上一轮结束后自动创建",workflow_run_id=run.run_id,parent_experiment_id=parent['experiment_id'],execution_mode=str(parent['execution_mode'] or 'shared_workspace'),created_by="continuous_research",tenant_id=tenant_id,user_id=user_id,workspace_id=workspace_id,workdir=workdir)
        conn=cls._connect(workdir)
        try:
            conn.execute("UPDATE research_continuations SET selected_hypothesis_id=?,updated_at=? WHERE research_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",(hypothesis['hypothesis_id'],utc_now_iso(),research_id,tenant_id,user_id,workspace_id))
            conn.commit()
        finally: conn.close()
        return run

    @classmethod
    def advance_all(cls, workdir: str | None = None) -> list[Any]:
        conn=cls._connect(workdir)
        try: rows=conn.execute("SELECT research_id,tenant_id,user_id,workspace_id FROM research_continuations WHERE status='running'").fetchall()
        finally: conn.close()
        runs=[]
        for row in rows:
            try:
                run=cls.advance(str(row['research_id']),tenant_id=str(row['tenant_id']),user_id=str(row['user_id']),workspace_id=str(row['workspace_id']),workdir=workdir)
                if run is not None: runs.append(run)
            except (LookupError,ValueError,KeyError): pass
        return runs
