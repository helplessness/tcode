from __future__ import annotations

import hashlib
import re
from typing import Any

from .text_safety_bridge import (
    find_valid_mutations,
    is_valid_mutation,
    strip_pdf_control_codes,
)

from .store import PaperWikiStore, _slug


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;。！？])\s+")
SENTENCE_ABBREVIATION_RE = re.compile(
    r"\b(?:fig|figs|eq|eqs|ref|refs|dr|mr|mrs|ms|vs|al)\.",
    re.IGNORECASE,
)
RESULT_HINTS = (
    "activity",
    "affinity",
    "best mutant",
    "catalytic",
    "compared",
    "conversion",
    "decreased",
    "enhanced",
    "impaired",
    "increased",
    "inactive",
    "kcat",
    "kinetic",
    "km",
    "reached",
    "reduced",
    "showed",
    "titer",
    "yield",
    "活性",
    "产量",
    "提高",
    "降低",
)


class PaperWikiCompiler:
    """从文献库记录构建可审查、可追溯的 Wiki 提案。

    编译器严格受证据约束。Agent 可以通过同一提案接口暂存更丰富的生成
    操作，而文献入库流程始终保留确定、可审计的降级路径。
    """

    VERSION = "paper-wiki-compiler-5"

    def __init__(self, *, wiki_store: PaperWikiStore, library_store: Any):
        self.wiki_store = wiki_store
        self.library_store = library_store

    def _summary(self, record: dict[str, Any]) -> str:
        l0 = dict(record.get("l0_summary") or {})
        return str(
            l0.get("summary_text")
            or l0.get("abstract_like_summary")
            or l0.get("summary")
            or ""
        ).strip()

    def _fact_claim(self, fact: dict[str, Any]) -> dict[str, Any] | None:
        """把单条事实转换为 Claim，并拒绝列语义未经确认的表格指标。"""

        fact_type = str(fact.get("fact_type") or "").strip()
        scope = dict(fact.get("attributes") or {})
        if (
            fact_type in {"table_measurement", "kinetic_metric", "assay_result"}
            and str(scope.get("table_id") or "").strip()
            and str(scope.get("table_alignment_status") or "").strip().lower()
            != "aligned"
        ):
            return None
        label = strip_pdf_control_codes(
            fact.get("label") or fact_type or ""
        ).strip()
        value = strip_pdf_control_codes(fact.get("value") or "").strip()
        statement = f"{label}: {value}" if value and value not in label else (value or label)
        if not statement:
            return None
        evidence_id = str(fact.get("evidence_id") or "").strip()
        scope["fact_type"] = fact_type
        scope["fact_label"] = label
        scope["anchor_ids"] = list(fact.get("anchor_ids") or [])
        source_fact_id = str(fact.get("fact_id") or "").strip()
        if source_fact_id:
            scope["source_fact_id"] = source_fact_id
        return {
            "claim_id": source_fact_id,
            "statement": statement,
            "scope": scope,
            "evidence_ids": [evidence_id] if evidence_id else [],
            "evidence_links": (
                [
                    {
                        "evidence_id": evidence_id,
                        "relation": "supports",
                        "support_strength": 0.8,
                    }
                ]
                if evidence_id
                else []
            ),
            "support_status": "supported" if evidence_id else "manual_unverified",
            "confidence": 0.8 if evidence_id else 0.45,
            "origin": "paper_library",
        }

    @staticmethod
    def _is_entity_only_fact(fact: dict[str, Any]) -> bool:
        """判断事实是否只表达实体身份而没有独立结论。"""

        fact_type = str(fact.get("fact_type") or "").strip().casefold()
        label = str(fact.get("label") or "").strip().casefold()
        value = str(fact.get("value") or "").strip()
        return fact_type in {"mutation", "gene_alias"} or (
            label in {"mutation", "gene"} and is_valid_mutation(value)
        )

    @staticmethod
    def _is_table_prose_fact(fact: dict[str, Any]) -> bool:
        """拒绝由表格 Markdown 错误分句形成的伪正文事实。"""

        if str(fact.get("fact_type") or "").strip().casefold() != "mutation_context":
            return False
        attributes = dict(fact.get("attributes") or {})
        if str(attributes.get("source") or "").strip().casefold() == "table":
            return True
        return any(
            ":table:" in str(anchor_id or "").strip().casefold()
            for anchor_id in fact.get("anchor_ids") or []
        )

    @staticmethod
    def _mentions_mutation(text: str, mutation: str) -> bool:
        return bool(
            re.search(
                rf"(?<![A-Za-z0-9]){re.escape(mutation)}(?![A-Za-z0-9])",
                str(text or ""),
                re.IGNORECASE,
            )
        )

    def _fact_relates_to_mutation(self, fact: dict[str, Any], mutation: str) -> bool:
        attributes = dict(fact.get("attributes") or {})
        related = [
            str(item).strip()
            for key in ("mutation", "mutations", "related_mutations")
            for item in (
                attributes.get(key)
                if isinstance(attributes.get(key), list)
                else [attributes.get(key)]
            )
            if str(item or "").strip()
        ]
        if any(item.casefold() == mutation.casefold() for item in related):
            return True
        return self._mentions_mutation(
            " ".join([str(fact.get("label") or ""), str(fact.get("value") or "")]),
            mutation,
        )

    @staticmethod
    def _context_score(statement: str) -> int:
        lowered = str(statement or "").casefold()
        score = sum(2 for hint in RESULT_HINTS if hint in lowered)
        if re.search(r"\d+(?:\.\d+)?\s*(?:%|mg/l|mm|μm|um|fold)", lowered):
            score += 2
        if any(token in lowered for token in ("constructed", "purification", "method")):
            score -= 1
        return score

    @staticmethod
    def _sentence_units(text: str) -> list[str]:
        raw = re.sub(r"\s+", " ", strip_pdf_control_codes(text)).strip()
        if not raw:
            return []
        protected = SENTENCE_ABBREVIATION_RE.sub(
            lambda match: match.group(0).replace(".", "__TCODE_DOT__"),
            raw,
        )
        return [
            part.replace("__TCODE_DOT__", ".").strip()
            for part in SENTENCE_SPLIT_RE.split(protected)
            if part.strip()
        ]

    @staticmethod
    def _is_table_chunk(chunk: dict[str, Any]) -> bool:
        """阻止表格 Markdown（包括旧版分块）进入正文 Claim。"""

        kind = str(chunk.get("kind") or "").strip().casefold()
        if kind in {"table", "table_chunk"}:
            return True
        if str(chunk.get("table_id") or "").strip():
            return True
        chunk_id = str(chunk.get("chunk_id") or "").strip().casefold()
        return ":table:" in chunk_id

    def _mutation_context_claims(
        self,
        *,
        mutation: str,
        chunks: list[dict[str, Any]],
        canonical_key: str,
        doc_id: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        candidates: list[tuple[int, int, str, str, str]] = []
        seen: set[str] = set()
        order = 0
        for chunk in chunks:
            # 表格分块含 Markdown 分隔符，宽表格框还可能带入邻近正文。
            # 结构化表格事实仍走常规 Claim 路径，原始 Markdown 不得变成正文 Claim。
            if self._is_table_chunk(chunk):
                continue
            chunk_id = str(chunk.get("chunk_id") or "").strip()
            if not chunk_id:
                continue
            for sentence in self._sentence_units(str(chunk.get("text") or "")):
                if not self._mentions_mutation(sentence, mutation) or len(sentence) < 24:
                    continue
                normalized = sentence.casefold()
                if normalized in seen:
                    continue
                seen.add(normalized)
                # 只有 EvidenceStore 写入成功后才分配证据 ID；不能从 chunk_id
                # 猜测 ID，否则不存在的证据也可能让 Claim 显示为已支持。
                evidence_id = str(chunk.get("evidence_id") or "").strip()
                candidates.append(
                    (
                        self._context_score(sentence),
                        order,
                        sentence[:1200],
                        chunk_id,
                        evidence_id,
                    )
                )
                order += 1

        claims: list[dict[str, Any]] = []
        for _score, _order, statement, chunk_id, evidence_id in sorted(
            candidates,
            key=lambda item: (-item[0], item[1]),
        )[: max(1, int(limit))]:
            identity = hashlib.sha256(f"{chunk_id}:{statement}".encode("utf-8")).hexdigest()[:24]
            claims.append(
                self._claim_for_page(
                    {
                        "claim_id": f"context-{identity}",
                        "statement": statement,
                        "scope": {
                            "source": "paper_chunk",
                            "mutation": mutation,
                            "source_doc_id": doc_id,
                            "source_chunk_id": chunk_id,
                        },
                        "evidence_ids": [evidence_id] if evidence_id else [],
                        "evidence_links": (
                            [
                                {
                                    "evidence_id": evidence_id,
                                    "relation": "supports",
                                    "support_strength": 0.78,
                                }
                            ]
                            if evidence_id
                            else []
                        ),
                        "support_status": "supported" if evidence_id else "manual_unverified",
                        "confidence": 0.78 if evidence_id else 0.45,
                        "origin": "paper_library",
                    },
                    canonical_key,
                )
            )
        return claims

    def _ensure_operation_evidence(
        self,
        *,
        doc_id: str,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """校验暂存 Claim 的来源，并按需创建 Evidence 记录。"""

        ensure = getattr(self.library_store, "ensure_evidence_for_references", None)
        if not callable(ensure):
            # 兼容替代 LibraryStore；生产 PaperLibraryStore 始终提供受控方法。
            return {
                "enabled": False,
                "skipped": True,
                "reason": "library_store_does_not_support_on_demand_evidence",
            }

        fact_ids: list[str] = []
        chunk_ids: list[str] = []
        for operation in operations:
            page = dict(operation.get("page") or {})
            for claim in list(page.get("claims") or []):
                if not isinstance(claim, dict):
                    continue
                scope = dict(claim.get("scope") or {})
                fact_id = str(scope.get("source_fact_id") or "").strip()
                chunk_id = str(scope.get("source_chunk_id") or "").strip()
                if fact_id and fact_id not in fact_ids:
                    fact_ids.append(fact_id)
                if chunk_id and chunk_id not in chunk_ids:
                    chunk_ids.append(chunk_id)

        try:
            result = dict(
                ensure(
                    doc_id,
                    fact_ids=fact_ids,
                    chunk_ids=chunk_ids,
                )
                or {}
            )
        except Exception as exc:
            result = {
                "enabled": False,
                "skipped": False,
                "error": f"{type(exc).__name__}: {exc}",
                "fact_evidence_ids": {},
                "chunk_evidence_ids": {},
            }

        fact_evidence_ids = {
            str(key): str(value)
            for key, value in dict(result.get("fact_evidence_ids") or {}).items()
            if str(key).strip() and str(value).strip()
        }
        chunk_evidence_ids = {
            str(key): str(value)
            for key, value in dict(result.get("chunk_evidence_ids") or {}).items()
            if str(key).strip() and str(value).strip()
        }
        for operation in operations:
            page = operation.get("page")
            if not isinstance(page, dict):
                continue
            claims = list(page.get("claims") or [])
            for claim in claims:
                if not isinstance(claim, dict):
                    continue
                scope = dict(claim.get("scope") or {})
                fact_id = str(scope.get("source_fact_id") or "").strip()
                chunk_id = str(scope.get("source_chunk_id") or "").strip()
                if not fact_id and not chunk_id:
                    continue
                evidence_id = (
                    fact_evidence_ids.get(fact_id)
                    if fact_id
                    else chunk_evidence_ids.get(chunk_id)
                )
                relation = str(scope.get("evidence_relation") or "supports")
                if evidence_id:
                    support_strength = 0.8 if fact_id else 0.78
                    if relation == "context_only":
                        support_strength = 0.6
                    claim["evidence_ids"] = [evidence_id]
                    claim["evidence_links"] = [
                        {
                            "evidence_id": evidence_id,
                            "relation": relation,
                            "support_strength": support_strength,
                        }
                    ]
                    claim["support_status"] = (
                        "partially_supported" if relation == "context_only" else "supported"
                    )
                    claim["confidence"] = support_strength
                else:
                    # 无法验证的来源不能留下看似已受支持的悬空证据 ID。
                    claim["evidence_ids"] = []
                    claim["evidence_links"] = []
                    claim["support_status"] = "manual_unverified"
                    claim["confidence"] = min(float(claim.get("confidence") or 0.45), 0.45)
            page["evidence_ids"] = list(
                dict.fromkeys(
                    evidence_id
                    for claim in claims
                    if isinstance(claim, dict)
                    for evidence_id in list(claim.get("evidence_ids") or [])
                    if str(evidence_id or "").strip()
                )
            )
        return result

    @staticmethod
    def _mutation_summary(mutation: str, claims: list[dict[str, Any]]) -> str:
        if not claims:
            return f"论文提及变异 {mutation}，但尚未提取到可独立归因的性能结果。"
        statement = str(claims[0].get("statement") or "").strip()
        if len(statement) > 260:
            statement = statement[:257].rstrip() + "..."
        return f"{mutation}：{statement}"

    def _claim_for_page(self, claim: dict[str, Any], canonical_key: str) -> dict[str, Any]:
        copied = dict(claim)
        copied["scope"] = dict(claim.get("scope") or {})
        copied["evidence_ids"] = list(claim.get("evidence_ids") or [])
        copied["evidence_links"] = [dict(item) for item in claim.get("evidence_links") or []]
        identity = str(
            copied["scope"].get("source_fact_id")
            or copied.get("claim_id")
            or copied.get("statement")
            or "claim"
        )
        copied["claim_id"] = (
            "claim-"
            + hashlib.sha256(f"{canonical_key}:{identity}".encode("utf-8")).hexdigest()[:24]
        )
        return copied

    def compile_document(
        self,
        doc_id: str,
        *,
        created_by: str = "paper_library_manager",
        max_pages: int = 12,
        auto_approve_new_pages: bool = False,
    ) -> dict[str, Any]:
        """为单篇论文编译受证据约束的 Paper 与 Mutation 页面提案。"""

        record_result = self.library_store.get_record(doc_id, sync_evidence=True)
        record = dict(record_result.get("record") or record_result)
        metadata = dict(record.get("metadata") or {})
        title = str(metadata.get("title") or doc_id).strip()
        summary = self._summary(record)
        facts = [dict(item) for item in record.get("l2_facts") or [] if isinstance(item, dict)]
        fact_claims = [
            (fact, claim)
            for fact in facts
            if not self._is_table_prose_fact(fact)
            if (claim := self._fact_claim(fact)) is not None
        ]
        base_claims = [claim for fact, claim in fact_claims if not self._is_entity_only_fact(fact)][:80]
        chunks = [dict(item) for item in record.get("l2_chunks") or [] if isinstance(item, dict)]
        paper_key = f"paper:{doc_id}"
        claims = [self._claim_for_page(claim, paper_key) for claim in base_claims]
        evidence_ids = [
            evidence_id
            for claim in claims
            for evidence_id in claim.get("evidence_ids", [])
            if evidence_id
        ]
        paper_page = {
            "page_type": "paper",
            "canonical_key": paper_key,
            "slug": _slug(title, doc_id),
            "title": title,
            "summary": summary,
            "status": "canonical",
            "source_doc_ids": [doc_id],
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "claims": claims,
            "origin": "compiler",
            "_refresh_generated_content": True,
        }
        operations: list[dict[str, Any]] = [{"type": "upsert_page", "page": paper_page}]

        mutations: list[str] = []
        for fact in facts:
            for match in find_valid_mutations(
                " ".join([str(fact.get("label") or ""), str(fact.get("value") or "")])
            ):
                if match not in mutations:
                    mutations.append(match)
        for mutation in mutations[:24]:
            matching_fact_claims = [
                (fact, claim)
                for fact, claim in fact_claims
                if not self._is_entity_only_fact(fact)
                and self._fact_relates_to_mutation(fact, mutation)
            ]
            mutation_key = f"mutation:{mutation.casefold()}"
            mutation_claims = self._mutation_context_claims(
                mutation=mutation,
                chunks=chunks,
                canonical_key=mutation_key,
                doc_id=doc_id,
            )
            seen_statements = {
                str(claim.get("statement") or "").strip().casefold() for claim in mutation_claims
            }
            fallback_fact_claims = (
                [
                    (fact, claim)
                    for fact, claim in matching_fact_claims
                    if str(fact.get("fact_type") or "")
                    in {"assay_result", "kinetic_metric", "table_measurement", "table_row"}
                ]
                if mutation_claims
                else matching_fact_claims
            )
            for _fact, claim in fallback_fact_claims:
                statement = str(claim.get("statement") or "").strip()
                if not statement or statement.casefold() in seen_statements:
                    continue
                mutation_claims.append(self._claim_for_page(claim, mutation_key))
                seen_statements.add(statement.casefold())
                if len(mutation_claims) >= 12:
                    break
            if not mutation_claims:
                identity_fact = next(
                    (fact for fact in facts if self._fact_relates_to_mutation(fact, mutation)),
                    {},
                )
                evidence_id = str(identity_fact.get("evidence_id") or "").strip()
                source_fact_id = str(identity_fact.get("fact_id") or "").strip()
                mutation_claims = [
                    self._claim_for_page(
                        {
                            "claim_id": f"mention-{hashlib.sha256(f'{doc_id}:{mutation}'.encode('utf-8')).hexdigest()[:24]}",
                            "statement": (
                                f"论文提及变异 {mutation}，但当前解析未提取到可独立归因的性能结果。"
                            ),
                            "scope": {
                                "mutation": mutation,
                                "source_doc_id": doc_id,
                                **(
                                    {
                                        "source_fact_id": source_fact_id,
                                        "evidence_relation": "context_only",
                                    }
                                    if source_fact_id
                                    else {}
                                ),
                            },
                            "evidence_ids": [evidence_id] if evidence_id else [],
                            "evidence_links": (
                                [{"evidence_id": evidence_id, "relation": "context_only", "support_strength": 0.6}]
                                if evidence_id
                                else []
                            ),
                            "support_status": "partially_supported" if evidence_id else "manual_unverified",
                            "confidence": 0.6 if evidence_id else 0.45,
                            "origin": "compiler",
                        },
                        mutation_key,
                    )
                ]
            operations.append(
                {
                    "type": "upsert_page",
                    "page": {
                        "page_type": "mutation",
                        "canonical_key": mutation_key,
                        "slug": mutation.casefold(),
                        "title": mutation,
                        "summary": self._mutation_summary(mutation, mutation_claims),
                        "status": "canonical",
                        "source_doc_ids": [doc_id],
                        "_replace_compiler_claims_for_source_doc_ids": [doc_id],
                        "_refresh_generated_content": True,
                        "evidence_ids": list(
                            dict.fromkeys(
                                evidence_id
                                for claim in mutation_claims
                                for evidence_id in claim.get("evidence_ids", [])
                            )
                        ),
                        "claims": mutation_claims,
                        "origin": "compiler",
                    },
                }
            )
        staged_operations = operations[: max(1, int(max_pages))]
        evidence_ensure = self._ensure_operation_evidence(
            doc_id=doc_id,
            operations=staged_operations,
        )
        proposal = self.wiki_store.stage_proposal(
            operations=staged_operations,
            trigger_kind="paper_ingest",
            trigger_doc_ids=[doc_id],
            created_by=created_by,
            compiler_version=self.VERSION,
            require_evidence=True,
        )
        proposal["evidence_ensure"] = evidence_ensure
        if auto_approve_new_pages and self._creates_only_new_pages(proposal):
            applied = self.wiki_store.approve_and_apply_proposal(
                str(proposal["proposal_id"]),
                actor=created_by,
                expected_hash=str(proposal["proposal_hash"]),
                comment="文献上传后自动批准首次创建的 Wiki 页面",
                require_new_pages_only=True,
            )
            applied["evidence_ensure"] = evidence_ensure
            return applied
        return proposal

    @staticmethod
    def _creates_only_new_pages(proposal: dict[str, Any]) -> bool:
        operations = list(proposal.get("operations") or [])
        return bool(
            str(proposal.get("status") or "") == "pending_review"
            and str(proposal.get("trigger_kind") or "") == "paper_ingest"
            and operations
            and all(
                str(operation.get("type") or "") == "upsert_page"
                and not str((operation.get("page") or {}).get("page_id") or "").strip()
                and not operation.get("before")
                for operation in operations
            )
        )
