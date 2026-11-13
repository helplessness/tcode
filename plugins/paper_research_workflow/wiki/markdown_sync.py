from __future__ import annotations

import json
import re
from typing import Any


CLAIMS_START = "<!-- paper-wiki:claims:start -->"
CLAIMS_END = "<!-- paper-wiki:claims:end -->"
CLAIM_LINE_RE = re.compile(
    r"^-\s+(?:<!--\s*paper-wiki:claim:([^\s]+)\s*-->\s*)?"
    r"`([^`]+)`\s+(.*)$"
)
EVIDENCE_MARKER_RE = re.compile(r"\s*\[evidence:([^\]]+)\]")


def _json_value(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return default


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    metadata: dict[str, Any] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata, body


def _split_claims(body: str) -> tuple[str, str, bool]:
    if CLAIMS_START in body and CLAIMS_END in body:
        before, remainder = body.split(CLAIMS_START, 1)
        claims, after = remainder.split(CLAIMS_END, 1)
        return (before + after).strip(), claims.strip(), True
    marker = "\n## 结构化结论\n"
    if marker in body:
        before, claims = body.rsplit(marker, 1)
        return before.strip(), claims.strip(), True
    return body.strip(), "", False


def _extract_title_and_content(body: str, fallback_title: str) -> tuple[str, str]:
    lines = body.splitlines()
    title = fallback_title
    title_index = -1
    for index, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip() or fallback_title
            title_index = index
            break
    if title_index >= 0:
        content = "\n".join(lines[title_index + 1 :]).strip()
    else:
        content = body.strip()
    return title, content


def _parse_claims(block: str, existing_claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing_order = [dict(claim) for claim in existing_claims if isinstance(claim, dict)]
    existing = {
        str(claim.get("claim_id") or ""): dict(claim)
        for claim in existing_order
        if str(claim.get("claim_id") or "")
    }
    claims: list[dict[str, Any]] = []
    for line in block.splitlines():
        match = CLAIM_LINE_RE.match(line.strip())
        if not match:
            continue
        claim_id, support_status, raw_statement = match.groups()
        evidence_ids = EVIDENCE_MARKER_RE.findall(raw_statement)
        statement = EVIDENCE_MARKER_RE.sub("", raw_statement).strip()
        if not statement:
            continue
        claim = dict(existing.get(str(claim_id or ""), {}))
        if not claim_id and len(claims) < len(existing_order):
            # Schema v1 Markdown had no stable Claim marker. Preserve IDs by
            # materialized order during the first reconcile/migration.
            claim = dict(existing_order[len(claims)])
        if claim_id:
            claim["claim_id"] = claim_id
        claim["statement"] = statement
        claim["support_status"] = support_status.strip()
        claim["evidence_ids"] = list(dict.fromkeys(evidence_ids))
        if claim.get("evidence_links"):
            claim["evidence_links"] = [
                link
                for link in claim.get("evidence_links") or []
                if str(link.get("evidence_id") or "") in claim["evidence_ids"]
            ]
        claims.append(claim)
    return claims


def parse_materialized_markdown(
    text: str,
    *,
    existing_page: dict[str, Any],
) -> dict[str, Any]:
    metadata, body = _frontmatter(str(text or ""))
    content_body, claims_block, claims_present = _split_claims(body)
    title, content = _extract_title_and_content(
        content_body,
        str(existing_page.get("title") or "Wiki page"),
    )
    payload = dict(existing_page)
    payload["title"] = title
    payload["content"] = content
    if "summary" in metadata:
        parsed_summary = _json_value(str(metadata["summary"]), metadata["summary"])
        payload["summary"] = str(parsed_summary or "").strip()
    page_type = str(metadata.get("page_type") or "").strip()
    if page_type:
        payload["page_type"] = page_type
    status = str(metadata.get("status") or "").strip()
    if status:
        payload["status"] = status
    for field in ("aliases", "source_doc_ids", "evidence_ids"):
        if field in metadata:
            parsed = _json_value(str(metadata[field]), payload.get(field) or [])
            if isinstance(parsed, list):
                payload[field] = parsed
    if claims_present:
        payload["claims"] = _parse_claims(
            claims_block,
            list(existing_page.get("claims") or []),
        )
    return payload


def semantic_projection(page: dict[str, Any]) -> dict[str, Any]:
    claims = []
    for claim in page.get("claims") or []:
        claims.append(
            {
                "claim_id": str(claim.get("claim_id") or ""),
                "statement": str(claim.get("statement") or "").strip(),
                "evidence_ids": list(claim.get("evidence_ids") or []),
                "support_status": str(claim.get("support_status") or ""),
            }
        )
    return {
        "page_type": str(page.get("page_type") or ""),
        "title": str(page.get("title") or "").strip(),
        "summary": str(page.get("summary") or "").strip(),
        "content": str(page.get("content") or "").strip(),
        "status": str(page.get("status") or ""),
        "aliases": list(page.get("aliases") or []),
        "source_doc_ids": list(page.get("source_doc_ids") or []),
        "evidence_ids": list(page.get("evidence_ids") or []),
        "claims": claims,
    }
