"""P1 Skill 混合路由器：确定性规则、BM25、向量和领域软信号。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from .index import SkillVectorIndex, cosine_similarity
from .routing_config import load_skill_routing_config
from .routing_profile import SkillRoutingProfile
from .trace import SkillRoutingTraceStore, query_fingerprint, utc_now_iso


ROUTER_VERSION = "p1-hybrid-v1"
_ASCII_TERM_RE = re.compile(r"^[a-z0-9_+.-]+$", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9_+.-]{2,}|[\u4e00-\u9fff]+", re.IGNORECASE)


@dataclass
class SkillRouteResult:
    decision: str
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    primary_skill: str = ""
    confidence: float = 0.0
    margin: float = 0.0
    trace: dict[str, Any] = field(default_factory=dict)


def _tokenize(text: str, stop_terms: set[str]) -> list[str]:
    result: list[str] = []
    for match in _TOKEN_RE.finditer(str(text or "").lower()):
        token = match.group(0)
        if token and "\u4e00" <= token[0] <= "\u9fff":
            pieces = [token]
            pieces.extend(token[index : index + 2] for index in range(max(0, len(token) - 1)))
        else:
            pieces = [token]
        for piece in pieces:
            if piece and piece not in stop_terms:
                result.append(piece)
    return result


def _phrase_matches(query_lower: str, phrase: str) -> bool:
    normalized = str(phrase or "").strip().lower()
    if not normalized:
        return False
    if _ASCII_TERM_RE.fullmatch(normalized):
        return re.search(rf"(?<![a-z0-9_]){re.escape(normalized)}(?![a-z0-9_])", query_lower) is not None
    return normalized in query_lower


def _bm25_scores(query_tokens: list[str], documents: list[list[str]]) -> list[float]:
    if not query_tokens or not documents:
        return [0.0] * len(documents)
    total_documents = len(documents)
    doc_frequency: Counter[str] = Counter()
    for document in documents:
        doc_frequency.update(set(document))
    average_length = sum(len(document) for document in documents) / max(1, total_documents)
    average_length = average_length or 1.0
    query_terms = set(query_tokens)
    k1 = 1.5
    b = 0.75
    raw_scores: list[float] = []
    for document in documents:
        frequencies = Counter(document)
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            document_frequency = doc_frequency.get(term, 0)
            idf = math.log(1.0 + (total_documents - document_frequency + 0.5) / (document_frequency + 0.5))
            denominator = frequency + k1 * (1.0 - b + b * len(document) / average_length)
            score += idf * (frequency * (k1 + 1.0)) / max(denominator, 1e-9)
        raw_scores.append(score)
    maximum = max(raw_scores, default=0.0)
    if maximum <= 0:
        return [0.0] * len(raw_scores)
    return [score / maximum for score in raw_scores]


def _configured_domain_terms(config: dict[str, Any]) -> dict[str, list[str]]:
    domains = config.get("domains", {}) if isinstance(config.get("domains"), dict) else {}
    result: dict[str, list[str]] = {}
    for name, raw in domains.items():
        terms = [str(name)]
        if isinstance(raw, dict):
            aliases = raw.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            terms.extend(str(item) for item in aliases or [])
        elif isinstance(raw, list):
            terms.extend(str(item) for item in raw)
        result[str(name)] = [term.strip() for term in terms if term.strip()]
    return result


def _domain_scores(
    query_lower: str,
    query_tokens: set[str],
    profiles: list[SkillRoutingProfile],
    config: dict[str, Any],
    stop_terms: set[str],
) -> list[float]:
    configured = _configured_domain_terms(config)
    aggregated: dict[str, list[str]] = {key: list(value) for key, value in configured.items()}
    for profile in profiles:
        for domain in profile.domains:
            bucket = aggregated.setdefault(domain, [domain])
            bucket.extend(profile.intents)

    domain_values: dict[str, float] = {}
    for domain, terms in aggregated.items():
        phrase_hit = any(_phrase_matches(query_lower, term) for term in terms)
        term_tokens = set(_tokenize(" ".join(terms), stop_terms))
        overlap = len(query_tokens & term_tokens) / max(1, len(query_tokens))
        domain_values[domain] = min(1.0, max(0.7 if phrase_hit else 0.0, overlap))

    return [
        max((domain_values.get(domain, 0.0) for domain in profile.domains), default=0.0)
        for profile in profiles
    ]


def _negative_score(
    query_lower: str,
    query_tokens: set[str],
    profile: SkillRoutingProfile,
    stop_terms: set[str],
) -> tuple[float, list[str]]:
    hits: list[str] = []
    strongest = 0.0
    for phrase in [*profile.negative_examples, *profile.excludes]:
        if _phrase_matches(query_lower, phrase):
            hits.append(phrase)
            strongest = 1.0
            continue
        phrase_tokens = set(_tokenize(phrase, stop_terms))
        if not phrase_tokens:
            continue
        overlap = len(query_tokens & phrase_tokens) / max(1, len(phrase_tokens))
        if overlap >= 0.7:
            hits.append(phrase)
            strongest = max(strongest, overlap)
    return strongest, hits[:4]


def _rule_score(query_lower: str, profile: SkillRoutingProfile) -> tuple[float, list[str]]:
    hits: list[str] = []
    if _phrase_matches(query_lower, profile.skill.name):
        hits.append(profile.skill.name)
        return 1.0, hits
    for alias in profile.aliases:
        if alias == profile.skill.name:
            continue
        if _phrase_matches(query_lower, alias):
            hits.append(alias)
    for explicit in (profile.target_server, profile.target_tool):
        if explicit and _phrase_matches(query_lower, explicit):
            hits.append(explicit)
    if not hits:
        return 0.0, []
    longest = max(len(hit) for hit in hits)
    return min(1.0, 0.82 + min(longest, 18) / 100.0), list(dict.fromkeys(hits))[:4]


def _reason(component_scores: dict[str, float], hits: list[str], negative_hits: list[str]) -> str:
    reasons: list[str] = []
    if hits:
        reasons.append(f"明确线索：{', '.join(hits)}")
    ranked = sorted(component_scores.items(), key=lambda item: item[1], reverse=True)
    labels = {"rule": "规则", "bm25": "关键词", "vector": "语义", "domain": "领域"}
    for key, value in ranked:
        if value > 0.05 and not (key == "rule" and hits):
            reasons.append(f"{labels.get(key, key)}相关度 {value:.2f}")
        if len(reasons) >= 2:
            break
    if negative_hits:
        reasons.append(f"存在排除线索：{', '.join(negative_hits)}")
    return "；".join(reasons) or "与当前任务弱相关"


class SkillRouter:
    @classmethod
    def route(
        cls,
        *,
        query: str,
        skills: list[Any],
        agent_name: str = "",
        recall_texts: Optional[list[str]] = None,
        limit: Optional[int] = None,
        workdir: Optional[str] = None,
        catalog_snapshot: Optional[dict[str, Any]] = None,
        use_vector: bool = True,
    ) -> SkillRouteResult:
        config = load_skill_routing_config(workdir)
        routing_cfg = config.get("routing", {}) if isinstance(config.get("routing"), dict) else {}
        stop_terms = set(str(item).lower() for item in routing_cfg.get("stop_terms", []) or [])
        query_text = str(query or "").strip()
        if bool(routing_cfg.get("include_recall_context", False)):
            recall = [str(item or "").strip() for item in (recall_texts or [])[:2] if str(item or "").strip()]
            if recall:
                query_text = "\n".join([query_text, *recall])

        snapshot = catalog_snapshot or {"snapshot_id": "unknown", "skill_count": len(skills)}
        base_trace = {
            "request_id": str(uuid.uuid4()),
            "router_version": ROUTER_VERSION,
            "routing_mode": str(routing_cfg.get("mode") or "active"),
            "catalog_snapshot_id": str(snapshot.get("snapshot_id") or "unknown"),
            "agent_name": str(agent_name or ""),
            "query_hash": query_fingerprint(query),
            "created_at": utc_now_iso(),
        }
        trace_cfg = config.get("trace", {}) if isinstance(config.get("trace"), dict) else {}
        if bool(trace_cfg.get("include_query", False)):
            base_trace["query"] = str(query or "")

        if not query_text:
            base_trace.update({"decision": "no-skill", "candidates": [], "reason": "empty-query"})
            SkillRoutingTraceStore.append_route_trace(base_trace, workdir=workdir, config=config)
            return SkillRouteResult(decision="no-skill", trace=base_trace)

        profiles = [
            profile
            for profile in (SkillRoutingProfile.from_skill(skill) for skill in skills)
            if profile.enabled and profile.applies_to_agent(agent_name)
        ]
        if not profiles:
            base_trace.update({"decision": "no-skill", "candidates": [], "reason": "empty-catalog"})
            SkillRoutingTraceStore.append_route_trace(base_trace, workdir=workdir, config=config)
            return SkillRouteResult(decision="no-skill", trace=base_trace)

        query_lower = query_text.lower()
        query_token_list = _tokenize(query_text, stop_terms)
        query_tokens = set(query_token_list)
        documents = [_tokenize(profile.search_text(), stop_terms) for profile in profiles]
        component_states = {key: 'available' for key in ('rule', 'bm25', 'domain', 'vector')}
        try:
            bm25_values = _bm25_scores(query_token_list, documents)
        except Exception:
            bm25_values = [0.0] * len(profiles)
            component_states['bm25'] = 'unavailable'
        try:
            domain_values = _domain_scores(query_lower, query_tokens, profiles, config, stop_terms)
        except Exception:
            domain_values = [0.0] * len(profiles)
            component_states['domain'] = 'unavailable'
        skill_vectors, query_vector = {}, []
        vector_metadata = {'enabled': False, 'backend': 'skipped', 'reason': 'latency_budget'}
        if use_vector:
            try:
                skill_vectors, vector_metadata = SkillVectorIndex.build(profiles, workdir=workdir, config=config)
                query_vector = SkillVectorIndex.embed_query(query_text, workdir=workdir, config=config, metadata=vector_metadata)
            except Exception as exc:
                vector_metadata = {'enabled': False, 'backend': 'unavailable', 'reason': type(exc).__name__}
        if not query_vector:
            component_states['vector'] = 'unavailable'

        weights = routing_cfg.get("weights", {})
        penalty_weight = float(routing_cfg.get("negative_penalty_weight", 0.25) or 0.25)
        priority_weight = float(routing_cfg.get("priority_weight", 0.03) or 0.03)
        scored: list[dict[str, Any]] = []
        for index, profile in enumerate(profiles):
            states = dict(component_states)
            try:
                rule_value, rule_hits = _rule_score(query_lower, profile)
            except Exception:
                rule_value, rule_hits = 0.0, []
                states['rule'] = 'unavailable'
            skill_key = profile.skill.skill_id or profile.skill.name
            try:
                vector_value = max(
                    (max(0.0, cosine_similarity(query_vector, vector)) for vector in skill_vectors.get(skill_key, [])),
                    default=0.0,
                )
            except Exception:
                vector_value = 0.0
                states['vector'] = 'unavailable'
            negative_value, negative_hits = _negative_score(query_lower, query_tokens, profile, stop_terms)
            components = {
                "rule": rule_value,
                "bm25": bm25_values[index],
                "vector": vector_value,
                "domain": domain_values[index],
            }
            raw_score = sum(float(weights.get(key, 0.0)) * value for key, value in components.items())
            raw_score += priority_weight * (profile.priority / 100.0)
            raw_score -= penalty_weight * negative_value
            score = max(0.0, min(1.0, raw_score))
            document_tokens = set(documents[index])
            overlap_tokens = sorted(query_tokens & document_tokens, key=lambda item: (-len(item), item))[:6]
            public = profile.to_public_dict()
            scored.append(
                {
                    "name": profile.skill.name,
                    "skill_id": skill_key,
                    "description": profile.skill.description,
                    "score": round(score, 6),
                    "confidence": round(score, 6),
                    "component_scores": {key: round(value, 6) if states[key] == 'available' else None for key, value in components.items()},
                    "component_states": states,
                    "scoring_complete": all(state == 'available' for state in states.values()),
                    "negative_score": round(negative_value, 6),
                    "negative_hits": negative_hits,
                    "rule_hits": rule_hits,
                    "overlap_tokens": overlap_tokens,
                    "reason": _reason(components, rule_hits, negative_hits),
                    "source": profile.skill.source,
                    "plugin": profile.skill.plugin,
                    **public,
                }
            )

        scored.sort(
            key=lambda item: (
                -float(item["score"]),
                -float(item["component_scores"]["rule"] or 0),
                str(item["name"]),
            )
        )
        candidate_limit = int(routing_cfg.get("candidate_limit", 6) or 6)
        candidates = scored[:candidate_limit]
        top_score = float(candidates[0]["score"]) if candidates else 0.0
        second_score = float(candidates[1]["score"]) if len(candidates) > 1 else 0.0
        margin = max(0.0, top_score - second_score)
        selection_min = float(routing_cfg.get("selection_min_score", 0.32) or 0.32)
        selection_margin = float(routing_cfg.get("selection_min_margin", 0.06) or 0.06)

        if not candidates or top_score < selection_min:
            decision = "no-skill"
            primary_skill = ""
        elif margin < selection_margin and float(candidates[0]["component_scores"]["rule"] or 0) < 0.99:
            decision = "clarify"
            primary_skill = ""
        else:
            decision = "single"
            primary_skill = str(candidates[0]["name"])

        recommendation_min = float(routing_cfg.get("recommendation_min_score", 0.16) or 0.16)
        recommendation_limit = int(limit or routing_cfg.get("recommendation_limit", 3) or 3)
        recommendations: list[dict[str, Any]] = []
        if decision != "no-skill":
            for item in candidates:
                if len(recommendations) >= recommendation_limit:
                    break
                if float(item["score"]) < recommendation_min:
                    continue
                copied = dict(item)
                copied["route_decision"] = decision
                copied["selected"] = decision == "single" and copied["name"] == primary_skill
                copied["route_margin"] = round(margin, 6)
                auto_threshold = float(routing_cfg.get("auto_load_min_score", 0.58) or 0.58)
                copied["auto_load_eligible"] = bool(
                    copied["selected"]
                    and float(copied["score"]) >= auto_threshold
                    and (margin >= selection_margin or float(copied["component_scores"]["rule"] or 0) >= 0.99)
                )
                recommendations.append(copied)

        max_trace_candidates = max(1, int(trace_cfg.get("max_candidates", 10) or 10))
        trace_candidates = [
            {
                key: item.get(key)
                for key in (
                    "name",
                    "skill_id",
                    "score",
                    "component_scores",
                    "component_states",
                    "scoring_complete",
                    "negative_score",
                    "rule_hits",
                    "negative_hits",
                    "source",
                    "plugin",
                )
            }
            for item in candidates[:max_trace_candidates]
        ]
        base_trace.update(
            {
                "decision": decision,
                "primary_skill": primary_skill or None,
                "confidence": round(top_score, 6),
                "margin": round(margin, 6),
                "candidate_count": len(profiles),
                "candidates": trace_candidates,
                "vector_index": vector_metadata,
                "config_fingerprint": hashlib.sha256(
                    json.dumps(config, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
                ).hexdigest()[:16],
            }
        )
        trace_path = SkillRoutingTraceStore.append_route_trace(base_trace, workdir=workdir, config=config)
        if trace_path:
            base_trace["trace_path"] = trace_path
        return SkillRouteResult(
            decision=decision,
            recommendations=recommendations,
            candidates=candidates,
            primary_skill=primary_skill,
            confidence=top_score,
            margin=margin,
            trace=base_trace,
        )
