"""Workspace-local P2/P3 run ledger and fact/prediction boundaries.

The published OWL bundle is immutable.  Everything proposed by an agent is
written below one run directory and is therefore reviewable without becoming
part of the ontology.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .entity_store import EntityStore
from .entity_validation import canonical_bytes


_SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,95}$")


def _technical_details_requested(request: str) -> bool:
    return bool(re.search(
        r"(?:技术|实现|后端|数据库|SQLite|MCP|RDKit|Workspace|工具调用|运行细节).{0,24}"
        r"(?:说明|解释|列出|详情|诊断|核实|为什么|为何|是否|怎么)|"
        r"(?:说明|解释|列出|诊断|核实).{0,24}(?:技术|实现|后端|数据库|SQLite|MCP|RDKit|Workspace|工具调用|运行细节)",
        request,
        re.I,
    ))


def _scientific_user_text(value: str, *, request: str = "") -> str:
    if _technical_details_requested(request):
        return value
    replacements = (
        (r"(?:当前)?仅(?:有)?五张(?:本地|冻结)?(?:知识)?卡(?:片)?", "当前本体证据覆盖有限"),
        (r"(?:离线、?)?无联网(?:/外部\s*MCP)?(?:/文档解析)?", "仅依据当前可核验证据"),
        (r"无(?:离线)?序列/结构候选库", "尚缺可用于扩展候选的序列与结构证据"),
        (r"无\s*RDKit/原子映射", "尚未验证具体原子对应与反应配平"),
        (r"私有\s*Workspace\s*本体不可用(?:（available=false）)?", "本次结论仅基于当前可核验的本体证据"),
        (r"Workspace\s*私有知识不可用(?:（available=false）)?", "本次结论仅基于当前可核验的本体证据"),
        (r"丙烯酸与\s*ThDP\s*中间体无实体", "当前本体证据未覆盖丙烯酸与 ThDP 中间体"),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value, flags=re.I)
    return value


def _validate_summary_roles(summary: str, candidates: list[dict[str, Any]], store: EntityStore) -> None:
    """A grouped Markdown answer cannot promote an upstream candidate's role."""
    names = []
    for candidate in candidates:
        entity_id = str(candidate.get("entity_id") or "")
        entity = store.entities.get(entity_id, {})
        aliases = [candidate["label"], entity.get("label", ""), *entity.get("aliases", [])]
        if entity_id:
            aliases.append(entity_id.rsplit(":", 1)[-1])
        names.append((candidate, [str(name).casefold() for name in aliases if len(str(name)) >= 3]))
    section_role = None
    candidate_column = None
    for line in summary.splitlines():
        if line.lstrip().startswith("#"):
            section_role = (
                "primary" if re.search(r"单酶|single[- ]enzyme", line, re.I)
                else "route_component" if re.search(r"多酶.*(?:组件|步骤)|(?:route|pathway) components", line, re.I)
                else None
            )
            candidate_column = None
            continue
        if not section_role or not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if candidate_column is None:
            candidate_column = next((index for index, cell in enumerate(cells)
                                     if re.search(r"候选|路线组件|candidate|enzyme", cell, re.I)), None)
            continue
        if candidate_column >= len(cells) or all(re.fullmatch(r"[:\s-]*", cell) for cell in cells):
            continue
        cell = cells[candidate_column].casefold()
        matches = [(candidate, max(len(name) for name in aliases if name in cell))
                   for candidate, aliases in names if any(name in cell for name in aliases)]
        if matches:
            candidate = max(matches, key=lambda item: item[1])[0]
            if candidate["candidate_role"] != section_role:
                raise ValueError(
                    f"user_summary role mismatch: {candidate['candidate_id']} is "
                    f"{candidate['candidate_role']}, not {section_role}; reorganize the summary "
                    "using entity_expanded_candidates and group_rank without changing upstream roles"
                )
_FACT_LEVELS = {"reported_exact", "reported_related"}
_HYPOTHESIS_LEVELS = {
    "mechanistic_analogy",
    "computed_prediction",
    "model_background_unverified",
}
_CANDIDATE_KINDS = {
    "entity_protein",
    "entity_protein_group",
    "entity_family",
    "model_candidate",
    "unmatched_capability",
}
_ENTITY_KIND_TYPES = {
    "entity_protein": "Protein",
    "entity_protein_group": "ProteinGroup",
    "entity_family": "EnzymeFamily",
}
_ARTIFACTS = (
    "01_reaction_analysis.json",
    "02_mechanism_hypotheses.json",
    "02_mechanisms.json",
    "03_capabilities.json",
    "baseline_candidate_draft.json",
    "03_candidate_and_routes.json",
    "04_candidates.json",
    "05_routes.json",
)

_PRODUCTION_PROTOCOL = "mechanism-guided-enzyme-search/3.10"


def private_references(value):
    result = set()
    if isinstance(value, Mapping):
        for child in value.values():
            result.update(private_references(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            result.update(private_references(child))
    elif isinstance(value, str) and value.startswith(('private-assertion:', 'private-evidence:')) and not any(c.isspace() for c in value):
        result.add(value.replace('private-evidence:', 'private-assertion:', 1))
    return result


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _nonempty_text(value: Any, label: str, *, maximum: int = 4000) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    if len(text) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    return text


def _text_list(
    value: Any,
    label: str,
    *,
    maximum_items: int,
    maximum_length: int = 1200,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    if len(value) > maximum_items:
        raise ValueError(f"{label} exceeds {maximum_items} items")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(
            _nonempty_text(item, f"{label}[{index}]", maximum=maximum_length)
        )
    return result


def _unique_ids(value: Any, label: str, *, maximum_items: int) -> list[str]:
    result = _text_list(
        value,
        label,
        maximum_items=maximum_items,
        maximum_length=200,
    )
    if len(result) != len(set(result)):
        raise ValueError(f"{label} contains duplicates")
    return result


class HypothesisLedger:
    """Validate and persist one entity-guided discovery run."""

    def __init__(
        self,
        workspace: Any,
        store: EntityStore,
        *,
        workspace_dir: str,
        max_hypotheses: int,
        max_candidates: int,
    ) -> None:
        self.workspace = workspace
        self.store = store
        self.workspace_dir = workspace_dir
        self.max_hypotheses = max_hypotheses
        self.max_candidates = max_candidates

    def _path(self, run_id: str, name: str) -> str:
        return (Path(self.workspace_dir) / "runs" / run_id / name).as_posix()

    def _read_json(self, run_id: str, name: str, *, required: bool = True) -> dict[str, Any] | None:
        try:
            text = self.workspace.read_text(self._path(run_id, name), encoding="utf-8")
        except FileNotFoundError:
            if required:
                raise ValueError(f"required run artifact is missing: {name}") from None
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"run artifact is invalid JSON: {name}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"run artifact must be an object: {name}")
        return value

    def _write_json(self, run_id: str, name: str, value: Mapping[str, Any]) -> None:
        self.workspace.write_text(
            self._path(run_id, name),
            json.dumps(_plain(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _existing_artifacts(self, run_id: str, names: list[str]) -> list[str]:
        existing = []
        for name in names:
            try:
                self.workspace.read_text(self._path(run_id, name), encoding="utf-8")
            except FileNotFoundError:
                continue
            existing.append(name)
        return existing

    def stage_hypothesis(self, run_id: str, context_digest: Any, slot: Any, hypothesis: Any) -> dict[str, Any]:
        self._require_context_digest(run_id, context_digest)
        if self._read_json(run_id, "02_mechanism_hypotheses.json", required=False):
            raise ValueError("mechanism hypotheses are already committed")
        position = int(slot)
        if not 1 <= position <= self.max_hypotheses:
            raise ValueError(f"slot must be between 1 and {self.max_hypotheses}")
        if not isinstance(hypothesis, Mapping):
            raise ValueError("hypothesis must be an object")
        bridge = hypothesis.get("counterfactual_bridge")
        required_bridge = {
            "shared_events", "missing_events", "conflicting_events", "native_termination",
            "target_alternative_termination", "unsupported_assumptions",
        }
        if not isinstance(bridge, Mapping) or not required_bridge <= set(bridge):
            raise ValueError("staged hypothesis requires a complete counterfactual_bridge")
        path = self._path(run_id, "mechanism_drafts.json")
        try:
            drafts = json.loads(self.workspace.read_text(path, encoding="utf-8"))
        except FileNotFoundError:
            drafts = {}
        drafts[str(position)] = _plain(dict(hypothesis))
        self.workspace.write_text(path, json.dumps(drafts, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"schema_version": "1.0", "run_id": run_id, "slot": position,
                "staged_hypothesis_id": str(hypothesis.get("hypothesis_id") or ""),
                "draft_count": len(drafts), "committed_artifacts": []}

    def staged_hypotheses(self, run_id: str) -> list[dict[str, Any]]:
        try:
            drafts = json.loads(self.workspace.read_text(self._path(run_id, "mechanism_drafts.json"), encoding="utf-8"))
        except FileNotFoundError:
            return []
        return [drafts[key] for key in sorted(drafts, key=int)]

    def private_references(self, run_id):
        return sorted(set().union(*(private_references(self._read_json(run_id, name, required=False))
                                   for name in (*_ARTIFACTS, '04_critical_report.json'))))

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self._read_json(run_id, "run.json")
        assert run is not None
        if run.get("run_id") != run_id:
            raise ValueError("run artifact does not match run_id")
        if run.get("bundle_digest") != self.store.bundle_digest:
            raise ValueError("run was initialized with a different entity bundle")
        return run

    def _update_run(self, run_id: str, **changes: Any) -> dict[str, Any]:
        run = self._require_run(run_id)
        run.update(_plain(changes))
        self._write_json(run_id, "run.json", run)
        return run

    def context_digest(self, run_id: str) -> str:
        self._require_run(run_id)
        documents = []
        for name in _ARTIFACTS:
            document = self._read_json(run_id, name, required=False)
            if document is not None:
                documents.append({"name": name, "document": document})
        return hashlib.sha256(
            canonical_bytes(
                {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "bundle_digest": self.store.bundle_digest,
                    "artifacts": documents,
                }
            )
        ).hexdigest()

    def _require_context_digest(self, run_id: str, supplied: Any) -> str:
        current = self.context_digest(run_id)
        if str(supplied or "") != current:
            # Return the authoritative value in the validation error.  A
            # worker can refresh and retry deterministically without guessing
            # or repeating a long evidence-reading loop.
            raise ValueError(
                "context_digest is stale; call enzyme_entity_run_context again "
                f"and use context_digest={current}"
            )
        return current

    def initialize(self, run_id: str, target_reaction: Any) -> dict[str, Any]:
        if not isinstance(target_reaction, Mapping):
            raise ValueError("target_reaction must be an object")
        target = _plain(dict(target_reaction))
        allowed = {
            "schema_version",
            "reaction_id",
            "reaction_name",
            "reactants",
            "products",
            "reactant_names",
            "product_names",
            "natural_language_request",
            "target_description",
            "context",
            "constraints",
            "component_assumptions",
            "substrate_or_product",
        }
        unknown = sorted(set(target) - allowed)
        if unknown:
            raise ValueError(f"unknown target_reaction fields: {', '.join(unknown)}")
        has_description = any(
            target.get(key)
            for key in (
                "reactants",
                "products",
                "reactant_names",
                "product_names",
                "natural_language_request",
                "target_description",
                "substrate_or_product",
            )
        )
        if not has_description:
            raise ValueError("target_reaction must describe reactants/products or a reaction request")
        existing = self._read_json(run_id, "run.json", required=False)
        document = {
            "schema_version": "1.0",
            "run_id": run_id,
            "bundle_digest": self.store.bundle_digest,
            "target_reaction": target,
            "stage_status": {
                "reaction_analysis": "initialized",
                "mechanism_and_capability": "pending",
                "family_screening_and_routes": "pending",
                "critical_report": "pending",
            },
            "knowledge_policy": {
                "offline_only": True,
                "published_ontology_mutable": False,
                "entity_bundle_scope": "five_curated_cards",
            },
        }
        if existing is not None and str(existing.get("run_id") or "") != run_id:
            existing = None
        if existing is not None and not allow_supersede:
            if existing != document:
                raise ValueError("run_id already exists with different input or state")
            status = "already_initialized"
        else:
            self._write_json(run_id, "run.json", document)
            status = "initialized"
        return {
            "status": status,
            "run_id": run_id,
            "bundle_digest": self.store.bundle_digest,
            "committed_artifacts": ["run.json"],
            "next_stage": "reaction_analysis",
        }

    def commit_reaction(self, run_id: str, analysis: Any) -> dict[str, Any]:
        self._require_run(run_id)
        if self._read_json(run_id, "01_reaction_analysis.json", required=False):
            raise ValueError("reaction analysis is already committed")
        if not isinstance(analysis, Mapping):
            raise ValueError("analysis must be an object")
        raw = dict(analysis)
        direction = str(raw.get("direction") or "unknown")
        if direction not in {"forward", "reverse", "reversible", "unknown"}:
            raise ValueError("analysis.direction is invalid")
        changes_raw = raw.get("net_changes") or []
        if not isinstance(changes_raw, list) or len(changes_raw) > 12:
            raise ValueError("analysis.net_changes must contain at most 12 items")
        net_changes = []
        seen_ids: set[str] = set()
        for index, item in enumerate(changes_raw):
            if not isinstance(item, Mapping):
                raise ValueError(f"analysis.net_changes[{index}] must be an object")
            change_id = _nonempty_text(item.get("change_id"), f"net_changes[{index}].change_id", maximum=96)
            if not _SAFE_ID.fullmatch(change_id) or change_id in seen_ids:
                raise ValueError(f"invalid or duplicate change_id: {change_id}")
            seen_ids.add(change_id)
            confidence = str(item.get("confidence") or "unknown")
            if confidence not in {"high", "medium", "low", "unknown"}:
                raise ValueError(f"invalid confidence for {change_id}")
            net_changes.append(
                {
                    "change_id": change_id,
                    "description": _nonempty_text(item.get("description"), f"net_changes[{index}].description"),
                    "basis": "user_input_interpretation",
                    "confidence": confidence,
                }
            )
        document = {
            "schema_version": "1.0",
            "run_id": run_id,
            "analysis_scope": "text_only_without_atom_mapping",
            "direction": direction,
            "reaction_summary": _nonempty_text(raw.get("reaction_summary"), "analysis.reaction_summary"),
            "net_changes": net_changes,
            "assumptions": _text_list(raw.get("assumptions"), "analysis.assumptions", maximum_items=12),
            "warnings": _text_list(raw.get("warnings"), "analysis.warnings", maximum_items=12),
            "unresolved_questions": _text_list(
                raw.get("unresolved_questions"),
                "analysis.unresolved_questions",
                maximum_items=12,
            ),
            "candidate_entities_intentionally_omitted": True,
        }
        self._write_json(run_id, "01_reaction_analysis.json", document)
        run = self._require_run(run_id)
        statuses = dict(run["stage_status"])
        statuses["reaction_analysis"] = "complete"
        statuses["mechanism_and_capability"] = "ready"
        self._update_run(run_id, stage_status=statuses)
        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "committed_artifacts": ["01_reaction_analysis.json"],
            "net_change_count": len(net_changes),
            "warning_count": len(document["warnings"]),
            "unresolved_question_count": len(document["unresolved_questions"]),
        }

    def context(self, run_id: str, stage: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        if stage not in {"mechanism_and_capability", "family_screening_and_routes", "critical_report"}:
            raise ValueError("stage is invalid")
        required = {
            "mechanism_and_capability": ("01_reaction_analysis.json",),
            "family_screening_and_routes": (
                "01_reaction_analysis.json",
                "02_mechanism_hypotheses.json",
            ),
            "critical_report": (
                "01_reaction_analysis.json",
                "02_mechanism_hypotheses.json",
                "baseline_candidate_draft.json",
                "03_candidate_and_routes.json",
            ),
        }[stage]
        artifacts = {name: self._read_json(run_id, name) for name in required}
        return {
            "status": "ready",
            "run_id": run_id,
            "stage": stage,
            "bundle_digest": self.store.bundle_digest,
            "target_reaction": run["target_reaction"],
            "artifacts": artifacts,
            "context_digest": self.context_digest(run_id),
            "fact_policy": {
                "fact_levels": sorted(_FACT_LEVELS),
                "hypothesis_levels": sorted(_HYPOTHESIS_LEVELS),
                "fact_requires_read_evidence": True,
                "no_record_means_unknown": True,
                "target_activity_is_not_proven_by_analogy": True,
            },
        }

    def _read_audit(self, run_id: str) -> list[dict[str, Any]]:
        try:
            text = self.workspace.read_text(
                self._path(run_id, "entity_usage.jsonl"), encoding="utf-8"
            )
        except FileNotFoundError:
            return []
        rows = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("entity_usage.jsonl contains invalid JSON") from exc
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def evidence_read_ids(
        self,
        run_id: str,
        *,
        agent: str | None = None,
    ) -> tuple[set[str], set[str]]:
        assertion_ids: set[str] = set()
        evidence_ids: set[str] = set()
        for row in self._read_audit(run_id):
            if row.get("operation") != "enzyme_entity_read_evidence" or row.get("ok") is not True:
                continue
            if agent is not None and row.get("agent") != agent:
                continue
            summary = row.get("result_summary") or {}
            assertion_ids.update(summary.get("assertion_ids") or [])
            evidence_ids.update(summary.get("evidence_ids") or [])
        return assertion_ids, evidence_ids

    def enforce_candidate_baseline_gate(self, run_id: str) -> None:
        """Keep stage-three entity lookup behind the frozen independent draft."""

        run = self._read_json(run_id, "run.json", required=False)
        if run is None:
            return
        if run.get("bundle_digest") != self.store.bundle_digest:
            raise ValueError("run was initialized with a different entity bundle")
        mechanisms_exist = self._read_json(
            run_id, "02_mechanism_hypotheses.json", required=False
        ) is not None
        baseline_exists = self._read_json(
            run_id, "baseline_candidate_draft.json", required=False
        ) is not None
        if mechanisms_exist and not baseline_exists:
            raise ValueError(
                "freeze baseline candidates before any stage-three entity lookup"
            )

    def _validate_entity_ids(self, values: Any, label: str, allowed_types: set[str]) -> list[str]:
        identifiers = _unique_ids(values, label, maximum_items=12)
        missing = sorted(item for item in set(identifiers) if item not in self.store.entities)
        if missing:
            raise ValueError(f"{label} contains unknown entities: {', '.join(missing)}")
        wrong = sorted(
            item
            for item in identifiers
            if self.store.entities[item]["entity_type"] not in allowed_types
        )
        if wrong:
            raise ValueError(f"{label} contains incompatible entity types: {', '.join(wrong)}")
        return identifiers

    def _claim(self, run_id: str, value: Any, label: str, expected_levels: set[str]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{label} must be an object")
        claim_id = _nonempty_text(value.get("claim_id"), f"{label}.claim_id", maximum=96)
        if not _SAFE_ID.fullmatch(claim_id):
            raise ValueError(f"{label}.claim_id has an invalid format")
        evidence_level = str(value.get("evidence_level") or "")
        if evidence_level not in expected_levels:
            raise ValueError(f"{label}.evidence_level is invalid")
        assertion_ids = _unique_ids(value.get("assertion_ids"), f"{label}.assertion_ids", maximum_items=8)
        evidence_ids = _unique_ids(value.get("evidence_ids"), f"{label}.evidence_ids", maximum_items=12)
        missing_assertions = sorted(item for item in set(assertion_ids) if item not in self.store.assertions)
        missing_evidence = sorted(item for item in set(evidence_ids) if item not in self.store.evidence)
        if missing_assertions:
            raise ValueError(f"{label} references unknown assertions: {', '.join(missing_assertions)}")
        if missing_evidence:
            raise ValueError(f"{label} references unknown evidence: {', '.join(missing_evidence)}")
        if evidence_level in _FACT_LEVELS:
            if any(self.store.assertions[a].get('scope') == 'private' and self.store.assertions[a].get('epistemic_kind') != 'reported' for a in assertion_ids):
                raise ValueError('private hypotheses/annotations cannot be submitted as reported facts')
            if not assertion_ids or not evidence_ids:
                raise ValueError(f"{label} is a reported fact and requires assertion_ids plus evidence_ids")
            linked = {
                evidence_id
                for assertion_id in assertion_ids
                for evidence_id in self.store.assertions[assertion_id].get("evidence_ids") or []
            }
            private_linked = {e for a in assertion_ids if a.startswith('private-assertion:') for e in self.store.assertions[a].get('evidence_ids', [])}
            if not private_linked <= set(evidence_ids):
                raise ValueError('each private assertion requires its own evidence reference')
            if not set(evidence_ids) <= linked:
                raise ValueError(f"{label} cites evidence not linked to its assertions")
            if evidence_level == "reported_exact" and not any(
                self.store.assertions[assertion_id].get("attribution")
                == "exact_protein"
                for assertion_id in assertion_ids
            ):
                # A broader assertion can never support an exact-protein claim.
                # Downgrading preserves the cited fact while enforcing the
                # attribution boundary deterministically.
                evidence_level = "reported_related"
            _read_assertions, read_evidence = self.evidence_read_ids(run_id)
            unread = sorted(set(evidence_ids) - read_evidence)
            if unread:
                raise ValueError(f"{label} cites evidence not read in this run: {', '.join(unread)}")
        elif evidence_ids:
            _read_assertions, read_evidence = self.evidence_read_ids(run_id)
            unread = sorted(set(evidence_ids) - read_evidence)
            if unread:
                raise ValueError(f"{label} cites evidence not read in this run: {', '.join(unread)}")
        return {
            "claim_id": claim_id,
            "text": _nonempty_text(value.get("text"), f"{label}.text"),
            "evidence_level": evidence_level,
            "assertion_ids": assertion_ids,
            "evidence_ids": evidence_ids,
            "scope_or_method": _nonempty_text(
                value.get("scope_or_method"), f"{label}.scope_or_method"
            ),
        }

    def commit_hypotheses(
        self,
        run_id: str,
        context_digest: Any,
        hypotheses: Any,
        global_gaps: Any,
    ) -> dict[str, Any]:
        self._require_context_digest(run_id, context_digest)
        if self._read_json(run_id, "02_mechanism_hypotheses.json", required=False):
            raise ValueError("mechanism hypotheses are already committed")
        if hypotheses == []:
            hypotheses = self.staged_hypotheses(run_id)
        if not isinstance(hypotheses, list) or not 1 <= len(hypotheses) <= self.max_hypotheses:
            raise ValueError(f"hypotheses must contain 1 to {self.max_hypotheses} items")
        normalized = []
        seen_hypotheses: set[str] = set()
        seen_claims: set[str] = set()
        for index, raw in enumerate(hypotheses):
            if not isinstance(raw, Mapping):
                raise ValueError(f"hypotheses[{index}] must be an object")
            hypothesis_id = _nonempty_text(raw.get("hypothesis_id"), f"hypotheses[{index}].hypothesis_id", maximum=96)
            if not _SAFE_ID.fullmatch(hypothesis_id) or hypothesis_id in seen_hypotheses:
                raise ValueError(f"invalid or duplicate hypothesis_id: {hypothesis_id}")
            seen_hypotheses.add(hypothesis_id)
            fact_claims = [
                self._claim(run_id, item, f"hypotheses[{index}].fact_claims[{claim_index}]", _FACT_LEVELS)
                for claim_index, item in enumerate(raw.get("fact_claims") or [])
            ]
            analogy_claims = [
                self._claim(run_id, item, f"hypotheses[{index}].analogy_claims[{claim_index}]", {"mechanistic_analogy"})
                for claim_index, item in enumerate(raw.get("analogy_claims") or [])
            ]
            prediction_claims = [
                self._claim(run_id, item, f"hypotheses[{index}].prediction_claims[{claim_index}]", {"computed_prediction", "model_background_unverified"})
                for claim_index, item in enumerate(raw.get("prediction_claims") or [])
            ]
            for claim in [*fact_claims, *analogy_claims, *prediction_claims]:
                if claim["claim_id"] in seen_claims:
                    raise ValueError(f"duplicate claim_id: {claim['claim_id']}")
                seen_claims.add(claim["claim_id"])
            raw_bridge = raw.get("counterfactual_bridge")
            if raw_bridge is not None and not isinstance(raw_bridge, Mapping):
                raise ValueError(
                    f"hypotheses[{index}].counterfactual_bridge must be an object"
                )
            bridge = dict(raw_bridge or {})
            shared_events = _text_list(
                bridge.get("shared_events")
                if raw_bridge is not None
                else [claim["text"] for claim in [*fact_claims, *analogy_claims]],
                f"hypotheses[{index}].counterfactual_bridge.shared_events",
                maximum_items=12,
            )
            missing_events = _text_list(
                bridge.get("missing_events")
                if raw_bridge is not None
                else raw.get("evidence_gaps"),
                f"hypotheses[{index}].counterfactual_bridge.missing_events",
                maximum_items=12,
            )
            conflicting_events = _text_list(
                bridge.get("conflicting_events")
                if raw_bridge is not None
                else raw.get("counterarguments"),
                f"hypotheses[{index}].counterfactual_bridge.conflicting_events",
                maximum_items=12,
            )
            counterfactual_bridge = {
                "shared_events": shared_events,
                "missing_events": missing_events,
                "conflicting_events": conflicting_events,
                "native_termination": _text_list(
                    bridge.get("native_termination"),
                    f"hypotheses[{index}].counterfactual_bridge.native_termination",
                    maximum_items=8,
                ),
                "target_alternative_termination": _text_list(
                    bridge.get("target_alternative_termination"),
                    f"hypotheses[{index}].counterfactual_bridge.target_alternative_termination",
                    maximum_items=8,
                ),
                "unsupported_assumptions": _text_list(
                    bridge.get("unsupported_assumptions")
                    if raw_bridge is not None
                    else raw.get("evidence_gaps"),
                    f"hypotheses[{index}].counterfactual_bridge.unsupported_assumptions",
                    maximum_items=12,
                ),
                "target_activity_status": "hypothesis_unvalidated",
                "source": "explicit_alignment" if raw_bridge is not None else "legacy_derived",
            }
            normalized.append(
                {
                    "hypothesis_id": hypothesis_id,
                    "title": _nonempty_text(raw.get("title"), f"hypotheses[{index}].title", maximum=240),
                    "proposed_mechanism": _nonempty_text(raw.get("proposed_mechanism"), f"hypotheses[{index}].proposed_mechanism"),
                    "required_capability_ids": self._validate_entity_ids(
                        raw.get("required_capability_ids"),
                        f"hypotheses[{index}].required_capability_ids",
                        {"CatalyticCapability"},
                    ),
                    "required_cofactor_ids": self._validate_entity_ids(
                        raw.get("required_cofactor_ids"),
                        f"hypotheses[{index}].required_cofactor_ids",
                        {"ChemicalEntity", "ChemicalClass"},
                    ),
                    "intermediate_ids": self._validate_entity_ids(
                        raw.get("intermediate_ids"),
                        f"hypotheses[{index}].intermediate_ids",
                        {"MechanisticIntermediate"},
                    ),
                    "fact_claims": fact_claims,
                    "analogy_claims": analogy_claims,
                    "prediction_claims": prediction_claims,
                    "counterarguments": _text_list(raw.get("counterarguments"), f"hypotheses[{index}].counterarguments", maximum_items=8),
                    "evidence_gaps": _text_list(raw.get("evidence_gaps"), f"hypotheses[{index}].evidence_gaps", maximum_items=8),
                    "falsification_tests": _text_list(raw.get("falsification_tests"), f"hypotheses[{index}].falsification_tests", maximum_items=8),
                    "counterfactual_bridge": counterfactual_bridge,
                    "target_activity_status": "hypothesis_unvalidated",
                }
            )
        document = {
            "schema_version": "1.0",
            "run_id": run_id,
            "bundle_digest": self.store.bundle_digest,
            "hypotheses": normalized,
            "global_knowledge_gaps": _text_list(global_gaps, "global_knowledge_gaps", maximum_items=12),
            "published_ontology_modified": False,
            "reasoning_protocol": _PRODUCTION_PROTOCOL,
            "knowledge_backend": "ontology_assertion_evidence",
        }
        self._write_json(run_id, "02_mechanism_hypotheses.json", document)
        # Keep the original artifact for compatibility while exposing the same
        # phase boundary used by the production Wiki workflow.
        self._write_json(run_id, "02_mechanisms.json", {
            "schema_version": "1.0", "run_id": run_id,
            "mechanisms": normalized, "reasoning_protocol": _PRODUCTION_PROTOCOL,
            "knowledge_backend": "ontology_assertion_evidence",
        })
        self._write_json(run_id, "03_capabilities.json", {
            "schema_version": "1.0", "run_id": run_id,
            "capabilities": sorted({item for row in normalized for item in row["required_capability_ids"]}),
            "cofactors": sorted({item for row in normalized for item in row["required_cofactor_ids"]}),
            "knowledge_gaps": document["global_knowledge_gaps"],
            "reasoning_protocol": _PRODUCTION_PROTOCOL,
        })
        self.workspace.write_text(
            self._path(run_id, "hypotheses.jsonl"),
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in normalized),
            encoding="utf-8",
        )
        run = self._require_run(run_id)
        statuses = dict(run["stage_status"])
        statuses["mechanism_and_capability"] = "complete"
        statuses["family_screening_and_routes"] = "ready"
        self._update_run(run_id, stage_status=statuses)
        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "committed_artifacts": ["02_mechanism_hypotheses.json", "02_mechanisms.json", "03_capabilities.json", "hypotheses.jsonl"],
            "hypothesis_count": len(normalized),
            "fact_claim_count": sum(len(item["fact_claims"]) for item in normalized),
            "prediction_claim_count": sum(
                len(item["analogy_claims"]) + len(item["prediction_claims"])
                for item in normalized
            ),
        }

    def commit_baseline(self, run_id: str, context_digest: Any, candidates: Any) -> dict[str, Any]:
        # A worker may replay the same commit after context compaction or a
        # transient transport failure.  Once the draft is durably frozen,
        # return its receipt instead of turning a safe replay into a stage
        # failure.  The run id and schema guard prevent cross-run acceptance.
        existing = self._read_json(run_id, "baseline_candidate_draft.json", required=False)
        if isinstance(existing, Mapping) and existing.get("run_id") == run_id and existing.get("schema_version") == "1.0":
            frozen = existing.get("baseline_candidate_draft")
            if isinstance(frozen, list):
                return {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "committed_artifacts": ["baseline_candidate_draft.json"],
                    "baseline_candidate_count": len(frozen),
                    "baseline_frozen": True,
                    "context_digest": self.context_digest(run_id),
                    "replayed": True,
                }
        self._require_context_digest(run_id, context_digest)
        self._read_json(run_id, "02_mechanism_hypotheses.json")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= self.max_candidates:
            raise ValueError(f"candidates must contain 1 to {self.max_candidates} items")
        normalized = []
        seen: set[str] = set()
        for index, raw in enumerate(candidates):
            if not isinstance(raw, Mapping):
                raise ValueError(f"candidates[{index}] must be an object")
            forbidden = set(raw) & {"entity_id", "assertion_ids", "evidence_ids", "wiki_ref", "card_id"}
            if forbidden:
                raise ValueError("baseline candidates must not contain entity or evidence references")
            proposal_id = _nonempty_text(raw.get("proposal_id"), f"candidates[{index}].proposal_id", maximum=96)
            if not _SAFE_ID.fullmatch(proposal_id) or proposal_id in seen:
                raise ValueError(f"invalid or duplicate proposal_id: {proposal_id}")
            seen.add(proposal_id)
            identity_level = str(raw.get("identity_level") or "")
            if identity_level not in {"protein", "named_enzyme", "family", "capability_gap"}:
                raise ValueError(f"invalid identity_level for {proposal_id}")
            label = _nonempty_text(raw.get("label"), f"candidates[{index}].label", maximum=240)
            normalized.append(
                {
                    "proposal_id": proposal_id,
                    "label": label,
                    "identity_level": identity_level,
                    "fit_reason": _nonempty_text(raw.get("fit_reason"), f"candidates[{index}].fit_reason"),
                    "knowledge_gaps": _text_list(raw.get("knowledge_gaps"), f"candidates[{index}].knowledge_gaps", maximum_items=6),
                    "evidence_level": "model_background_unverified",
                    "frozen_before_entity_candidate_lookup": True,
                }
            )
        document = {
            "schema_version": "1.0",
            "run_id": run_id,
            "baseline_candidate_draft": normalized,
            "entity_references_present": False,
        }
        self._write_json(run_id, "baseline_candidate_draft.json", document)
        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "committed_artifacts": ["baseline_candidate_draft.json"],
            "baseline_candidate_count": len(normalized),
            "baseline_frozen": True,
            "context_digest": self.context_digest(run_id),
        }

    def capability_candidates(
        self,
        run_id: str,
        *,
        capability_ids: list[str],
        cofactor_ids: list[str],
        intermediate_ids: list[str],
        limit: int,
    ) -> dict[str, Any]:
        self._require_run(run_id)
        self._read_json(run_id, "baseline_candidate_draft.json")
        capabilities = self._validate_entity_ids(capability_ids, "capability_ids", {"CatalyticCapability"})
        if not capabilities:
            raise ValueError("capability_ids must not be empty")
        cofactors = self._validate_entity_ids(cofactor_ids, "cofactor_ids", {"ChemicalEntity", "ChemicalClass"})
        intermediates = self._validate_entity_ids(intermediate_ids, "intermediate_ids", {"MechanisticIntermediate"})

        rows_by_subject: dict[str, list[dict[str, Any]]] = {}
        for capability_id in capabilities:
            for assertion in self.store.incoming.get(capability_id, ()):
                if (
                    assertion.get("predicate") == "supports_capability"
                    and assertion.get("state") == "active"
                    and assertion.get("polarity") == "supports"
                    and self.store.entities[assertion["subject_id"]]["entity_type"]
                    in {"Protein", "ProteinGroup", "EnzymeFamily"}
                ):
                    rows_by_subject.setdefault(assertion["subject_id"], []).append(assertion)

        intermediate_reactions: dict[str, list[dict[str, Any]]] = {}
        for intermediate_id in intermediates:
            for assertion in self.store.incoming.get(intermediate_id, ()):
                if assertion.get("predicate") == "has_intermediate" and assertion.get("polarity") == "supports":
                    intermediate_reactions.setdefault(assertion["subject_id"], []).append(assertion)
        for reaction_id, intermediate_assertions in intermediate_reactions.items():
            for catalyst in self.store.incoming.get(reaction_id, ()):
                if catalyst.get("predicate") != "catalyzes_reported" or catalyst.get("polarity") != "supports":
                    continue
                subject_id = catalyst["subject_id"]
                if self.store.entities[subject_id]["entity_type"] not in {"Protein", "ProteinGroup", "EnzymeFamily"}:
                    continue
                existing = rows_by_subject.get(subject_id)
                if existing is not None:
                    existing.extend([*intermediate_assertions, catalyst])

        results = []
        family_precedents: dict[str, dict[str, Any]] = {}
        for subject_id, matched_rows in rows_by_subject.items():
            outgoing = [
                row
                for row in self.store.outgoing.get(subject_id, ())
                if row.get("state") == "active" and row.get("polarity") == "supports"
            ]
            cofactor_rows = [
                row
                for row in outgoing
                if row.get("predicate") == "uses_cofactor" and row.get("object_id") in set(cofactors)
            ]
            family_rows = [row for row in outgoing if row.get("predicate") == "member_of"]
            all_rows = {row["assertion_id"]: row for row in [*matched_rows, *cofactor_rows, *family_rows]}
            entity = self.store.entity_summary(subject_id)
            results.append(
                {
                    "entity": entity,
                    "identity_scope": entity.get("identity_scope"),
                    "matched_capability_ids": sorted(
                        {
                            row["object_id"]
                            for row in matched_rows
                            if row.get("predicate") == "supports_capability"
                        }
                    ),
                    "matched_cofactor_ids": sorted({row["object_id"] for row in cofactor_rows}),
                    "matched_intermediate_ids": sorted(
                        {
                            row["object_id"]
                            for row in matched_rows
                            if row.get("predicate") == "has_intermediate"
                        }
                    ),
                    "explicit_family_memberships": [
                        self.store.entity_summary(row["object_id"]) for row in family_rows
                    ],
                    "assertion_ids": sorted(all_rows),
                    "evidence_ids": sorted(
                        {
                            evidence_id
                            for row in all_rows.values()
                            for evidence_id in row.get("evidence_ids") or []
                        }
                    ),
                    "candidate_evidence_level": "mechanistic_analogy",
                    "target_activity_proven": False,
                }
            )
            for membership in family_rows:
                family_id = membership["object_id"]
                family = family_precedents.setdefault(
                    family_id,
                    {
                        "entity": self.store.entity_summary(family_id),
                        "identity_scope": "family",
                        "matched_capability_ids": set(),
                        "matched_cofactor_ids": set(),
                        "matched_intermediate_ids": set(),
                        "explicit_member_precedents": set(),
                        "assertion_rows": {},
                        "candidate_evidence_level": "mechanistic_analogy",
                        "target_activity_proven": False,
                        "attribution_boundary": "member_protein_precedent_not_family_activity",
                    },
                )
                family["matched_capability_ids"].update(
                    row["object_id"]
                    for row in matched_rows
                    if row.get("predicate") == "supports_capability"
                )
                family["matched_cofactor_ids"].update(row["object_id"] for row in cofactor_rows)
                family["matched_intermediate_ids"].update(
                    row["object_id"]
                    for row in matched_rows
                    if row.get("predicate") == "has_intermediate"
                )
                family["explicit_member_precedents"].add(subject_id)
                family["assertion_rows"].update(all_rows)
        for family in family_precedents.values():
            assertion_rows = family.pop("assertion_rows")
            family["matched_capability_ids"] = sorted(family["matched_capability_ids"])
            family["matched_cofactor_ids"] = sorted(family["matched_cofactor_ids"])
            family["matched_intermediate_ids"] = sorted(family["matched_intermediate_ids"])
            family["explicit_member_precedents"] = sorted(family["explicit_member_precedents"])
            family["explicit_family_memberships"] = []
            family["assertion_ids"] = sorted(assertion_rows)
            family["evidence_ids"] = sorted(
                {
                    evidence_id
                    for row in assertion_rows.values()
                    for evidence_id in row.get("evidence_ids") or []
                }
            )
            results.append(family)
        type_order = {"EnzymeFamily": 0, "ProteinGroup": 1, "Protein": 2}
        results.sort(
            key=lambda row: (
                type_order.get(row["entity"]["entity_type"], 9),
                row["entity"]["label"].casefold(),
                row["entity"]["entity_id"],
            )
        )
        return {
            "status": "matched" if results else "no_recorded_candidate",
            "candidates": results[:limit],
            "total": len(results),
            "evidence_required": bool(results),
            "ranking_note": "Deterministic identity ordering only; no catalytic fitness score is computed.",
            "knowledge_boundary": (
                "Candidates share explicit reported capabilities or intermediates. This does not establish target activity."
                if results
                else "The current five-card bundle has no candidate with the requested explicit capability."
            ),
        }

    def record_route_options(self, run_id: str, result: Mapping[str, Any]) -> str:
        """Persist bounded deterministic route options for later commit by ref."""

        self._require_run(run_id)
        self._read_json(run_id, "baseline_candidate_draft.json")
        path = self._path(run_id, "route_options.jsonl")
        try:
            current = self.workspace.read_text(path, encoding="utf-8")
        except FileNotFoundError:
            current = ""
        rows: dict[str, dict[str, Any]] = {}
        for line in current.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("route_options.jsonl contains invalid JSON") from exc
            if isinstance(row, dict) and row.get("route_ref"):
                rows[str(row["route_ref"])] = row
        for route in result.get("routes") or []:
            if isinstance(route, Mapping) and route.get("route_ref"):
                rows[str(route["route_ref"])] = _plain(route)
        self.workspace.write_text(
            path,
            "".join(
                json.dumps(rows[key], ensure_ascii=False, sort_keys=True) + "\n"
                for key in sorted(rows)
            ),
            encoding="utf-8",
        )
        return path

    def _route_options(self, run_id: str) -> dict[str, dict[str, Any]]:
        try:
            text = self.workspace.read_text(
                self._path(run_id, "route_options.jsonl"), encoding="utf-8"
            )
        except FileNotFoundError:
            return {}
        rows: dict[str, dict[str, Any]] = {}
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("route_options.jsonl contains invalid JSON") from exc
            if not isinstance(row, dict) or not row.get("route_ref"):
                raise ValueError("route_options.jsonl contains an invalid route option")
            rows[str(row["route_ref"])] = row
        return rows

    def _normalize_routes(
        self,
        run_id: str,
        values: Any,
        candidate_ids: set[str],
    ) -> list[dict[str, Any]]:
        if not isinstance(values, list) or len(values) > 8:
            raise ValueError("routes must contain at most 8 items")
        known_options = self._route_options(run_id)
        _read_assertions, read_evidence = self.evidence_read_ids(run_id)
        required_checks = {
            "material_handoff",
            "direction",
            "cofactor_resource",
            "energy_balance",
            "byproduct_control",
            "condition_compatibility",
            "intermediate_stability",
        }
        route_ids: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(values):
            if not isinstance(raw, Mapping):
                raise ValueError(f"routes[{index}] must be an object")
            route_id = _nonempty_text(raw.get("route_id"), f"routes[{index}].route_id", maximum=96)
            if not _SAFE_ID.fullmatch(route_id) or route_id in route_ids:
                raise ValueError(f"invalid or duplicate route_id: {route_id}")
            route_ids.add(route_id)
            route_kind = str(raw.get("route_kind") or "")
            if route_kind not in {
                "reported_path_hypothesis",
                "hybrid_path_hypothesis",
                "model_path_hypothesis",
            }:
                raise ValueError(f"invalid route_kind for {route_id}")
            route_ref = str(raw.get("route_ref") or "")
            reported_path = None
            if route_ref:
                reported_path = known_options.get(route_ref)
                if reported_path is None:
                    raise ValueError(f"route {route_id} references an unknown route_ref")
                unread = sorted(set(reported_path.get("evidence_ids") or []) - read_evidence)
                if unread:
                    raise ValueError(
                        f"route {route_id} cites reported steps with unread evidence: {', '.join(unread)}"
                    )
            if route_kind == "reported_path_hypothesis" and reported_path is None:
                raise ValueError(f"reported route {route_id} requires route_ref")
            if route_kind == "model_path_hypothesis" and reported_path is not None:
                raise ValueError(f"model route {route_id} must not set route_ref")

            raw_steps = raw.get("hypothetical_steps") or []
            if not isinstance(raw_steps, list) or len(raw_steps) > 8:
                raise ValueError(f"route {route_id} hypothetical_steps must contain at most 8 items")
            if route_kind != "reported_path_hypothesis" and not raw_steps:
                raise ValueError(f"route {route_id} requires at least one hypothetical step")
            hypothetical_steps = []
            step_ids: set[str] = set()
            for step_index, step in enumerate(raw_steps):
                if not isinstance(step, Mapping):
                    raise ValueError(f"route {route_id} hypothetical step must be an object")
                step_id = _nonempty_text(
                    step.get("step_id"),
                    f"routes[{index}].hypothetical_steps[{step_index}].step_id",
                    maximum=96,
                )
                if not _SAFE_ID.fullmatch(step_id) or step_id in step_ids:
                    raise ValueError(f"invalid or duplicate step_id in route {route_id}: {step_id}")
                step_ids.add(step_id)
                linked_candidates = _unique_ids(
                    step.get("candidate_ids"),
                    f"route {route_id} step {step_id} candidate_ids",
                    maximum_items=8,
                )
                missing_candidates = sorted(set(linked_candidates) - candidate_ids)
                if missing_candidates:
                    raise ValueError(
                        f"route {route_id} step {step_id} references unknown candidates: {', '.join(missing_candidates)}"
                    )
                evidence_level = str(step.get("evidence_level") or "")
                if evidence_level not in {
                    "mechanistic_analogy",
                    "model_background_unverified",
                }:
                    raise ValueError(f"route {route_id} step {step_id} has invalid evidence_level")
                input_labels = _text_list(
                    step.get("input_labels"),
                    f"route {route_id} step {step_id}.input_labels",
                    maximum_items=12,
                    maximum_length=240,
                )
                output_labels = _text_list(
                    step.get("output_labels"),
                    f"route {route_id} step {step_id}.output_labels",
                    maximum_items=12,
                    maximum_length=240,
                )
                if not input_labels or not output_labels:
                    raise ValueError(
                        f"route {route_id} step {step_id} requires input_labels and output_labels"
                    )
                hypothetical_steps.append(
                    {
                        "step_id": step_id,
                        "description": _nonempty_text(
                            step.get("description"), f"route {route_id} step {step_id}.description"
                        ),
                        "input_labels": input_labels,
                        "output_labels": output_labels,
                        "candidate_ids": linked_candidates,
                        "evidence_level": evidence_level,
                        "knowledge_gaps": _text_list(
                            step.get("knowledge_gaps"),
                            f"route {route_id} step {step_id}.knowledge_gaps",
                            maximum_items=8,
                        ),
                        "target_activity_status": "hypothesis_unvalidated",
                    }
                )

            raw_checks = raw.get("closure_checks")
            if not isinstance(raw_checks, list) or len(raw_checks) != len(required_checks):
                raise ValueError(f"route {route_id} must contain the seven closure checks")
            closure_checks = []
            seen_checks: set[str] = set()
            for check in raw_checks:
                if not isinstance(check, Mapping):
                    raise ValueError(f"route {route_id} closure check must be an object")
                check_id = str(check.get("check_id") or "")
                if check_id not in required_checks or check_id in seen_checks:
                    raise ValueError(f"route {route_id} has invalid or duplicate check_id: {check_id}")
                seen_checks.add(check_id)
                status = str(check.get("status") or "")
                if status not in {"pass", "gap", "unknown", "not_applicable"}:
                    raise ValueError(f"route {route_id} check {check_id} has invalid status")
                closure_checks.append(
                    {
                        "check_id": check_id,
                        "status": status,
                        "finding": _nonempty_text(
                            check.get("finding"), f"route {route_id} check {check_id}.finding"
                        ),
                    }
                )
            if seen_checks != required_checks:
                raise ValueError(f"route {route_id} does not cover all closure checks")

            raw_auxiliary = raw.get("auxiliary_steps") or []
            if not isinstance(raw_auxiliary, list) or len(raw_auxiliary) > 8:
                raise ValueError(f"route {route_id} auxiliary_steps must contain at most 8 items")
            auxiliary_steps = []
            auxiliary_ids: set[str] = set()
            for auxiliary_index, auxiliary in enumerate(raw_auxiliary):
                if not isinstance(auxiliary, Mapping):
                    raise ValueError(f"route {route_id} auxiliary step must be an object")
                auxiliary_id = _nonempty_text(
                    auxiliary.get("auxiliary_id"),
                    f"routes[{index}].auxiliary_steps[{auxiliary_index}].auxiliary_id",
                    maximum=96,
                )
                if not _SAFE_ID.fullmatch(auxiliary_id) or auxiliary_id in auxiliary_ids:
                    raise ValueError(f"invalid or duplicate auxiliary_id in route {route_id}")
                auxiliary_ids.add(auxiliary_id)
                function = str(auxiliary.get("function") or "")
                if function not in {
                    "cofactor_supply_or_regeneration",
                    "energy_supply_or_regeneration",
                    "byproduct_mitigation",
                    "intermediate_protection",
                    "condition_staging",
                    "other",
                }:
                    raise ValueError(f"route {route_id} auxiliary step has invalid function")
                auxiliary_steps.append(
                    {
                        "auxiliary_id": auxiliary_id,
                        "function": function,
                        "description": _nonempty_text(
                            auxiliary.get("description"),
                            f"route {route_id} auxiliary step {auxiliary_id}.description",
                        ),
                        "required_status": str(auxiliary.get("required_status") or "unknown"),
                        "evidence_level": "model_background_unverified",
                    }
                )
                if auxiliary_steps[-1]["required_status"] not in {"required", "optional", "unknown"}:
                    raise ValueError(f"route {route_id} auxiliary step has invalid required_status")
            normalized.append(
                {
                    "route_id": route_id,
                    "title": _nonempty_text(raw.get("title"), f"route {route_id}.title", maximum=240),
                    "route_kind": route_kind,
                    "reported_path": reported_path,
                    "hypothetical_steps": hypothetical_steps,
                    "closure_checks": closure_checks,
                    "auxiliary_steps": auxiliary_steps,
                    "validation_priority": _nonempty_text(
                        raw.get("validation_priority"), f"route {route_id}.validation_priority"
                    ),
                    "target_pathway_status": "hypothesis_unvalidated",
                }
            )
        return normalized

    def commit_candidates(
        self,
        run_id: str,
        context_digest: Any,
        candidates: Any,
        routes: Any,
        comparison_notes: Any,
        route_gaps: Any,
    ) -> dict[str, Any]:
        self._require_context_digest(run_id, context_digest)
        baseline = self._read_json(run_id, "baseline_candidate_draft.json")
        assert baseline is not None
        if self._read_json(run_id, "03_candidate_and_routes.json", required=False):
            raise ValueError("candidate screening is already committed")
        if not isinstance(candidates, list) or len(candidates) > self.max_candidates:
            raise ValueError(f"candidates must contain at most {self.max_candidates} items")
        normalized = []
        seen: set[str] = set()
        for index, raw in enumerate(candidates):
            if not isinstance(raw, Mapping):
                raise ValueError(f"candidates[{index}] must be an object")
            candidate_id = _nonempty_text(raw.get("candidate_id"), f"candidates[{index}].candidate_id", maximum=96)
            if not _SAFE_ID.fullmatch(candidate_id) or candidate_id in seen:
                raise ValueError(f"invalid or duplicate candidate_id: {candidate_id}")
            seen.add(candidate_id)
            kind = str(raw.get("candidate_kind") or "")
            if kind not in _CANDIDATE_KINDS:
                raise ValueError(f"invalid candidate_kind for {candidate_id}")
            role = str(raw.get("candidate_role") or "")
            allowed_roles = {"primary", "route_component", "diagnostic_control", "family_hint", "unmatched_capability"}
            if role and role not in allowed_roles:
                raise ValueError(f"candidate {candidate_id} has invalid candidate_role")
            if kind == "unmatched_capability" and role not in {"", "unmatched_capability"}:
                raise ValueError(f"candidate {candidate_id} is an unmatched capability, not a ranked enzyme")
            if kind in {"entity_family", "entity_protein_group"} and role not in {"", "family_hint"}:
                raise ValueError(f"candidate {candidate_id} is a family hint, not a specific enzyme")
            if kind not in {"unmatched_capability", "entity_family", "entity_protein_group"} and role in {"unmatched_capability", "family_hint"}:
                raise ValueError(f"candidate {candidate_id} has a role inconsistent with its kind")
            entity_id = str(raw.get("entity_id") or "")
            proposal_id = str(raw.get("proposal_id") or "")
            if kind in _ENTITY_KIND_TYPES:
                if entity_id not in self.store.entities:
                    raise ValueError(f"candidate {candidate_id} references an unknown entity")
                if self.store.entities[entity_id]["entity_type"] != _ENTITY_KIND_TYPES[kind]:
                    raise ValueError(f"candidate {candidate_id} entity type does not match candidate_kind")
                # Entity candidates are grounded by entity_id; a copied baseline
                # proposal_id is redundant and can falsely mix model and entity
                # identity. Strip it deterministically.
                proposal_id = ""
            else:
                if entity_id:
                    raise ValueError(f"non-entity candidate {candidate_id} must not set entity_id")
                if not proposal_id or proposal_id not in {
                    row["proposal_id"] for row in baseline["baseline_candidate_draft"]
                }:
                    raise ValueError(f"candidate {candidate_id} must reference a frozen baseline proposal")
            evidence_level = str(raw.get("evidence_level") or "")
            allowed_levels = {"mechanistic_analogy"} if kind in _ENTITY_KIND_TYPES else {"model_background_unverified"}
            if evidence_level not in allowed_levels:
                raise ValueError(f"candidate {candidate_id} has an invalid evidence_level")
            assertion_ids = _unique_ids(raw.get("supporting_assertion_ids"), f"candidates[{index}].supporting_assertion_ids", maximum_items=8)
            evidence_ids = _unique_ids(raw.get("supporting_evidence_ids"), f"candidates[{index}].supporting_evidence_ids", maximum_items=12)
            if kind in _ENTITY_KIND_TYPES:
                if not assertion_ids or not evidence_ids:
                    raise ValueError(f"entity candidate {candidate_id} requires supporting assertions and evidence")
                missing_assertions = sorted(item for item in set(assertion_ids) if item not in self.store.assertions)
                if missing_assertions:
                    raise ValueError(f"candidate {candidate_id} references unknown assertions")
                linked = {
                    evidence_id
                    for assertion_id in assertion_ids
                    for evidence_id in self.store.assertions[assertion_id].get("evidence_ids") or []
                }
                evidence_ids = [
                    evidence_id for evidence_id in evidence_ids if evidence_id in linked
                ]
                if not evidence_ids:
                    raise ValueError(
                        f"candidate {candidate_id} has no evidence linked to its assertions"
                    )
                private_linked = {e for a in assertion_ids if a.startswith('private-assertion:') for e in self.store.assertions[a].get('evidence_ids', [])}
                if not private_linked <= set(evidence_ids):
                    raise ValueError('each private candidate assertion requires its own evidence reference')
                cited_assertions = [self.store.assertions[item] for item in assertion_ids]
                if any(row.get('scope') == 'private' and (row.get('epistemic_kind') != 'reported' or row.get('polarity') != 'supports') for row in cited_assertions):
                    raise ValueError('private candidate support requires reported positive assertions')
                if kind in {"entity_protein", "entity_protein_group"}:
                    relevant = any(
                        row.get("subject_id") == entity_id
                        and row.get("predicate") in {"supports_capability", "catalyzes_reported"}
                        for row in cited_assertions
                    )
                else:
                    direct = any(
                        row.get("subject_id") == entity_id
                        and row.get("predicate") == "supports_capability"
                        for row in cited_assertions
                    )
                    member_ids = {
                        row["subject_id"]
                        for row in cited_assertions
                        if row.get("predicate") == "member_of" and row.get("object_id") == entity_id
                    }
                    member_precedent = any(
                        row.get("subject_id") in member_ids
                        and row.get("predicate") in {"supports_capability", "catalyzes_reported"}
                        for row in cited_assertions
                    )
                    relevant = direct or member_precedent
                if not relevant:
                    raise ValueError(
                        f"candidate {candidate_id} evidence is not attributable to that entity"
                    )
                _read_assertions, read_evidence = self.evidence_read_ids(run_id)
                unread = sorted(set(evidence_ids) - read_evidence)
                if unread:
                    raise ValueError(f"candidate {candidate_id} cites unread evidence: {', '.join(unread)}")
            elif assertion_ids or evidence_ids:
                raise ValueError(f"model candidate {candidate_id} must not cite local entity evidence")
            source_fact_scope = str(raw.get("source_fact_scope") or "none")
            if source_fact_scope not in {"reported_exact", "reported_related", "none"}:
                raise ValueError(f"candidate {candidate_id} has invalid source_fact_scope")
            if kind in _ENTITY_KIND_TYPES and source_fact_scope == "none":
                raise ValueError(f"entity candidate {candidate_id} requires an explicit source_fact_scope")
            if kind not in _ENTITY_KIND_TYPES and source_fact_scope != "none":
                raise ValueError(f"model candidate {candidate_id} cannot claim reported source facts")
            if kind in {"entity_family", "entity_protein_group"} and source_fact_scope != "reported_related":
                raise ValueError(
                    f"candidate {candidate_id} cannot attribute family/group precedent as reported_exact"
                )
            if kind == "entity_protein" and source_fact_scope == "reported_exact" and not any(
                row.get("subject_id") == entity_id
                and row.get("attribution") == "exact_protein"
                and row.get("predicate") in {"supports_capability", "catalyzes_reported"}
                for row in cited_assertions
            ):
                raise ValueError(
                    f"candidate {candidate_id} lacks an exact-protein capability or activity assertion"
                )
            normalized.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_kind": kind,
                    "candidate_role": role,
                    "label": _nonempty_text(raw.get("label"), f"candidates[{index}].label", maximum=240),
                    "entity_id": entity_id or None,
                    "proposal_id": proposal_id or None,
                    "evidence_level": evidence_level,
                    "source_fact_scope": source_fact_scope,
                    "fit_reason": _nonempty_text(raw.get("fit_reason"), f"candidates[{index}].fit_reason"),
                    "supporting_assertion_ids": assertion_ids,
                    "supporting_evidence_ids": evidence_ids,
                    "conflicts": _text_list(raw.get("conflicts"), f"candidates[{index}].conflicts", maximum_items=6),
                    "knowledge_gaps": _text_list(raw.get("knowledge_gaps"), f"candidates[{index}].knowledge_gaps", maximum_items=8),
                    "target_activity_status": "hypothesis_unvalidated",
                }
            )
        normalized_routes = self._normalize_routes(
            run_id,
            routes,
            {row["candidate_id"] for row in normalized},
        )
        route_candidate_ids = {
            candidate_id
            for route in normalized_routes
            for step in route.get("hypothetical_steps") or []
            for candidate_id in step.get("candidate_ids") or []
        }
        group_ranks: dict[str, int] = {}
        for candidate in normalized:
            role = candidate["candidate_role"]
            if not role:
                role = (
                    "unmatched_capability" if candidate["candidate_kind"] == "unmatched_capability"
                    else "family_hint" if candidate["candidate_kind"] in {"entity_family", "entity_protein_group"}
                    else "route_component" if candidate["candidate_id"] in route_candidate_ids
                    else "primary"
                )
            candidate["candidate_role"] = role
            group_ranks[role] = group_ranks.get(role, 0) + 1
            candidate["group_rank"] = group_ranks[role]
        normalized_route_gaps = _text_list(route_gaps, "route_gaps", maximum_items=12)
        if not normalized_routes and not normalized_route_gaps:
            raise ValueError("an empty route set requires explicit route_gaps")
        document = {
            "schema_version": "1.0",
            "run_id": run_id,
            "baseline_candidate_draft": baseline["baseline_candidate_draft"],
            "entity_expanded_candidates": normalized,
            "comparison_notes": _text_list(comparison_notes, "comparison_notes", maximum_items=12),
            "routes": normalized_routes,
            "route_planning_status": (
                "completed_with_hypotheses" if normalized_routes else "no_local_route"
            ),
            "route_gaps": normalized_route_gaps,
            "published_ontology_modified": False,
            "reasoning_protocol": _PRODUCTION_PROTOCOL,
            "knowledge_backend": "ontology_assertion_evidence",
            "ranking_policy": "Chemistry-first model comparison; ontology hits change evidence state only and add no fitness bonus.",
        }
        self._write_json(run_id, "03_candidate_and_routes.json", document)
        self._write_json(run_id, "04_candidates.json", {
            "schema_version": "1.0", "run_id": run_id,
            "candidate_generation": {
                "baseline_frozen_before_knowledge_lookup": True,
                "baseline": document["baseline_candidate_draft"],
            },
            "candidates": document["entity_expanded_candidates"],
            "comparison_notes": document["comparison_notes"],
            "ranking_policy": document["ranking_policy"],
        })
        self._write_json(run_id, "05_routes.json", {
            "schema_version": "1.0", "run_id": run_id,
            "routes": document["routes"], "route_gaps": document["route_gaps"],
            "route_planning_status": document["route_planning_status"],
            "separation_policy": "Multi-enzyme route components are not ranked as direct single-enzyme candidates.",
        })
        run = self._require_run(run_id)
        statuses = dict(run["stage_status"])
        statuses["family_screening_and_routes"] = "complete"
        statuses["critical_report"] = "ready"
        self._update_run(run_id, stage_status=statuses)
        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "committed_artifacts": ["baseline_candidate_draft.json", "03_candidate_and_routes.json", "04_candidates.json", "05_routes.json"],
            "baseline_candidate_count": len(baseline["baseline_candidate_draft"]),
            "entity_expanded_candidate_count": len(normalized),
            "candidate_ids": [row["candidate_id"] for row in normalized],
            "route_count": len(normalized_routes),
            "route_planning_status": document["route_planning_status"],
        }

    def commit_critical_report(
        self,
        run_id: str,
        context_digest: Any,
        review_decision: Any,
        checks: Any,
        limitations: Any,
        next_actions: Any,
        user_summary: Any,
        allow_supersede: bool = False,
        resolved_findings: Any = None,
    ) -> dict[str, Any]:
        # Commits are deliberately idempotent.  A model may repeat the final
        # tool call after compaction or after the host has already recorded the
        # receipt.  In that case an old context digest must not turn a valid
        # report into a user-visible PARTIAL failure.  Only treat a complete
        # pair of report files as replayable; an interrupted write still goes
        # through the normal validation path below.
        existing = self._read_json(run_id, "04_critical_report.json", required=False)
        if existing is not None and (
            str(existing.get("run_id") or "") != run_id
            or str(existing.get("schema_version") or "") != "1.0"
            or not isinstance(existing.get("counts"), Mapping)
        ):
            existing = None
        if existing is not None and not allow_supersede:
            try:
                self.workspace.read_text(
                    self._path(run_id, "04_critical_report.md"), encoding="utf-8"
                )
            except FileNotFoundError:
                existing = None
        if existing is not None and not allow_supersede:
            final_artifacts = self._existing_artifacts(run_id, [
                "01_reaction_analysis.json", "02_mechanism_hypotheses.json",
                "02_mechanisms.json", "03_capabilities.json", "hypotheses.jsonl",
                "baseline_candidate_draft.json", "03_candidate_and_routes.json",
                "04_candidates.json", "05_routes.json", "04_critical_report.json",
                "04_critical_report.md", "06_critique.json", "entity_usage.jsonl",
            ])
            if existing.get("review_decision") == "PASS":
                final_artifacts.extend(self._existing_artifacts(
                    run_id, ["07_final_report.json", "07_final_report.md", "mechanism_routes.mmd"]
                ))
            return {
                "schema_version": "1.0",
                "run_id": run_id,
                "review_decision": str(existing.get("review_decision") or "REVISE"),
                "final_artifacts": final_artifacts,
                "top_summary": dict(existing.get("counts") or {}),
                "user_summary": str(existing.get("user_summary") or ""),
                "replayed": True,
                "private_assertion_ids": list(existing.get("private_assertion_ids") or []),
            }
        if existing is not None and existing.get("review_decision") != "REVISE":
            raise ValueError("only a REVISE critique may be superseded in the same run")
        review_input_digest = self._require_context_digest(run_id, context_digest)
        mechanisms = self._read_json(run_id, "02_mechanism_hypotheses.json")
        candidates = self._read_json(run_id, "03_candidate_and_routes.json")
        assert mechanisms is not None and candidates is not None
        decision = str(review_decision or "")
        if decision not in {"PASS", "REVISE"}:
            raise ValueError("review_decision must be PASS or REVISE")
        required_checks = {
            "cofactor_not_activity",
            "family_attribution",
            "evidence_read",
            "fact_prediction_separation",
            "target_activity_unproven",
            "route_closure_and_auxiliary_steps",
        }
        if not isinstance(checks, list) or len(checks) != len(required_checks):
            raise ValueError("checks must contain the six required review checks")
        normalized_checks = []
        seen: set[str] = set()
        for index, raw in enumerate(checks):
            if not isinstance(raw, Mapping):
                raise ValueError(f"checks[{index}] must be an object")
            check_id = str(raw.get("check_id") or "")
            if check_id not in required_checks or check_id in seen:
                raise ValueError(f"invalid or duplicate check_id: {check_id}")
            seen.add(check_id)
            status = str(raw.get("status") or "")
            if status not in {"pass", "fail", "unknown"}:
                raise ValueError(f"invalid status for check {check_id}")
            normalized_checks.append(
                {
                    "check_id": check_id,
                    "status": status,
                    "finding": _nonempty_text(raw.get("finding"), f"checks[{index}].finding"),
                }
            )
        if seen != required_checks:
            raise ValueError("checks do not cover every required review check")
        if decision == "PASS" and any(row["status"] != "pass" for row in normalized_checks):
            raise ValueError("PASS requires all six checks to pass")

        normalized_resolutions = []
        if allow_supersede:
            if existing is None:
                raise ValueError("allow_supersede requires an existing REVISE critique")
            if existing.get("review_input_digest") != review_input_digest:
                raise ValueError("REVISE critique input changed; start a new run for revised upstream artifacts")
            unresolved = {
                row["check_id"]
                for row in existing.get("checks", [])
                if row.get("status") != "pass"
            }
            if decision != "PASS":
                raise ValueError("a superseding critique must be PASS")
            if not isinstance(resolved_findings, list):
                raise ValueError("resolved_findings must document every failed or unknown REVISE check")
            resolved_ids = set()
            for index, raw in enumerate(resolved_findings):
                if not isinstance(raw, Mapping):
                    raise ValueError(f"resolved_findings[{index}] must be an object")
                check_id = str(raw.get("check_id") or "")
                if check_id not in unresolved or check_id in resolved_ids:
                    raise ValueError(f"invalid or duplicate resolved check_id: {check_id}")
                resolved_ids.add(check_id)
                normalized_resolutions.append({
                    "check_id": check_id,
                    "resolution": _nonempty_text(
                        raw.get("resolution"), f"resolved_findings[{index}].resolution"
                    ),
                })
            if resolved_ids != unresolved:
                raise ValueError("resolved_findings must cover every failed or unknown REVISE check")
            old_findings = {
                row["check_id"]: row.get("finding") for row in existing.get("checks", [])
            }
            new_findings = {row["check_id"]: row["finding"] for row in normalized_checks}
            unchanged = sorted(
                check_id for check_id in unresolved
                if new_findings.get(check_id) == old_findings.get(check_id)
            )
            if unchanged:
                raise ValueError(
                    "superseding PASS must include a revised finding for: " + ", ".join(unchanged)
                )
        elif resolved_findings not in (None, []):
            raise ValueError("resolved_findings is only valid with allow_supersede")

        fact_claims = [
            claim
            for hypothesis in mechanisms["hypotheses"]
            for claim in hypothesis["fact_claims"]
        ]
        entity_candidates = [
            row
            for row in candidates["entity_expanded_candidates"]
            if row["candidate_kind"] in _ENTITY_KIND_TYPES
        ]
        route_evidence = {
            evidence_id
            for route in candidates["routes"]
            for evidence_id in (
                (route.get("reported_path") or {}).get("evidence_ids") or []
            )
        }
        cited_evidence = sorted(
            {
                evidence_id
                for claim in fact_claims
                for evidence_id in claim["evidence_ids"]
            }
            | {
                evidence_id
                for candidate in entity_candidates
                for evidence_id in candidate["supporting_evidence_ids"]
            }
            | route_evidence
        )
        _read_assertions, read_evidence = self.evidence_read_ids(run_id)
        unread = sorted(set(cited_evidence) - read_evidence)
        automated_checks = {
            "all_cited_evidence_read": not unread,
            "unread_evidence_ids": unread,
            "published_ontology_modified": False,
            "all_target_candidates_unvalidated": all(
                row.get("target_activity_status") == "hypothesis_unvalidated"
                for row in candidates["entity_expanded_candidates"]
            ),
            "all_target_routes_unvalidated": all(
                route.get("target_pathway_status") == "hypothesis_unvalidated"
                for route in candidates["routes"]
            ),
            "route_planning_status": candidates["route_planning_status"],
            "route_assessments_complete": all(
                len(route.get("closure_checks") or []) == 7
                for route in candidates["routes"]
            ),
            "route_gaps_explicit": bool(candidates["routes"] or candidates["route_gaps"]),
        }
        if decision == "PASS" and (
            not automated_checks["all_cited_evidence_read"]
            or not automated_checks["all_target_candidates_unvalidated"]
            or not automated_checks["all_target_routes_unvalidated"]
            or not automated_checks["route_assessments_complete"]
            or not automated_checks["route_gaps_explicit"]
        ):
            raise ValueError("PASS conflicts with automated evidence or target-activity checks")
        submitted_summary = _nonempty_text(user_summary, "user_summary", maximum=6000)
        if decision == "PASS":
            _validate_summary_roles(submitted_summary, candidates["entity_expanded_candidates"], self.store)
        run_record = self._require_run(run_id)
        target = dict(run_record.get("target_reaction") or {})
        request = str(target.get("natural_language_request") or target.get("target_description") or "")
        summary = _scientific_user_text(submitted_summary, request=request)
        private_ids = self.private_references(run_id)
        private_sources = [dict(self.store.assertions[key]) for key in private_ids]
        if private_sources:
            summary += '\n\n私有知识依据：本报告使用当前 Workspace 已审核的 %d 条断言；审核不等于目标活性已获验证。' % len(private_sources)
            if any(row.get('conflict_state') == 'open' for row in private_sources):
                summary += '其中存在未解决的冲突，旧事实与不同主张同时保留，不能视为一致结论。'
        document = {
            "schema_version": "1.0",
            "run_id": run_id,
            "review_decision": decision,
            "review_input_digest": review_input_digest,
            "resolved_findings": normalized_resolutions,
            "private_sources": private_sources,
            "checks": normalized_checks,
            "automated_checks": automated_checks,
            "private_assertion_ids": self.private_references(run_id),
            "limitations": _text_list(limitations, "limitations", maximum_items=16),
            "next_actions": _text_list(next_actions, "next_actions", maximum_items=16),
            "user_summary": summary,
            "submitted_user_summary": submitted_summary,
            "technical_details_requested": _technical_details_requested(request),
            "counts": {
                "mechanism_hypotheses": len(mechanisms["hypotheses"]),
                "baseline_candidates": len(candidates["baseline_candidate_draft"]),
                "entity_expanded_candidates": len(candidates["entity_expanded_candidates"]),
                "reported_fact_claims": len(fact_claims),
                "citation_evidence": len(cited_evidence),
                "route_hypotheses": len(candidates["routes"]),
                "auxiliary_steps": sum(
                    len(route["auxiliary_steps"]) for route in candidates["routes"]
                ),
            },
            "reasoning_protocol": _PRODUCTION_PROTOCOL,
            "knowledge_backend": "ontology_assertion_evidence",
        }
        self._write_json(run_id, "04_critical_report.json", document)
        markdown = self._render_critical_markdown(document)
        self.workspace.write_text(
            self._path(run_id, "04_critical_report.md"), markdown, encoding="utf-8"
        )
        self._write_json(run_id, "06_critique.json", {
            "schema_version": "1.0", "run_id": run_id,
            "review_decision": decision, "checks": normalized_checks,
            "automated_checks": automated_checks,
            "limitations": document["limitations"],
        })
        if decision == "PASS":
            self._write_json(run_id, "07_final_report.json", document)
            self.workspace.write_text(self._path(run_id, "07_final_report.md"), markdown, encoding="utf-8")
            self.workspace.write_text(
                self._path(run_id, "mechanism_routes.mmd"),
                "flowchart LR\n  Target[Target reaction]\n  Mechanisms[Mechanism hypotheses]\n  Candidates[Direct enzyme candidates]\n  Routes[Multi-enzyme route alternatives]\n  Target --> Mechanisms --> Candidates\n  Mechanisms --> Routes\n",
                encoding="utf-8",
            )
        run = self._require_run(run_id)
        statuses = dict(run["stage_status"])
        statuses["critical_report"] = "complete"
        self._update_run(run_id, stage_status=statuses, review_decision=decision)
        final_artifacts = [
            "01_reaction_analysis.json", "02_mechanism_hypotheses.json",
            "02_mechanisms.json", "03_capabilities.json", "hypotheses.jsonl",
            "baseline_candidate_draft.json", "03_candidate_and_routes.json",
            "04_candidates.json", "05_routes.json", "04_critical_report.json",
            "04_critical_report.md", "06_critique.json", "entity_usage.jsonl",
        ]
        if decision == "PASS":
            final_artifacts.extend(["07_final_report.json", "07_final_report.md", "mechanism_routes.mmd"])
        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "review_decision": decision,
            "final_artifacts": final_artifacts,
            "top_summary": document["counts"],
            "user_summary": summary,
            "private_assertion_ids": document["private_assertion_ids"],
        }

    @staticmethod
    def _render_critical_markdown(document: Mapping[str, Any]) -> str:
        lines = [
            "# 酶实体发现 Demo 批判报告",
            "",
            f"- Run ID：`{document['run_id']}`",
            f"- 审核结论：**{document['review_decision']}**",
            "- 知识边界：公共实体包、当前 Workspace 已审核私有事实与 Agent 未核实背景知识",
            f"- 路线规划：{document['automated_checks']['route_planning_status']}",
            "",
            "## 面向用户的结论",
            "",
            str(document["user_summary"]),
            "",
            "## 必检项",
            "",
        ]
        for check in document["checks"]:
            lines.append(f"- `{check['check_id']}`：{check['status']} — {check['finding']}")
        lines.extend(["", "## 限制", ""])
        projection_request = "请做技术诊断" if document.get("technical_details_requested") else ""
        lines.extend(f"- {_scientific_user_text(item, request=projection_request)}" for item in document["limitations"])
        lines.extend(["", "## 下一步", ""])
        lines.extend(f"- {item}" for item in document["next_actions"])
        return "\n".join(lines).rstrip() + "\n"
