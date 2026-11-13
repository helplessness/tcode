"""Immutable, owner-scoped research versions; never infer missing history."""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any

from .models import canonical_json, sha256_text, utc_now_iso
from .store import ResearchEventStore

FIELDS = {
    "project": ("research_projects", "research_id", ("title", "description", "status", "origin_candidate_ids_json")),
    "hypothesis": ("research_hypotheses", "hypothesis_id", ("statement", "rationale", "status", "test_plan", "falsification_condition", "evidence_refs_json", "counter_evidence_refs_json")),
    "experiment": ("research_experiments", "experiment_id", ("title", "reason", "status", "decision", "result_summary", "hypothesis_id", "hypothesis_revision", "parent_experiment_id", "workflow_run_id", "run_snapshot_json", "metrics_json", "input_artifacts_json", "output_artifacts_json")),
    "finding": ("research_findings", "finding_id", ("statement", "status", "provenance", "scope_json", "evidence_ids_json", "source_event_ids_json", "current_revision")),
    "conflict": ("research_conflicts", "conflict_id", ("summary", "status", "conflict_type", "original_refs_json", "challenging_refs_json", "original_evidence_ids_json", "challenging_evidence_ids_json", "current_resolution_id")),
}
SOURCE_FIELDS = {"origin_candidate_ids", "evidence_refs", "counter_evidence_refs", "evidence_ids", "source_event_ids", "original_refs", "challenging_refs", "original_evidence_ids", "challenging_evidence_ids", "input_artifacts", "output_artifacts", "workflow_run_id", "run_snapshot", "evidence_relations"}

class ResearchSnapshotStore:
    def __init__(self, *, tenant_id: str, user_id: str, workspace_id: str, workdir: str | None = None):
        self.owner = (tenant_id, user_id, workspace_id)
        if not all(self.owner): raise ValueError("研究版本需要完整账号与工作区")
        self.workdir = workdir

    @contextmanager
    def connection(self):
        ResearchEventStore.ensure_initialized(self.workdir)
        conn = ResearchEventStore._connect(self.workdir)
        try: yield conn
        finally: conn.close()

    def project(self, conn, research_id):
        row = conn.execute("SELECT * FROM research_projects WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=?", (*self.owner, research_id)).fetchone()
        if row is None: raise LookupError("研究课题不存在或无权访问")
        return row

    @staticmethod
    def receipt(row):
        return {key: row[key] for key in ("snapshot_id", "research_id", "title", "captured_at", "content_hash", "hidden")}

    def capture(self, research_id: str, title: str, request_id: str):
        title = title.strip()
        if not title or len(title) > 160 or not request_id or len(request_id) > 180: raise ValueError("请输入有效版本名称与请求标识")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self.project(conn, research_id)
                existing = conn.execute("SELECT * FROM research_snapshots WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? AND request_id=?", (*self.owner, research_id, request_id)).fetchone()
                if existing: conn.rollback(); return self.receipt(existing)
                objects = []
                for kind, (table, key, fields) in FIELDS.items():
                    rows = conn.execute(f"SELECT * FROM {table} WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? ORDER BY {key} LIMIT 2001", (*self.owner, research_id)).fetchall()
                    if len(objects) + len(rows) > 2000: raise ValueError("研究对象超过版本上限 2000，不能保存不完整快照")
                    for row in rows:
                        data = {}
                        for field in fields:
                            value = row[field]
                            name = field.removesuffix("_json")
                            if field.endswith("_json"):
                                value = json.loads(value)
                                if name in SOURCE_FIELDS and isinstance(value, list): value = sorted(value, key=canonical_json)
                            data[name] = value
                        if kind == "hypothesis":
                            links = conn.execute("SELECT * FROM research_knowledge_links WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? AND subject_type='hypothesis' AND subject_id=? AND superseded_by='' ORDER BY link_key", (*self.owner, research_id, row[key])).fetchall()
                            from .hypothesis_map import HypothesisMap
                            source_service = HypothesisMap(tenant_id=self.owner[0], user_id=self.owner[1], workspace_id=self.owner[2], workdir=self.workdir)
                            relations = []
                            for link in links:
                                metadata = json.loads(link["metadata_json"])
                                source = source_service.source(link["object_type"], link["object_id"])
                                confirmed = (link["discovered_by"] == "user" and metadata.get("review_status") == "confirmed"
                                    and metadata.get("hypothesis_revision") == row["revision"]
                                    and metadata.get("reviewed_source_hash") == source["content_hash"] and source["status"] == "active")
                                relations.append({"source": source["title"] if source["status"] != "missing" else metadata.get("source_title", "来源（名称暂不可用）"),
                                    "relation": link["relation"], "state": link["state"], "reason": metadata.get("reason", ""), "scope": metadata.get("scope", ""),
                                    "review_status": "confirmed" if confirmed else "pending", "source_hash": source["content_hash"], "source_status": source["status"], "revision": link["revision"]})
                            data["evidence_relations"] = relations
                        objects.append({"id": "project" if kind == "project" else f"{kind}:{row[key]}", "kind": kind, "object_id": row[key], "title": str(data.get("title") or data.get("statement") or data.get("summary") or {"project": "未命名课题", "hypothesis": "未命名假说", "experiment": "未命名实验", "finding": "未命名结论", "conflict": "未命名冲突"}[kind]), "data": data})
                document = {"schema": "tcode.research-snapshot.v1", "objects": objects}
                content = canonical_json(document)
                if len(content.encode()) > 4_000_000: raise ValueError("研究版本超过 4 MB，未保存")
                sid = f"rs-{uuid.uuid4().hex}"
                conn.execute("INSERT INTO research_snapshots(snapshot_id,tenant_id,user_id,workspace_id,research_id,title,captured_at,document_json,content_hash,request_id) VALUES(?,?,?,?,?,?,?,?,?,?)", (sid, *self.owner, research_id, title, utc_now_iso(), content, sha256_text(content), request_id))
                conn.commit()
                return self.receipt(conn.execute("SELECT * FROM research_snapshots WHERE snapshot_id=?", (sid,)).fetchone())
            except Exception:
                conn.rollback(); raise

    def list(self, research_id: str, *, offset: int = 0, hidden: bool = False):
        if offset < 0: raise ValueError("分页位置无效")
        with self.connection() as conn:
            self.project(conn, research_id)
            rows = conn.execute("SELECT snapshot_id,research_id,title,captured_at,content_hash,hidden FROM research_snapshots WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? AND hidden=? ORDER BY captured_at DESC,snapshot_id DESC LIMIT 51 OFFSET ?", (*self.owner, research_id, int(hidden), offset)).fetchall()
            return {"items": [self.receipt(r) for r in rows[:50]], "next_offset": offset + 50 if len(rows) > 50 else None, "history_notice": "仅比较实际保存的研究版本；保存之前的历史不能完整还原。"}

    def read(self, snapshot_id: str):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM research_snapshots WHERE tenant_id=? AND user_id=? AND workspace_id=? AND snapshot_id=?", (*self.owner, snapshot_id)).fetchone()
            if row is None: raise LookupError("研究版本不存在或无权访问")
            self.project(conn, row["research_id"])
            if sha256_text(row["document_json"]) != row["content_hash"]: raise ValueError("研究版本完整性检查失败")
            return {**self.receipt(row), "document": json.loads(row["document_json"])}

    def hide(self, snapshot_id: str, hidden: bool):
        value = self.read(snapshot_id)
        with self.connection() as conn:
            conn.execute("UPDATE research_snapshots SET hidden=? WHERE snapshot_id=? AND tenant_id=? AND user_id=? AND workspace_id=?", (int(hidden), snapshot_id, *self.owner))
        return {**{k: v for k, v in value.items() if k != "document"}, "hidden": int(hidden)}

    def compare(self, base_id: str, head_id: str, *, offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 50: raise ValueError("比较分页无效")
        base, head = self.read(base_id), self.read(head_id)
        if base["research_id"] != head["research_id"]: raise ValueError("请选择同一课题的两个版本")
        if base["captured_at"] > head["captured_at"]: raise ValueError("比较前版本必须早于比较后版本")
        before = {o["id"]: o for o in base["document"]["objects"]}
        after = {o["id"]: o for o in head["document"]["objects"]}
        def names(data, objects):
            refs = {"hypothesis_id": "hypothesis", "parent_experiment_id": "experiment"}
            return {k: objects.get(f"{kind}:{data.get(k)}", {}).get("title", "") for k, kind in refs.items() if data.get(k)}
        changes = []
        for oid in sorted(before.keys() | after.keys()):
            b, a = before.get(oid), after.get(oid)
            fields = sorted(k for k in ((b or a)["data"].keys() | (a or b)["data"].keys()) if not b or not a or b["data"].get(k) != a["data"].get(k))
            if not fields: continue
            status = "added" if b is None else "removed" if a is None else "changed"
            source_changed = any(k in fields and ((b and b["data"].get(k)) or (a and a["data"].get(k))) for k in SOURCE_FIELDS)
            changes.append({"id": oid, "kind": (a or b)["kind"], "title": (a or b)["title"], "status": status, "before_names": names(b["data"], before) if b else {}, "after_names": names(a["data"], after) if a else {}, "fields": fields, "source_changed": source_changed, "before": {k: b["data"].get(k) for k in fields} if b else None, "after": {k: a["data"].get(k) for k in fields} if a else None})
        return {"schema": "tcode.research-changes.v1", "research_id": base["research_id"], "base": {k:v for k,v in base.items() if k != "document"}, "head": {k:v for k,v in head.items() if k != "document"}, "counts": {s: sum(c["status"] == s for c in changes) for s in ("added", "removed", "changed")}, "source_changed_count": sum(c["source_changed"] for c in changes), "total": len(changes), "changes": changes[offset:offset+limit], "next_offset": offset+limit if len(changes)>offset+limit else None, "history_notice": "差异来自已保存字段，不推断变化原因、因果或科研结论真假。"}


def comparison_summary(result):
    """Give the model readable facts; opaque IDs stay in the UI receipt."""
    labels = {"status": "状态", "decision": "实验判断", "result_summary": "结果摘要", "reason": "实验目的", "title": "名称", "statement": "判断内容", "hypothesis_id": "关联假说", "parent_experiment_id": "上级实验", "hypothesis_revision": "假说版本", "evidence_relations": "假说证据关系"}
    values = {"planned": "待执行", "completed": "已完成", "running": "进行中", "inconclusive": "尚无定论", "frozen": "已冻结", "failed": "失败", "active": "进行中", "testing": "验证中", "approved": "已批准"}
    def display(key, value, names):
        if value is None or value == "": return "未记录"
        if key in ("hypothesis_id", "parent_experiment_id"): return names.get(key) or "未保存名称"
        if key == "workflow_run_id": return "已关联计算任务"
        if key == "current_resolution_id": return "已保存处理记录"
        if key == "evidence_relations" and isinstance(value, list):
            from .hypothesis_map import RELATIONS
            rows = [f"{r.get('source') or '来源名称未记录'}：{RELATIONS.get(r.get('relation'), '待判断')}；"
                f"{'用户已核对' if r.get('review_status') == 'confirmed' else '待核对'}；"
                f"理由：{str(r.get('reason') or '未记录')[:240]}；适用条件：{str(r.get('scope') or '未记录')[:160]}"
                for r in value[:5] if isinstance(r, dict)]
            return "\n".join(rows) + (f"\n共 {len(value)} 条，完整内容请打开比较页。" if len(value) > 5 else "") if rows else "未记录"
        if isinstance(value, (list, dict)): return f"已记录 {len(value)} 项相关信息" if value else "未记录"
        return values.get(str(value), str(value))
    changes = []
    for change in result["changes"]:
        fields = []
        for key in change["fields"]:
            fields.append({"内容": labels.get(key, "来源信息" if key in SOURCE_FIELDS else "补充信息"), **{label: display(key, change[side].get(key), change.get(f"{side}_names", {})) if change[side] else "此版本不存在" for label, side in (("比较前", "before"), ("比较后", "after"))}})
        changes.append({"名称": change["title"], "变化": {"added": "新增", "removed": "移除", "changed": "修改"}[change["status"]], "前后内容": fields})
    return {"比较前版本": result["base"]["title"], "比较后版本": result["head"]["title"], "新增": result["counts"]["added"], "修改": result["counts"]["changed"], "移除": result["counts"]["removed"], "来源引用变化": result["source_changed_count"], "变化": changes, "还有后续变化页": result["next_offset"] is not None, "说明": result["history_notice"], "展示要求": "只使用名称和中文说明；不要输出内部标识、字段名或代码。平台已经附上可点击摘要。"}
