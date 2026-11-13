"""Closed, manifest-backed knowledge-card access and deterministic search."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from .markdown_wiki import parse_wiki


_CARD_ID = re.compile(r"^ERD-CARD-[0-9]{3,}$")


def normalize_term(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "_", text).strip("_")


def _rag_tokens(value: Any) -> list[str]:
    """Tokenize Wiki prose and enzyme annotations for deterministic sparse RAG."""

    if isinstance(value, Mapping):
        values = value.values()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = value
    else:
        values = [value]
    tokens: list[str] = []
    for item in values:
        if isinstance(item, (Mapping, list, tuple)):
            tokens.extend(_rag_tokens(item))
            continue
        normalized = unicodedata.normalize("NFKC", str(item or "")).casefold()
        words = re.findall(r"[a-z0-9]+", normalized.replace("_", " "))
        tokens.extend(word for word in words if len(word) > 1 or word.isdigit())
        for chunk in re.findall(r"[\u3400-\u9fff]+", normalized):
            if len(chunk) == 1:
                tokens.append(chunk)
                continue
            tokens.extend(chunk[index:index + 2] for index in range(len(chunk) - 1))
            if len(chunk) <= 8:
                tokens.append(chunk)
        phrase = normalize_term(item)
        if phrase and "_" in phrase:
            tokens.append(phrase)
    return tokens


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_relative_path(value: Any, *, prefix: str) -> str:
    path = Path(str(value or ""))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"knowledge manifest path escapes library: {value}")
    normalized = path.as_posix()
    if not normalized.startswith(prefix + "/"):
        raise ValueError(f"knowledge manifest path must stay under {prefix}/: {value}")
    return normalized


class KnowledgeLibrary:
    """Read-only knowledge library whose paths are fixed by a checksummed manifest."""

    def __init__(self, workspace: Any, plugin_root: str | Path) -> None:
        self._workspace = workspace
        self._plugin_root = Path(plugin_root).resolve()
        self._knowledge_root = self._plugin_root / "knowledge"
        self._schema_path = self._plugin_root / "schemas" / "knowledge_card.schema.json"
        self._manifest: dict[str, Any] | None = None
        self._cards: dict[str, dict[str, Any]] | None = None

    def _read(self, path: Path) -> str:
        return self._workspace.read_text(str(path), encoding="utf-8")

    def _load(self) -> None:
        # Manifest changes are noticed even by a long-lived plugin worker.
        path = self._knowledge_root / "manifest.json"
        stat = Path(self._workspace.resolve_read_path(str(path))).stat()
        stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        if getattr(self, "_manifest_stamp", None) == stamp:
            return
        raw = self._read(path)
        signature = _sha256_text(raw)
        if getattr(self, "_manifest_signature", None) == signature:
            self._manifest_stamp = stamp
            return
        manifest = json.loads(raw)
        if manifest.get("schema_version") != "1.0":
            raise ValueError("unsupported knowledge manifest schema_version")
        if not manifest.get("policy", {}).get("offline_only"):
            raise ValueError("knowledge manifest must declare offline_only")
        if not manifest.get("policy", {}).get("hidden_gold_excluded"):
            raise ValueError("knowledge manifest must exclude hidden gold")
        entries = {}
        for entry in manifest.get("cards") or []:
            card_id = str(entry.get("card_id") or "")
            if not _CARD_ID.fullmatch(card_id) or card_id in entries:
                raise ValueError(f"invalid or duplicate knowledge card: {card_id}")
            _safe_relative_path(entry.get("path"), prefix="cards")
            if Path(entry["path"]).suffix != ".md":
                raise ValueError("production Wiki must use Markdown, not legacy JSON cards")
            entries[card_id] = entry
        if not entries:
            raise ValueError("knowledge library must contain at least one enzyme Wiki card")
        self._validator = Draft202012Validator(json.loads(self._read(self._schema_path)))
        self._manifest, self._entries = manifest, entries
        self._cards = OrderedDict()
        self._manifest_signature = signature
        self._manifest_stamp = stamp

    def _document(self, card_id: str) -> dict[str, Any]:
        self._load()
        if card_id not in self._entries:
            raise KeyError(f"knowledge card not found: {card_id}")
        entry = self._entries[card_id]
        path = self._knowledge_root / entry["path"]
        # Resolve through the SDK before stat/read; cache only a bounded working set.
        resolved = Path(self._workspace.resolve_read_path(str(path)))
        stat = resolved.stat()
        stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        cached = self._cards.get(card_id)
        if cached and cached[0] == stamp:
            self._cards.move_to_end(card_id)
            return cached[1]
        text = self._read(path)
        if _sha256_text(text) != entry["sha256"]:
            raise ValueError(f"knowledge card checksum mismatch: {card_id}")
        document = parse_wiki(text, entry)
        errors = list(self._validator.iter_errors(document))
        if errors:
            raise ValueError(f"knowledge card schema invalid: {card_id}: {errors[0].message}")
        if document["card_id"] != card_id or document["card_version"] != entry["card_version"]:
            raise ValueError(f"knowledge card identity/version mismatch: {card_id}")
        identity = document["identity"]
        if (identity["identity_level"], identity["enzyme_id"].split(":", 1)[0]) not in {("protein", "UniProt"), ("named_enzyme", "EC")}:
            raise ValueError(f"Wiki identity must distinguish protein and EC enzyme activity: {card_id}")
        evidence_ids = {e["evidence_id"] for e in document["evidence"]}
        if len(evidence_ids) != len(document["evidence"]):
            raise ValueError(f"duplicate evidence id: {card_id}")
        for reaction in document["wiki"]["known_reactions"]:
            if not set(reaction["evidence_refs"]) <= evidence_ids:
                raise ValueError(f"unknown reaction evidence reference: {card_id}")
        self._cards[card_id] = (stamp, document)
        self._cards.move_to_end(card_id)
        while len(self._cards) > 32:
            self._cards.popitem(last=False)
        return document

    def iter_cards(self):
        self._load()
        # Freeze IDs; a changed manifest is rejected by the index builder.
        enzyme_ids = set()
        for card_id in tuple(self._entries):
            document = self._document(card_id)
            enzyme_id = document["identity"]["enzyme_id"]
            if enzyme_id in enzyme_ids:
                raise ValueError(f"duplicate enzyme identity: {enzyme_id}")
            enzyme_ids.add(enzyme_id)
            yield document

    def validate(self) -> dict[str, Any]:
        count = sum(1 for _ in self.iter_cards())
        return {"valid": True, "knowledge_version": self.manifest()["knowledge_version"],
                "card_count": count, "ontology_count": 0}

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.manifest(), sort_keys=True).encode()).hexdigest()

    def manifest(self) -> dict[str, Any]:
        self._load()
        assert self._manifest is not None
        return json.loads(json.dumps(self._manifest))

    @staticmethod
    def _available_sections() -> list[str]:
        return [
            "identity",
            "wiki",
            "classification",
            "mechanism",
            "evidence",
            "known_limitations",
        ]

    def card(
        self,
        card_id: str,
        *,
        detail_level: str = "full",
        sections: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Return a Wiki card using summary -> sections -> full disclosure."""

        self._load()
        normalized = str(card_id or "").strip()
        if not _CARD_ID.fullmatch(normalized):
            raise ValueError("card_id must match ERD-CARD-NNN")
        document = self._document(normalized)
        level = str(detail_level or "full").strip().lower()
        if level not in {"summary", "sections", "full"}:
            raise ValueError("detail_level must be summary, sections, or full")
        available = self._available_sections()
        if level == "full":
            result = json.loads(json.dumps(document))
            result["disclosure"] = {
                "level": "full",
                "available_sections": available,
            }
            return result
        if level == "summary":
            wiki = document["wiki"]
            return {
                "card_id": normalized,
                "card_version": document["card_version"],
                "identity": json.loads(json.dumps(document["identity"])),
                "wiki_summary": wiki["summary"],
                "known_reactions": [
                    {
                        key: json.loads(json.dumps(reaction[key]))
                        for key in (
                            "reaction_id",
                            "name",
                            "activity_type",
                            "mechanistic_role",
                            "activity_status",
                            "mechanism_status",
                            "scope",
                        ) if key in reaction
                    }
                    for reaction in wiki["known_reactions"]
                ],
                "cofactor_classes": list(document["classification"]["cofactor_classes"]),
                "cofactor_notes": document["classification"]["cofactor_notes"],
                "functional_roles": list(wiki["functional_roles"]),
                "disclosure": {
                    "level": "summary",
                    "available_sections": available,
                    "next": "request detail_level=sections with only the evidence-bearing sections needed",
                },
            }
        requested_sections = [str(item or "").strip() for item in (sections or [])]
        if not requested_sections:
            raise ValueError("sections detail_level requires at least one section")
        unknown = sorted(set(requested_sections) - set(available))
        if unknown:
            raise ValueError(f"unknown card sections: {', '.join(unknown)}")
        result = {
            "card_id": normalized,
            "card_version": document["card_version"],
            "identity": json.loads(json.dumps(document["identity"])),
        }
        for section in requested_sections:
            result[section] = json.loads(json.dumps(document[section]))
        result["disclosure"] = {
            "level": "sections",
            "returned_sections": list(dict.fromkeys(requested_sections)),
            "available_sections": available,
            "field_refs": [f"{normalized}:{section}" for section in requested_sections],
            "next": "request full only when an independent final audit requires every field",
        }
        return result

    def stats(self) -> dict[str, Any]:
        self._load()
        assert self._manifest is not None
        assert self._cards is not None
        cofactors: Counter[str] = Counter()
        families: Counter[str] = Counter()
        transformations: Counter[str] = Counter()
        for card in self.iter_cards():
            classification = card["classification"]
            cofactors.update(classification["cofactor_classes"])
            transformations.update(classification["transformation_tags"])
            families.update([card["identity"]["enzyme_family"]])
        return {
            "knowledge_version": self._manifest["knowledge_version"],
            "card_count": len(self._entries),
            "ontology_count": len(self._manifest.get("ontologies") or []),
            "cofactor_counts": dict(sorted(cofactors.items())),
            "family_counts": dict(sorted(families.items())),
            "transformation_counts": dict(sorted(transformations.items())),
            "offline_only": True,
            "hidden_gold_exposed": False,
        }

    def search(self, query: Mapping[str, Any], *, workspace=None, workspace_dir=None) -> dict[str, Any]:
        """Indexed text retrieval; the score is not chemical suitability."""
        from .retrieval_tools import WikiIndex
        index = WikiIndex(self, workspace or self._workspace,
                          workspace_dir or ".tcode/plugins/enzyme_reaction_discovery")
        text = " ".join(str(value) for key, value in query.items()
                        if key != "limit" and isinstance(value, (str, list, tuple)))
        result = index.search([text], limit=max(1, min(15, int(query.get("limit") or 8))))
        # Compatibility for explicit card search; workflow uses claim-level rows.
        rows = []
        for hit in result["results"]:
            rows.append({**hit,
                         "retrieval": {"mode": result["retrieval_mode"],
                                       "matched_passages": [{"field_path": hit["field_path"],
                                                             "text": hit["excerpt"]}]}})
        return {**result, "results": rows}


__all__ = ["KnowledgeLibrary", "normalize_term"]
