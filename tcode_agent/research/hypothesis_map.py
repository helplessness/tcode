"""Read-only hypothesis projection over existing versioned knowledge links."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .experiment_store import ResearchExperimentStore
from .knowledge import ResearchKnowledgeManager
from .knowledge_store import ResearchKnowledgeStore
from ..evidence.store import EvidenceStore

RELATIONS = {"supports": "支持", "contradicts": "反对", "context_only": "相关但不足", "references": "待判断"}
STATUSES = {"proposed": "待批准", "approved": "已批准", "testing": "验证中", "supported": "已有支持", "refuted": "已证伪", "withdrawn": "已撤回"}


class HypothesisMap:
    def __init__(self, **owner):
        self.manager = ResearchKnowledgeManager(**owner)
        self.owner = self.manager.owner
        self.workdir = self.manager.workdir.as_posix()

    def _hypotheses(self, research_id):
        return ResearchExperimentStore.list_hypotheses(research_id, **self.owner, workdir=self.workdir)

    def _links(self, research_id):
        try:
            self.manager._project(research_id)
        except ValueError:
            raise LookupError("课题不存在或无权访问") from None
        self.research_id = research_id
        return ResearchKnowledgeStore.list_links(research_id, **self.owner, workdir=self.workdir, limit=10000)

    def _paper_original(self, doc_id):
        record, digest = self.manager._paper_record(doc_id)
        if not record:
            return {}, digest, False
        path = Path(str(record.get("pdf_path") or "")).expanduser().resolve()
        try:
            path.relative_to((self.manager.library_root / "docs").resolve())
            if not path.is_file() or path.stat().st_size > 100_000_000:
                return record, digest, False
            with path.open("rb") as stream:
                original_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            expected = str(record.get("sha256") or "")
            return record, hashlib.sha256((digest + original_hash).encode()).hexdigest(), bool(expected and expected == original_hash)
        except (ValueError, OSError):
            return record, digest, False

    def source(self, kind, object_id):
        """Resolve current source; unavailable originals are never reconstructed."""
        data = {"object_type": kind, "object_id": object_id, "title": "来源（名称暂不可用）", "status": "missing", "content": "", "locator": "", "content_hash": ""}
        if kind == "evidence" and not object_id.startswith("event:"):
            item = EvidenceStore.get(object_id, workdir=self.workdir)
            if item:
                from ..evidence.assets import check_assets
                assets = check_assets(item, workdir=self.workdir)
                codes = {issue['code'] for issue in assets['issues']}
                original_missing = bool(codes & {'asset_missing', 'asset_unreadable', 'asset_outside_workspace', 'asset_hash_mismatch'})
                data.update(title=item.title or "未命名证据", content=item.content[:20000], locator=item.locator, status=item.status, content_hash=item.content_hash, source_type=item.source_type,
                            source_status=item.status, content_truncated=len(item.content) > 20000,
                            original_available=False if original_missing else True if assets['checked_file_count'] else None,
                            original_verified=bool(assets['file_count'] and assets['verified_file_count'] == assets['file_count'] and not original_missing),
                            asset_issues=assets['issues'], asset_checks_truncated=assets['truncated'])
                doc_id = self.manager._evidence_doc_id(item)
                if doc_id:
                    record, digest, available = self._paper_original(doc_id)
                    data["content_hash"] = hashlib.sha256((item.content_hash + digest).encode()).hexdigest()
                    original_missing = original_missing or not available
                    data['original_available'] = not original_missing
                    data['original_verified'] = available and not original_missing
                if original_missing:
                    # Retraction/supersession remains authoritative even if files also disappear.
                    if item.status == 'active':
                        data['status'] = 'missing'
                    data['source_notice'] = '原始来源缺失、不可读取或校验不一致；保存的摘录不能冒充当前已核对原文。'
                elif not doc_id and 'asset_verification_pending' in codes:
                    if item.status == 'active':
                        data['status'] = 'stale'
                    data['source_notice'] = '来源文件发生变化，需重新校验后使用。'
                elif 'asset_baseline_missing' in codes and not data.get('original_verified'):
                    data['source_notice'] = '来源文件可读取，但尚未建立内容校验基线；不能据此宣称原文内容已验证。'
                elif 'asset_remote_not_checked' in codes:
                    data['source_notice'] = '远程原文未检查，本次仅读取本地保存记录。'
                if assets['truncated']:
                    if item.status == 'active' and not original_missing:
                        data['status'] = 'stale'
                    data['source_notice'] = data.get('source_notice', '') + '关联文件数量超出检查上限，来源校验不完整。'
        elif kind in {"paper", "passage"}:
            doc_id = object_id.split(":passage:", 1)[0]
            record, digest, available = self._paper_original(doc_id)
            if record:
                data.update(title=(record.get("metadata") or {}).get("title") or "未命名论文", content_hash=digest, status="active" if available else "missing")
                if kind == "passage":
                    passage = next((p for p in record.get("l1_passage_summaries", []) if isinstance(p, dict) and p.get("passage_id") == object_id), None)
                    if passage:
                        chunks = {c.get("chunk_id"): c for c in record.get("l2_chunks", []) if isinstance(c, dict)}
                        original = "\n\n".join(str(chunks[c].get("text") or "") for c in passage.get("source_chunk_ids", []) if c in chunks)
                        data.update(content=(original or str(passage.get("summary_text") or ""))[:20000], locator=str(passage.get("locator") or passage.get("page_span") or ""))
                        data["original_text"] = bool(original)
                    else:
                        data.update(status="missing")
                else:
                    data["content"] = "\n\n".join(str(c.get("text") or "") for c in record.get("l2_chunks", []) if isinstance(c, dict))[:20000]
                    data["original_text"] = bool(data["content"])
        elif kind == "evidence" and object_id.startswith("event:"):
            event = self.manager._event_snapshot(object_id[6:])
            if event:
                data.update(title="研究活动记录", status="active", content=str((event.get("payload") or {}).get("preview") or "已保存研究活动；这不是独立的文献证据。")[:20000], content_hash=event.get("content_hash", ""))
        elif kind in {"wiki_page", "wiki_claim"}:
            if not hasattr(self, "wiki"):
                self.wiki = self.manager._wiki_snapshot()
            pages, claims = self.wiki
            item = (pages if kind == "wiki_page" else claims).get(object_id)
            if item:
                from .models import canonical_json, sha256_text
                data.update(title=item.get("title") or item.get("statement") or "未命名知识资料", content=str(item.get("content") or item.get("body") or item.get("statement") or "请在知识资料中核对完整原文。")[:20000], status="active" if item.get("status") not in {"retracted", "archived"} else "retracted", content_hash=sha256_text(canonical_json(item)))
        category = str(data.get("source_type") or "")
        data["source_label"] = {"paper":"文献提取内容，需核对原文", "model_inference":"模型推断", "agent":"模型记录", "computation":"计算结果", "workflow":"计算结果", "tool":"工具结果", "user":"用户记录", "user_confirmed":"用户记录"}.get(category, "知识资料" if kind.startswith("wiki_") else "文献资料" if kind in {"paper","passage"} else "来源类型未说明")
        data["content_label"] = ("原文提取文本，请对照 PDF 核对" if data.get("original_text") else "提取的段落摘要，不是原文") if kind == "passage" else "原文提取文本，请对照 PDF 核对" if kind == "paper" and data.get("original_text") else "论文记录未包含原文文本" if kind == "paper" else "来源记录正文"
        return data

    def read(self, research_id, *, offset=0, hypothesis_id="", relation_offset=0, source_offset=0):
        if min(offset, relation_offset, source_offset) < 0:
            raise ValueError("分页位置无效")
        links = self._links(research_id)
        hypotheses = self._hypotheses(research_id)
        if hypothesis_id and not any(h.hypothesis_id == hypothesis_id for h in hypotheses):
            raise LookupError("假说不属于此课题或无权访问")
        rows = [h for h in hypotheses if not hypothesis_id or h.hypothesis_id == hypothesis_id]
        selected = rows[offset:offset + 10]
        sources = {}
        def resolve(kind, oid):
            key = kind + ":" + oid
            if key not in sources:
                if len(sources) >= 1000:
                    raise ValueError("来源超过地图上限，请选择单个假说后查看")
                sources[key] = self.source(kind, oid)
            return sources[key]
        output = []
        for h in selected:
            relations = []
            overrides = {(l.object_type, l.object_id): l for l in links if l.subject_type == "hypothesis" and l.subject_id == h.hypothesis_id}
            references = [("evidence", str(ref), relation) for refs, relation in ((h.evidence_refs, "supports"), (h.counter_evidence_refs, "contradicts")) for ref in refs]
            references += [(l.object_type, l.object_id, l.relation) for l in overrides.values()]
            total_relations = len({(kind, oid) for kind, oid, _ in references if not (overrides.get((kind, oid)) and overrides[(kind, oid)].state in {"removed", "retracted"})})
            seen = set()
            skipped = 0
            for kind, oid, relation in references:
                if (kind, oid) in seen:
                    continue
                seen.add((kind, oid))
                link = overrides.get((kind, oid))
                if link and link.state in {"removed", "retracted"}:
                    continue
                if skipped < relation_offset:
                    skipped += 1
                    continue
                if len(relations) >= 50:
                    break
                source = resolve(kind, oid)
                metadata = link.metadata if link else {}
                ambiguous = not link and oid in h.evidence_refs and oid in h.counter_evidence_refs
                relation = link.relation if link else "references" if ambiguous else relation
                reviewed = bool(link and link.discovered_by == "user" and metadata.get("review_status") == "confirmed" and metadata.get("hypothesis_revision") == h.revision and metadata.get("reviewed_source_hash") == source["content_hash"] and source["status"] == "active")
                relations.append({"id": link.link_id if link else f"legacy:{h.hypothesis_id}:{kind}:{oid}", "object_type": kind, "object_id": oid, "relation": relation if relation in RELATIONS else "references", "title": source["title"], "source_status": source["status"], "review_status": "confirmed" if reviewed else "pending", "reason": str(metadata.get("reason") or ("旧记录同时把这条来源列为支持和反对，需要核对。" if ambiguous else "旧记录仅保存了引用关系，尚未核对判断理由。")), "scope": str(metadata.get("scope") or "适用条件尚未记录"), "revision": link.revision if link else 0, "legacy": link is None})
            output.append({"hypothesis_id": h.hypothesis_id, "title": h.statement, "status": h.status, "revision": h.revision, "rationale": h.rationale, "test_plan": h.test_plan, "falsification_condition": h.falsification_condition, "relations": relations[:200], "relation_total": total_relations, "relation_offset": relation_offset, "relation_next_offset": relation_offset + 50 if total_relations > relation_offset + 50 else None})
        available = {(l.object_type, l.object_id) for l in links if l.state not in {"removed", "retracted"} and l.object_type in {"evidence", "paper", "passage", "wiki_page", "wiki_claim"}}
        available.update(("evidence", str(ref)) for h in hypotheses for ref in h.evidence_refs + h.counter_evidence_refs)
        choices = sorted(available)[source_offset:source_offset+50]
        for kind, oid in choices:
            resolve(kind, oid)
        return {"schema": "tcode.hypothesis-map.v1", "research_id": research_id, "hypotheses": output, "sources": [{k: v for k, v in s.items() if k not in {"content", "content_hash", "locator"}} for key, s in sources.items() if key in {kind+":"+oid for kind,oid in choices}], "total": len(rows), "next_offset": offset + 10 if len(rows) > offset + 10 else None, "source_total": len(available), "source_offset": source_offset, "source_next_offset": source_offset+50 if len(available)>source_offset+50 else None, "notice": "关系表示已记录的判断；用户核对不等于假说已被证明。证据变化或假说修订后需要重新核对。"}

    def source_choices(self, research_id, *, offset=0, limit=50):
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("分页位置无效")
        links = self._links(research_id)
        hypotheses = self._hypotheses(research_id)
        available = {(l.object_type, l.object_id) for l in links if l.state not in {"removed", "retracted"} and l.object_type in {"evidence", "paper", "passage", "wiki_page", "wiki_claim"}}
        available.update(("evidence", str(ref)) for h in hypotheses for ref in h.evidence_refs + h.counter_evidence_refs)
        ordered = sorted(available)
        items = [self.source(kind, oid) for kind, oid in ordered[offset:offset+limit]]
        for item in items:
            body = str(item["content"])
            item["content_truncated"] = len(body) > 1000
            item["content"] = body[:1000]
            if item["content_truncated"]:
                item["content_label"] += "（仅内容摘录，完整核对请单独读取来源）"
        return {"items": items, "total": len(ordered), "next_offset": offset+limit if offset+limit < len(ordered) else None,
                "说明": "来源名称和内部引用供关联工具使用；向用户展示名称。来源记录不是已验证结论。"}

    def read_source(self, research_id, kind, object_id):
        links = self._links(research_id)
        hypotheses = self._hypotheses(research_id)
        allowed = any(l.object_type == kind and l.object_id == object_id and l.state not in {"removed", "retracted"} for l in links)
        allowed = allowed or (kind == "evidence" and any(object_id in h.evidence_refs + h.counter_evidence_refs for h in hypotheses))
        if not allowed:
            raise LookupError("来源不属于当前课题或未载入此页")
        return self.source(kind, object_id)

    def review(self, research_id, hypothesis_id, *, object_type, object_id, relation, reason, scope, hypothesis_revision, expected_source_hash):
        if self.manager._project(research_id).status == "archived":
            raise ValueError("课题已归档，请先恢复后再修改关系")
        graph = self.read(research_id, hypothesis_id=hypothesis_id)
        h = graph["hypotheses"][0]
        if h["revision"] != hypothesis_revision:
            raise ValueError("假说已更新，请刷新后重新核对")
        if relation not in RELATIONS or not reason.strip() or len(reason) > 4000 or len(scope) > 4000:
            raise ValueError("请选择关系并填写判断理由")
        source = self.read_source(research_id, object_type, object_id)
        if source["content_hash"] != expected_source_hash:
            raise ValueError("来源已更新，请重新读取原文后核对")
        if not str(source["content"]).strip():
            raise ValueError("没有可核对的来源正文，请补齐资料")
        if source["status"] != "active":
            raise ValueError("来源缺失或已失效，无法确认关系")
        return self.manager.create_manual_link(research_id, subject_type="hypothesis", subject_id=hypothesis_id, object_type=object_type, object_id=object_id, relation=relation, metadata={"reason": reason.strip(), "scope": scope.strip(), "review_status": "confirmed", "hypothesis_revision": hypothesis_revision, "reviewed_source_hash": source["content_hash"], "source_title": source["title"]}).to_dict()


def hypothesis_map_summary(graph):
    return {"假说": [{"名称": h["title"], "状态": STATUSES.get(h["status"], "状态未说明"), "版本": h["revision"], "依据": [{"名称": r["title"], "关系": RELATIONS[r["relation"]], "核对状态": "用户已核对" if r["review_status"] == "confirmed" else "待核对", "来源": "可读取" if r["source_status"] == "active" else "缺失或失效", "理由": r["reason"], "适用条件": r["scope"]} for r in h["relations"]], "待验证": h["test_plan"], "证伪条件": h["falsification_condition"], "依据总数": h["relation_total"]} for h in graph["hypotheses"]], "还有下一页": graph["next_offset"] is not None, "说明": graph["notice"], "展示要求": "只用名称和中文状态；平台已经附上可点击摘要。不要将待核对引用说成已验证事实。"}


class ImportedHypothesisMap(HypothesisMap):
    """Read immutable package snapshots without adopting their scientific judgements."""
    def _links(self, asset_id):
        from .pack_asset_store import ResearchPackAssetStore
        from types import SimpleNamespace
        asset = ResearchPackAssetStore.get_asset(asset_id, **self.owner, workdir=self.workdir)
        if asset is None or asset.status == "invalid":
            raise LookupError("研究包不存在或无权访问")
        self.asset_id = asset_id
        rows = ResearchPackAssetStore.list_objects(asset_id, **self.owner, workdir=self.workdir, object_type="knowledge_link", limit=10000)
        return [SimpleNamespace(**{**r.payload, "discovered_by": "import"}) for r in rows]

    def _hypotheses(self, asset_id):
        from .pack_asset_store import ResearchPackAssetStore
        from types import SimpleNamespace
        rows = ResearchPackAssetStore.list_objects(asset_id, **self.owner, workdir=self.workdir, object_type="hypothesis", limit=10000)
        return [SimpleNamespace(**r.payload) for r in rows]

    def source(self, kind, object_id):
        from .pack_asset_store import ResearchPackAssetStore
        row = ResearchPackAssetStore.get_object(self.asset_id, object_id, **self.owner, workdir=self.workdir, object_type=kind)
        payload = row.payload if row else {}
        content = str(payload.get("content") or payload.get("text") or payload.get("statement") or payload.get("summary_text") or "")
        return {"object_type": kind, "object_id": object_id, "title": payload.get("title") or payload.get("statement") or "来源（研究包未携带）",
                "content": content[:20000], "content_hash": row.content_hash if row else "", "locator": payload.get("locator") or "",
                "status": "active" if row and content else "missing", "source_type": payload.get("source_type", ""), "source_label": "研究包来源记录 · " + {"paper":"文献提取，需核对原文", "model_inference":"模型推断", "user":"用户记录", "user_confirmed":"用户记录", "computation":"计算结果"}.get(str(payload.get("source_type") or ""), "来源类型未说明"), "content_label": "来源方保存的记录；请重新核对原始文献"}

    def read(self, asset_id, **kwargs):
        graph = super().read(asset_id, **kwargs)
        graph["readonly"] = True
        graph["notice"] = "研究包交接视图只读。来源方的关联、理由和适用条件已保留；核对状态需要接收方重新判断，不自动成为本地结论。标准研究包不携带原始 PDF，原文需另行提供。"
        return graph
