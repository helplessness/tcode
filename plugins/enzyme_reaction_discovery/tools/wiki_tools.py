"""生成有界 Wiki 证据包；不执行目标依赖的化学推断或排名。"""

from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Mapping
from typing import Any
from .wiki_chunks import CHUNK_VERSION, evidence_fragments, resolve_evidence


_CURRENCY = {"CHEBI:15377", "CHEBI:15378", "CHEBI:30616", "CHEBI:57287",
             "CHEBI:456215", "CHEBI:33019", "CHEBI:16810", "CHEBI:29985"}


def _native_reaction_index(library: Any) -> dict[str, dict[str, Any]]:
    participants = {}
    for entry in library.manifest()["cards"]:
        card = library.card(entry["card_id"], detail_level="full")
        # 旧版兼容视图未必含 Markdown；缺少结构化参与物时不生成通路线索。
        native = next((r.get("markdown", "").split("## 辅因子与机理", 1)[0]
                       for r in card["wiki"]["known_reactions"] if r.get("activity_type") == "native"), "")
        rows = re.findall(r"^\|\s*(左侧|右侧)\s*\|\s*([^|]+?)\s*\|\s*(CHEBI:\d+)\s*\|", native, re.M)
        native_ref = next((f"{entry['card_id']}:{path}" for path, fragment in evidence_fragments(card)
                           if fragment.get("name") == "催化的反应"), "")
        participants[entry["card_id"]] = {
            "name": card["identity"]["display_name"], "rows": rows, "native_ref": native_ref,
        }
    return participants


def native_reaction_neighbors(library: Any, card_ids: list[str]) -> list[dict[str, str]]:
    """按共享 ChEBI 参与物生成相邻反应线索；线索本身不是通路证据。"""
    if not card_ids:
        return []
    participants = _native_reaction_index(library)
    hints = []
    for source_id in dict.fromkeys(card_ids):
        if source_id not in participants:
            continue
        for source_side, name, chebi in participants[source_id]["rows"]:
            if chebi in _CURRENCY:
                continue
            for neighbor_id, neighbor in participants.items():
                if neighbor_id == source_id:
                    continue
                for neighbor_side, _, neighbor_chebi in neighbor["rows"]:
                    if chebi == neighbor_chebi:
                        hints.append({"read_card_id": source_id, "neighbor_card_id": neighbor_id,
                                      "neighbor_name": neighbor["name"], "shared_participant": name,
                                      "chebi": chebi, "read_side": source_side,
                                      "neighbor_side": neighbor_side,
                                      "read_ref": neighbor["native_ref"],
                                      "status": "unread_search_lead_direction_unverified"})
    return hints[:6]


def native_pathway_scaffolds(library: Any, seed_card_ids: list[str], max_steps: int) -> list[dict[str, Any]]:
    """按代谢物身份和反应侧连续性连接已检索的天然反应。

    该图不包含目标化学、预设通路名称或推断活性；返回的 ref 必须回读原文后才能引用。
    """
    index = _native_reaction_index(library)
    seeds = list(dict.fromkeys(cid for cid in seed_card_ids if cid in index))[:12]
    if len(seeds) < 2:
        return []
    edges: dict[str, list[tuple[str, str, str, str, str]]] = {cid: [] for cid in index}
    for source_id, source in index.items():
        for side, name, chebi in source["rows"]:
            if chebi in _CURRENCY:
                continue
            for neighbor_id, neighbor in index.items():
                if neighbor_id == source_id:
                    continue
                for neighbor_side, _, neighbor_chebi in neighbor["rows"]:
                    if chebi == neighbor_chebi:
                        edges[source_id].append((neighbor_id, side, neighbor_side, chebi, name.strip()))
    paths = []
    seen = set()
    seed_set = set(seeds)
    for start in seeds:
        queue = [([start], [])]
        explored = 0
        # ponytail: bounded scan suits the small curated Wiki; index edges if it grows substantially.
        while queue and explored < 200:
            cards, links = queue.pop(0)
            explored += 1
            if len(cards) >= min(max_steps, 8):
                continue
            for neighbor_id, exit_side, entry_side, chebi, name in edges[cards[-1]]:
                if neighbor_id in cards or (links and links[-1][2] == exit_side):
                    continue
                next_cards = [*cards, neighbor_id]
                next_links = [*links, (exit_side, entry_side, chebi, name)]
                if neighbor_id in seed_set and len(next_cards) >= 3:
                    key = min(tuple(next_cards), tuple(reversed(next_cards)))
                    if key not in seen:
                        seen.add(key)
                        paths.append((next_cards, next_links))
                elif len(next_cards) < min(max_steps, 8):
                    queue.append((next_cards, next_links))
    paths.sort(key=lambda item: (len(item[0]), item[0]))
    return [{
        "card_ids": cards,
        "read_refs": [index[cid]["native_ref"] for cid in cards],
        "joins": [{"shared_participant": name, "chebi": chebi,
                   "from_side": left, "to_side": right}
                  for left, right, chebi, name in links],
        "status": "unread_native_reaction_lead_target_substrates_unverified",
    } for cards, links in paths[:3] if all(index[cid]["native_ref"] for cid in cards)]


def wiki_packet(library: Any, run: Mapping[str, Any] | None = None, *, refs=()) -> dict[str, Any]:
    """将指定 Wiki ref 组装为 Workflow 可渐进阅读的有界证据包。"""
    manifest = library.manifest()
    fingerprint = library.fingerprint()
    if run and run.get("knowledge_fingerprint") != fingerprint:
        raise ValueError("Wiki changed or this is a legacy run; initialize a fresh run")
    cards = []
    selected = {}
    for ref in refs:
        match = re.fullmatch(r"(ERD-CARD-[0-9]{3,}):(wiki\.(?:known_reactions\.[0-9]+|fragments\.v[0-9]+_[a-f0-9]{24}_[0-9]+))", ref)
        if not match:
            raise ValueError(f"invalid Wiki claim ref: {ref}")
        selected.setdefault(match[1], set()).add(match[2])
    for card_id in sorted(selected):
        card = library.card(card_id, detail_level="full")
        card_id = card["card_id"]
        identity_level = card["identity"].get("identity_level", "named_enzyme" if card["identity"]["enzyme_id"].startswith("EC:") else "protein")
        claims = []
        for path in sorted(selected[card_id]):
            reaction = resolve_evidence(card, path)
            sources = [{key: value for key, value in e.items() if key != "supports"}
                       for e in card["evidence"] if e["evidence_id"] in reaction["evidence_refs"]]
            claims.append({
                "ref": f"{card_id}:{path}", "citation_id": citation_id(f"{card_id}:{path}"),
                "field_path": path, "role": "evidence",
                "statement": (reaction["attribution"] + "\n\n" + reaction["markdown"]
                              + "\n\n## 适用范围与限制\n\n" + reaction["scope"])
                if "markdown" in reaction else json.dumps({k: reaction[k] for k in (
                    "name", "substrates", "products", "cofactors", "mechanistic_role",
                    "mechanistic_events", "key_intermediates", "scope",
                )}, ensure_ascii=False, separators=(",", ":")),
                "activity_status": reaction.get("activity_status", "curator_annotation_unverified"),
                "mechanism_status": reaction.get("mechanism_status", "curator_annotation_unverified"),
                "attribution": reaction.get("attribution", "按原始资料核对"),
                "source_scope": reaction.get("source_scope", "legacy_parent_section"),
                "source_locator": reaction.get("source_locator", {}),
                "identity_level": identity_level,
                "source_enzyme": card["identity"]["enzyme_id"],
                "wiki_protein": card["identity"]["enzyme_id"] if identity_level == "protein" else "",
                "source_protein": card["identity"]["enzyme_id"] if reaction.get("activity_status") == "curated_protein_annotation" else "see_original_attribution",
                "sources": sources,
            })
        if len(claims) != len(selected[card_id]):
            raise ValueError(f"unknown Wiki reaction index: {card_id}")
        cards.append({
            "card_id": card_id, "display_name": card["identity"]["display_name"],
            "identity_level": identity_level,
            "enzyme_family": card["identity"]["enzyme_family"],
            "enzyme_id": card["identity"]["enzyme_id"],
            "organism": card["identity"].get("organism", ""),
            "source_path": card.get("source_path", ""),
            "ec_number": card["identity"]["ec_class"],
            "cofactor_classes": list(card["classification"]["cofactor_classes"]),
            "cofactor_notes": card["classification"]["cofactor_notes"],
            # Always expose the native reaction beside a retrieved mechanism
            # fragment. A mechanism hit alone must not turn a different
            # substrate into a reported substrate of this protein.
            "native_reaction": next((
                reaction["markdown"].split("## 辅因子与机理", 1)[0].strip()
                for reaction in card["wiki"]["known_reactions"]
                if reaction.get("activity_type") == "native"
            ), ""),
            "claims": claims,
        })
    return {"knowledge_version": manifest["knowledge_version"], "knowledge_fingerprint": fingerprint,
            "evidence_format": CHUNK_VERSION,
            "cards": cards, "card_count": len(cards), "complete_library": False,
            "library_card_count": len(manifest["cards"]),
            "order_is_not_rank": True,
            "scope": "一篇 Wiki 对应一个具体 UniProt 蛋白；EC 是反应类别，不是蛋白身份。同类蛋白先例不等于本蛋白实测活性。机理说明的证据强度以正文和来源为准；迁移到新反应属于假说，未报道不是反证。辅因子的结合、催化与调节角色须结合正文判断。"}


def claim_index(packet: Mapping[str, Any]) -> dict[str, Any]:
    """按稳定片段 ref 建立证据快速查找表。"""
    return {c["ref"]: c for card in packet["cards"] for c in card["claims"]}


def citation_id(ref: str) -> str:
    """短引用只负责传输；稳定绑定完整片段 ID，不依赖排名或分页顺序。"""
    return "W" + hashlib.sha256(ref.encode()).hexdigest()[:10]


def citation_index(packet: Mapping[str, Any]) -> dict[str, str]:
    """只为已授权片段生成短编号；发生碰撞则拒绝，绝不猜测原文。"""
    return reference_index(claim_index(packet))


def reference_index(refs, prefix="W"):
    """读取句柄 R 与引用句柄 W 分开，避免把检索命中误当成已读证据。"""
    result = {}
    for ref in refs:
        short = prefix + citation_id(ref)[1:]
        if short in result and result[short] != ref:
            raise ValueError("Wiki citation ID collision; use canonical references")
        result[short] = ref
    return result


def require_wiki_refs(refs: Any, claims: Mapping[str, Any]) -> list[str]:
    """校验模型提交的 Wiki ref 全部存在于已授权证据中。"""
    if not isinstance(refs, (list, tuple)) or not 1 <= len(refs) <= 6:
        raise ValueError("wiki_refs requires 1-6 exact Wiki claim refs")
    aliases = reference_index(claims)
    resolved = []
    for ref in refs:
        canonical = aliases.get(ref, ref) if isinstance(ref, str) else ref
        if not isinstance(canonical, str) or canonical not in claims:
            raise ValueError(f"unknown Wiki claim ref: {ref}")
        resolved.append(canonical)
    return list(dict.fromkeys(resolved))
