from __future__ import annotations

import json
import uuid
from dataclasses import replace
from typing import Any

from tcode_agent.agent import Agent
from tcode_agent.agent.agents import BuiltinAgents
from tcode_agent.providers import create_provider

from .store import PaperWikiStore, WikiValidationError


REGENERATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "operations"],
    "properties": {
        "summary": {"type": "string"},
        "operations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "page_id", "page_json"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["upsert_page", "archive_page", "restore_page"],
                    },
                    "page_id": {"type": "string"},
                    "page_json": {"type": "string"},
                },
            },
        },
    },
}


def _compact_page(page: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(page or {})
    return {
        "page_id": source.get("page_id"),
        "canonical_key": source.get("canonical_key"),
        "page_type": source.get("page_type"),
        "title": source.get("title"),
        "summary": str(source.get("summary") or "")[:3000],
        "content": str(source.get("content") or "")[:6000],
        "status": source.get("status"),
        "aliases": list(source.get("aliases") or []),
        "source_doc_ids": list(source.get("source_doc_ids") or []),
        "evidence_ids": list(source.get("evidence_ids") or []),
        "claims": [
            {
                "claim_id": claim.get("claim_id"),
                "statement": str(claim.get("statement") or "")[:1500],
                "scope": dict(claim.get("scope") or {}),
                "evidence_ids": list(claim.get("evidence_ids") or []),
                "support_status": claim.get("support_status"),
                "confidence": claim.get("confidence"),
                "origin": claim.get("origin"),
            }
            for claim in list(source.get("claims") or [])[:100]
            if isinstance(claim, dict)
        ],
    }


def _proposal_context(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    context: list[dict[str, Any]] = []
    for operation in proposal.get("operations") or []:
        op_type = str(operation.get("type") or "")
        context.append(
            {
                "type": op_type,
                "page_id": operation.get("page_id"),
                "before": _compact_page(operation.get("before")) if op_type == "upsert_page" else None,
                "proposed": _compact_page(operation.get("page")) if op_type == "upsert_page" else None,
            }
        )
    return context


def _allowed_evidence_ids(proposal: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for operation in proposal.get("operations") or []:
        for page in (operation.get("before"), operation.get("page")):
            if not isinstance(page, dict):
                continue
            for evidence_id in page.get("evidence_ids") or []:
                text = str(evidence_id or "").strip()
                if text and text not in result:
                    result.append(text)
            for claim in page.get("claims") or []:
                if not isinstance(claim, dict):
                    continue
                for evidence_id in claim.get("evidence_ids") or []:
                    text = str(evidence_id or "").strip()
                    if text and text not in result:
                        result.append(text)
    return result


class PaperWikiLLMRegenerator:
    VERSION = "paper-wiki-llm-regenerator-1"

    def __init__(self, *, store: PaperWikiStore):
        self.store = store

    async def regenerate(
        self,
        proposal_id: str,
        *,
        instructions: str,
        created_by: str,
    ) -> dict[str, Any]:
        proposal = self.store.get_proposal(proposal_id)
        if str(proposal.get("status") or "") != "pending_review":
            raise WikiValidationError(f"proposal is not pending: {proposal.get('status')}")
        allowed_evidence = _allowed_evidence_ids(proposal)
        evidence_records = self.store.evidence_records(allowed_evidence, limit=50)
        evidence_context = [
            {
                "evidence_id": record.get("evidence_id"),
                "title": record.get("title"),
                "source_id": record.get("source_id"),
                "locator": record.get("locator"),
                "content": str(record.get("content") or "")[:2500],
            }
            for record in evidence_records
        ]
        prompt = (
            "你正在重新生成一个 Paper Wiki Proposal。只基于给出的当前页面、原 Proposal、"
            "审核意见和 Evidence；不得创造新的论文、Evidence ID、数值或适用条件。\n\n"
            "输出 JSON Schema 要求的 operations。upsert_page 的 page_json 必须是完整 JSON 对象字符串，"
            "至少包含 page_type、title、summary、content、source_doc_ids、evidence_ids、claims；"
            "每个 Claim 包含 statement、scope、evidence_ids、support_status、confidence、origin。"
            "无法由 Evidence 支持的内容应删除或标记 manual_unverified。不要批准或应用 Proposal。\n\n"
            f"审核意见：{str(instructions or '修复证据不足、过度概括和条件缺失问题。')[:6000]}\n\n"
            f"允许使用的 Evidence ID：{json.dumps(allowed_evidence, ensure_ascii=False)}\n\n"
            "原 Proposal 上下文：\n"
            + json.dumps(_proposal_context(proposal), ensure_ascii=False)[:40_000]
            + "\n\nEvidence 内容：\n"
            + json.dumps(evidence_context, ensure_ascii=False)[:30_000]
        )

        agent_info = BuiltinAgents.get("paper_synthesis") or BuiltinAgents.get("critic")
        if agent_info is None:
            raise RuntimeError("paper_synthesis agent is unavailable")
        agent_info = replace(
            agent_info,
            name="paper_wiki_regenerator",
            permission={"*": "deny"},
            steps=1,
            prompt=(
                "你是 Paper Wiki 的受限重新生成器。你只能返回结构化 JSON，不能调用工具，"
                "不能批准或应用 Wiki 修改。论文原文 Evidence 是最终可信来源。"
            ),
        )
        provider = create_provider(workdir=self.store.workdir.as_posix())
        agent = Agent(
            agent_info=agent_info,
            provider=provider,
            session_id=f"wiki-regenerate-{uuid.uuid4().hex[:12]}",
            workdir=self.store.workdir.as_posix(),
        )
        try:
            message = await agent.run(
                prompt,
                output_format={"type": "json_schema", "schema": REGENERATION_SCHEMA},
            )
            payload = json.loads(str(message.content or "{}"))
        finally:
            try:
                await agent.close()
            except Exception:
                pass

        operations: list[dict[str, Any]] = []
        allowed_set = set(allowed_evidence)
        for item in payload.get("operations") or []:
            op_type = str(item.get("type") or "")
            if op_type == "upsert_page":
                page = json.loads(str(item.get("page_json") or "{}"))
                if not isinstance(page, dict):
                    raise WikiValidationError("LLM regeneration page_json must decode to an object")
                outer_page_id = str(item.get("page_id") or "").strip()
                if outer_page_id and not str(page.get("page_id") or "").strip():
                    page["page_id"] = outer_page_id
                used_evidence = {
                    str(evidence_id)
                    for claim in page.get("claims") or []
                    if isinstance(claim, dict)
                    for evidence_id in claim.get("evidence_ids") or []
                } | {
                    str(link.get("evidence_id") or "")
                    for claim in page.get("claims") or []
                    if isinstance(claim, dict)
                    for link in claim.get("evidence_links") or []
                    if isinstance(link, dict)
                } | {str(item) for item in page.get("evidence_ids") or []}
                unknown = {item for item in used_evidence if item and item not in allowed_set}
                if unknown:
                    raise WikiValidationError(
                        f"LLM regeneration introduced unknown evidence IDs: {sorted(unknown)}"
                    )
                operations.append({"type": "upsert_page", "page": page})
            elif op_type in {"archive_page", "restore_page"}:
                operations.append({"type": op_type, "page_id": str(item.get("page_id") or "")})
        replacement = self.store.stage_proposal(
            operations=operations,
            trigger_kind="llm_regeneration",
            trigger_doc_ids=list(proposal.get("trigger_doc_ids") or []),
            created_by=created_by,
            compiler_version=self.VERSION,
            require_evidence=True,
            parent_proposal_id=proposal_id,
        )
        if str(replacement.get("status") or "") != "pending_review":
            raise WikiValidationError(
                "LLM regenerated Proposal did not pass structural validation: "
                + json.dumps(replacement.get("validation") or {}, ensure_ascii=False)
            )
        self.store.supersede_proposal(
            proposal_id,
            replacement_proposal_id=str(replacement["proposal_id"]),
            actor=created_by,
            reason=str(instructions or "LLM regeneration requested"),
        )
        replacement["regeneration_summary"] = str(payload.get("summary") or "")
        return replacement
