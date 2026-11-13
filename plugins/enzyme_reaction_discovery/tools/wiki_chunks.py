"""从普通 Wiki 派生可引用片段；不生成化学事件、机制或活性评分。"""
from __future__ import annotations

import hashlib
import json
import re

CHUNK_VERSION = "v1"
TARGET_CHARS = 1200


def evidence_fragments(card):
    """Legacy views remain readable; new Markdown searches use content-addressed refs.

    A paragraph/list item is indivisible. Oversized units fail publication rather
    than silently truncating a reaction or its attribution. Every chunk inherits
    the full scope and source boundary of its parent section.
    """
    for index, reaction in enumerate(card["wiki"]["known_reactions"]):
        if "markdown" not in reaction:
            yield f"wiki.known_reactions.{index}", reaction
            continue
        sections = re.split(r"(?=^#{2,3} )", reaction["markdown"], flags=re.M)
        duplicates = {}
        for section in filter(str.strip, sections):
            heading, _, body = section.strip().partition("\n")
            units = re.split(r"\n\s*\n", body.strip())
            blocks, block = [], []
            for unit in units:
                if block and len("\n\n".join([*block, unit])) > TARGET_CHARS:
                    blocks.append("\n\n".join(block))
                    block = []
                block.append(unit)
            if block:
                blocks.append("\n\n".join(block))
            for text in blocks:
                markdown = heading + "\n\n" + text
                # Inline sources are preferred. A source-less subsection carries
                # parent sources explicitly as context, not as direct proof.
                urls = set(re.findall(r"\]\((https://[^\s)]+)\)", markdown))
                direct = [s["evidence_id"] for s in card["evidence"] if s["url"] in urls]
                inherited = reaction.get("evidence_refs", [])
                attribution = reaction["attribution"]
                activity = reaction["activity_status"]
                # Do not guess that variants mentioned in an otherwise native
                # section are this exact protein. The full prose remains visible.
                if activity != "related_protein_only" and re.search(r"变体|突变体|工程化|mutant|variant", text, re.I):
                    activity = "attribution_requires_review"
                    attribution += "；本段涉及变体或工程化，活性归属须按正文核对"
                source_scope = "inline_section_sources" if direct else "inherited_parent_sources_not_direct_proof"
                content = {"parent": reaction["reaction_id"], "heading": heading,
                           "markdown": markdown, "scope": reaction["scope"],
                           "attribution": attribution, "sources": direct or inherited,
                           "source_scope": source_scope}
                digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
                occurrence = duplicates.get(digest, 0)
                duplicates[digest] = occurrence + 1
                chunk_id = f"{CHUNK_VERSION}_{digest}_{occurrence}"
                fragment = {**reaction, "reaction_id": chunk_id, "markdown": markdown,
                            "name": heading.lstrip("# "), "mechanistic_role": text,
                            "attribution": attribution, "activity_status": activity,
                            "evidence_refs": direct or inherited, "source_scope": source_scope,
                            "parent_reaction_id": reaction["reaction_id"],
                            "source_locator": {"path": card.get("source_path", ""), "heading": heading,
                                               "content_sha256": hashlib.sha256(markdown.encode()).hexdigest()},
                            "fragment_version": CHUNK_VERSION}
                # Includes identity/scope/source overhead; don't allow one huge
                # unbreakable paragraph to poison all document embeddings.
                if len(json.dumps(fragment, ensure_ascii=False)) > 10000:
                    raise ValueError(f"Wiki fragment exceeds budget: {card['card_id']} {heading}; split the paragraph")
                yield f"wiki.fragments.{chunk_id}", fragment


def resolve_evidence(card, field):
    # Explicit legacy references are never remapped by array position to chunks.
    match = re.fullmatch(r"wiki\.known_reactions\.([0-9]+)", field)
    if match:
        index = int(match[1])
        rows = card["wiki"]["known_reactions"]
        if index < len(rows):
            return rows[index]
    else:
        for path, fragment in evidence_fragments(card):
            if path == field:
                return fragment
    raise ValueError(f"unknown or stale Wiki fragment: {card['card_id']}:{field}")
