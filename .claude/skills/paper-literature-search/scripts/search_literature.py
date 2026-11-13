#!/usr/bin/env python3
"""直接检索公开文献源，不依赖 MCP。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_SOURCES = ("pubmed", "arxiv", "crossref", "semanticscholar", "openalex")
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 0.5
DEFAULT_USER_AGENT = "tcode-paper-literature-search/0.1 (mailto:dev@example.com)"

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ARXIV_BASE = "http://export.arxiv.org/api/query"
CROSSREF_BASE = "https://api.crossref.org"
SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1"
OPENALEX_BASE = "https://api.openalex.org"
ARXIV_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
SEMANTIC_SCHOLAR_FIELDS = ",".join(
    [
        "paperId",
        "title",
        "abstract",
        "authors",
        "year",
        "venue",
        "journal",
        "publicationDate",
        "url",
        "openAccessPdf",
        "externalIds",
        "citationCount",
        "referenceCount",
        "isOpenAccess",
        "fieldsOfStudy",
    ]
)


def _extract_year(value: Any) -> int | None:
    text = str(value or "").strip()
    for index in range(0, max(0, len(text) - 3)):
        chunk = text[index:index + 4]
        if chunk.isdigit():
            try:
                year = int(chunk)
            except ValueError:
                return None
            if 1800 <= year <= 2100:
                return year
    return None


def _normalize_pmc_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if upper.startswith("PMC"):
        suffix = upper[3:]
        return f"PMC{suffix}" if suffix.isdigit() else text
    return f"PMC{text}" if text.isdigit() else text


def _year_in_range(year: Any, start_year: int | None, end_year: int | None) -> bool:
    parsed = _extract_year(year)
    if parsed is None:
        return True
    if start_year is not None and parsed < start_year:
        return False
    if end_year is not None and parsed > end_year:
        return False
    return True


def _openalex_abstract(inverted_index: Any) -> str:
    if not isinstance(inverted_index, dict) or not inverted_index:
        return ""
    tokens: list[tuple[int, str]] = []
    for token, positions in inverted_index.items():
        if not isinstance(token, str) or not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                tokens.append((pos, token))
    if not tokens:
        return ""
    tokens.sort(key=lambda item: item[0])
    return " ".join(token for _, token in tokens)


def _openalex_id_tail(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "/" in text:
        return text.rsplit("/", 1)[-1]
    return text


def _normalize_doi(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if lower.startswith("https://doi.org/"):
        return text[len("https://doi.org/"):].strip()
    if lower.startswith("http://doi.org/"):
        return text[len("http://doi.org/"):].strip()
    if lower.startswith("doi:"):
        return text[4:].strip()
    return text


def _http_get_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> tuple[int, str]:
    request_url = url
    if params:
        query_string = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        request_url = f"{url}?{query_string}"

    last_error: Exception | None = None
    merged_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
    if headers:
        merged_headers.update(headers)

    for attempt in range(1, max(1, retries) + 1):
        request = urllib.request.Request(request_url, headers=merged_headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                status = getattr(response, "status", 200)
                body = response.read().decode("utf-8", errors="replace")
                if status in RETRYABLE_STATUS_CODES and attempt < retries:
                    time.sleep(backoff_seconds * attempt)
                    continue
                return status, body
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if status in RETRYABLE_STATUS_CODES and attempt < retries:
                time.sleep(backoff_seconds * attempt)
                continue
            return status, body
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(backoff_seconds * attempt)

    curl_path = shutil.which("curl")
    if curl_path:
        return _curl_get_text(
            request_url,
            headers=merged_headers,
            timeout_seconds=timeout_seconds,
            retries=retries,
            backoff_seconds=backoff_seconds,
        )

    raise RuntimeError(f"HTTP request failed after {retries} attempts: {last_error}")


def _curl_get_text(
    request_url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
    retries: int,
    backoff_seconds: float,
) -> tuple[int, str]:
    command = [
        "curl",
        "-sS",
        "-L",
        "--connect-timeout",
        str(max(1, int(timeout_seconds))),
        "--max-time",
        str(max(1, int(timeout_seconds))),
        "--retry",
        str(max(0, int(retries) - 1)),
        "--retry-all-errors",
        "--retry-delay",
        str(max(1, int(backoff_seconds))),
        "-w",
        "\n%{http_code}",
    ]
    for key, value in headers.items():
        command.extend(["-H", f"{key}: {value}"])
    command.append(request_url)

    env = dict(os.environ)
    completed = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    if completed.returncode != 0:
        raise RuntimeError(f"curl failed with exit code {completed.returncode}: {completed.stderr.strip()}")

    body, separator, status_text = completed.stdout.rpartition("\n")
    if not separator:
        raise RuntimeError("curl response missing HTTP status trailer")
    try:
        status = int(status_text.strip())
    except ValueError as exc:
        raise RuntimeError(f"invalid curl HTTP status: {status_text!r}") from exc
    return status, body


def _http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    status, body = _http_get_text(url, params=params, headers=headers)
    if not body.strip():
        return status, {}
    payload = json.loads(body)
    if isinstance(payload, dict):
        return status, payload
    return status, {"data": payload}


def _unique_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in results:
        key = str(item.get("doi") or item.get("url") or item.get("title") or item.get("source_id") or "").strip().lower()
        if not key:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _parse_pubmed_summaries(body: str) -> list[dict[str, Any]]:
    root = ET.fromstring(body)
    articles: list[dict[str, Any]] = []
    for doc_sum in root.findall(".//DocSum"):
        pubmed_id = (doc_sum.findtext("Id") or "").strip()
        fields: dict[str, Any] = {"authors": []}
        for item in doc_sum.findall("Item"):
            name = str(item.get("Name") or "").strip()
            if not name:
                continue
            if name == "AuthorList":
                fields["authors"] = [
                    (child.text or "").strip()
                    for child in item.findall("Item")
                    if (child.text or "").strip()
                ]
                continue
            if name == "ArticleIds":
                for child in item.findall("Item"):
                    child_name = str(child.get("Name") or "").strip().lower()
                    child_text = (child.text or "").strip()
                    if child_name and child_text:
                        fields[f"articleid_{child_name}"] = child_text
                continue
            text = (item.text or "").strip()
            if text:
                fields[name] = text
        doi = str(fields.get("DOI") or fields.get("articleid_doi") or fields.get("ElocationID") or "").strip()
        if doi.lower().startswith("doi:"):
            doi = doi[4:].strip()
        pmc_id = _normalize_pmc_id(fields.get("PmcID") or fields.get("articleid_pmc") or "")
        pmc_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/" if pmc_id else ""
        pub_date = str(fields.get("PubDate") or "").strip()
        articles.append(
            {
                "source": "pubmed",
                "source_id": pubmed_id,
                "title": str(fields.get("Title") or "").strip(),
                "year": _extract_year(pub_date),
                "authors": fields.get("authors") if isinstance(fields.get("authors"), list) else [],
                "journal": str(fields.get("FullJournalName") or fields.get("Source") or "").strip(),
                "doi": doi,
                "abstract": "",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pubmed_id}/" if pubmed_id else "",
                "pdf_url": "",
                "pdf_url_status": "official_article_only" if pmc_id else "unavailable",
                "pdf_note": (
                    "Use PMCID/PMC article page for downstream full-text retrieval; do not assume the PMC /pdf/ path is a stable direct download URL."
                    if pmc_id
                    else "No PMCID found in PubMed summary; use DOI or PubMed landing page for downstream full-text resolution."
                ),
                "pmc_id": pmc_id,
                "pmc_url": pmc_url,
                "extra": {
                    "pubmed_id": pubmed_id,
                    "pmc_id": pmc_id,
                    "pmc_url": pmc_url,
                    "pub_date": pub_date,
                },
            }
        )
    return articles


def search_pubmed(query: str, max_results: int) -> list[dict[str, Any]]:
    status, search_payload = _http_get_json(
        f"{PUBMED_BASE}/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
            "sort": "relevance",
        },
    )
    if status != 200:
        raise RuntimeError(f"PubMed API returned {status}")

    id_list = search_payload.get("esearchresult", {}).get("idlist", [])
    ids = [str(item).strip() for item in id_list if str(item).strip()]
    if not ids:
        return []

    status, summary_body = _http_get_text(
        f"{PUBMED_BASE}/esummary.fcgi",
        params={
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "xml",
        },
    )
    if status != 200:
        raise RuntimeError(f"PubMed summary API returned {status}")
    return _parse_pubmed_summaries(summary_body)


def search_arxiv(query: str, max_results: int) -> list[dict[str, Any]]:
    status, body = _http_get_text(
        ARXIV_BASE,
        params={
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
        },
    )
    if status != 200:
        raise RuntimeError(f"ArXiv API returned {status}")

    root = ET.fromstring(body)
    results: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ARXIV_NAMESPACES):
        title_el = entry.find("atom:title", ARXIV_NAMESPACES)
        summary_el = entry.find("atom:summary", ARXIV_NAMESPACES)
        published_el = entry.find("atom:published", ARXIV_NAMESPACES)
        doi_el = entry.find("arxiv:doi", ARXIV_NAMESPACES)
        id_el = entry.find("atom:id", ARXIV_NAMESPACES)
        authors = [
            (author.text or "").strip()
            for author in entry.findall("atom:author/atom:name", ARXIV_NAMESPACES)
            if (author.text or "").strip()
        ]
        url = (id_el.text or "").strip() if id_el is not None and id_el.text else ""
        source_id = url.split("/")[-1] if url else ""
        pdf_url = f"https://arxiv.org/pdf/{source_id.split('v')[0]}.pdf" if source_id else ""
        published = (published_el.text or "").strip() if published_el is not None and published_el.text else ""
        results.append(
            {
                "source": "arxiv",
                "source_id": source_id,
                "title": (title_el.text or "").strip().replace("\n", " ") if title_el is not None and title_el.text else "",
                "year": _extract_year(published),
                "authors": authors,
                "journal": "arXiv",
                "doi": (doi_el.text or "").strip() if doi_el is not None and doi_el.text else "",
                "abstract": (summary_el.text or "").strip() if summary_el is not None and summary_el.text else "",
                "url": url,
                "pdf_url": pdf_url,
                "pdf_url_status": "verified_direct" if pdf_url else "unavailable",
                "pdf_note": "arXiv exposes a stable direct PDF URL." if pdf_url else "No arXiv PDF URL found.",
                "extra": {"published": published},
            }
        )
    return results


def _crossref_pub_date(item: dict[str, Any]) -> tuple[str, int | None]:
    published = item.get("published-print", item.get("published-online", {}))
    if not isinstance(published, dict):
        return "", None
    parts = published.get("date-parts", [[]])
    if not isinstance(parts, list) or not parts:
        return "", None
    values = parts[0] if isinstance(parts[0], list) else []
    text = "-".join(str(part) for part in values)
    return text, _extract_year(text)


def search_crossref(query: str, max_results: int) -> list[dict[str, Any]]:
    status, payload = _http_get_json(
        f"{CROSSREF_BASE}/works",
        params={"query.bibliographic": query, "rows": max_results},
        headers={"Accept": "application/json"},
    )
    if status != 200:
        raise RuntimeError(f"CrossRef API returned {status}")

    message = payload.get("message", {})
    items = message.get("items", []) if isinstance(message, dict) else []
    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title_values = item.get("title", []) if isinstance(item.get("title"), list) else []
        container = item.get("container-title", []) if isinstance(item.get("container-title"), list) else []
        authors: list[str] = []
        for author in item.get("author", []) or []:
            if not isinstance(author, dict):
                continue
            given = str(author.get("given") or "").strip()
            family = str(author.get("family") or "").strip()
            full_name = f"{given} {family}".strip()
            if full_name:
                authors.append(full_name)
        pub_date, year = _crossref_pub_date(item)
        doi = str(item.get("DOI") or "").strip()
        results.append(
            {
                "source": "crossref",
                "source_id": doi,
                "title": str(title_values[0] if title_values else "").strip(),
                "year": year,
                "authors": authors,
                "journal": str(container[0] if container else "").strip(),
                "doi": doi,
                "abstract": str(item.get("abstract") or "").strip(),
                "url": str(item.get("URL") or (f"https://doi.org/{doi}" if doi else "")).strip(),
                "pdf_url": "",
                "pdf_url_status": "unavailable",
                "pdf_note": "Crossref search returns metadata; resolve full text downstream from DOI/PMCID rather than assuming a direct PDF link here.",
                "extra": {
                    "publisher": str(item.get("publisher") or "").strip(),
                    "pub_date": pub_date,
                    "type": str(item.get("type") or "").strip(),
                },
            }
        )
    return results


def search_semanticscholar(query: str, max_results: int) -> list[dict[str, Any]]:
    headers = {"Accept": "application/json"}
    api_key = str(os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
    if api_key:
        headers["x-api-key"] = api_key

    status, payload = _http_get_json(
        f"{SEMANTIC_SCHOLAR_BASE}/paper/search",
        params={
            "query": query,
            "limit": max_results,
            "offset": 0,
            "fields": SEMANTIC_SCHOLAR_FIELDS,
        },
        headers=headers,
    )
    if status != 200:
        raise RuntimeError(f"Semantic Scholar API returned {status}")

    data = payload.get("data", []) if isinstance(payload.get("data"), list) else []
    results: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        authors = [
            str(author.get("name") or "").strip()
            for author in item.get("authors", []) or []
            if isinstance(author, dict) and str(author.get("name") or "").strip()
        ]
        external_ids = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
        open_access_pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
        journal = item.get("journal") if isinstance(item.get("journal"), dict) else {}
        paper_id = str(item.get("paperId") or "").strip()
        doi = str(external_ids.get("DOI") or "").strip()
        open_access_pdf_url = str(open_access_pdf.get("url") or "").strip()
        results.append(
            {
                "source": "semanticscholar",
                "source_id": paper_id,
                "title": str(item.get("title") or "").strip(),
                "year": item.get("year") if isinstance(item.get("year"), int) else _extract_year(item.get("publicationDate")),
                "authors": authors,
                "journal": str(journal.get("name") or item.get("venue") or "").strip(),
                "doi": doi,
                "abstract": str(item.get("abstract") or "").strip(),
                "url": str(item.get("url") or (f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else "")).strip(),
                "pdf_url": open_access_pdf_url,
                "pdf_url_status": "candidate_unverified" if open_access_pdf_url else "unavailable",
                "pdf_note": (
                    "Semantic Scholar openAccessPdf is a candidate link and may still point to a publisher page or restricted endpoint; verify before treating it as a stable direct download."
                    if open_access_pdf_url
                    else "No openAccessPdf URL returned by Semantic Scholar."
                ),
                "extra": {
                    "citation_count": int(item.get("citationCount") or 0),
                    "reference_count": int(item.get("referenceCount") or 0),
                    "fields_of_study": item.get("fieldsOfStudy") if isinstance(item.get("fieldsOfStudy"), list) else [],
                },
            }
        )
    return results


def search_openalex(query: str, max_results: int) -> list[dict[str, Any]]:
    status, payload = _http_get_json(
        f"{OPENALEX_BASE}/works",
        params={
            "search": query,
            "per-page": max_results,
            "page": 1,
            "select": ",".join(
                [
                    "id",
                    "display_name",
                    "publication_year",
                    "publication_date",
                    "authorships",
                    "primary_location",
                    "best_oa_location",
                    "open_access",
                    "doi",
                    "ids",
                    "abstract_inverted_index",
                    "type",
                    "cited_by_count",
                ]
            ),
        },
        headers={"Accept": "application/json"},
    )
    if status != 200:
        raise RuntimeError(f"OpenAlex API returned {status}")

    items = payload.get("results", []) if isinstance(payload.get("results"), list) else []
    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        authorships = item.get("authorships") if isinstance(item.get("authorships"), list) else []
        authors: list[str] = []
        for authorship in authorships:
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
            name = str(author.get("display_name") or "").strip()
            if name:
                authors.append(name)

        primary_location = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
        primary_source = primary_location.get("source") if isinstance(primary_location.get("source"), dict) else {}
        best_oa_location = item.get("best_oa_location") if isinstance(item.get("best_oa_location"), dict) else {}
        open_access = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}

        source_id = _openalex_id_tail(item.get("id"))
        doi = _normalize_doi(item.get("doi") or (item.get("ids") or {}).get("doi"))
        landing_url = str(primary_location.get("landing_page_url") or item.get("id") or "").strip()
        pdf_url = str(best_oa_location.get("pdf_url") or open_access.get("oa_url") or "").strip()
        has_open_pdf = bool(pdf_url)

        results.append(
            {
                "source": "openalex",
                "source_id": source_id,
                "title": str(item.get("display_name") or "").strip(),
                "year": item.get("publication_year") if isinstance(item.get("publication_year"), int) else _extract_year(item.get("publication_date")),
                "authors": authors,
                "journal": str(primary_source.get("display_name") or "").strip(),
                "doi": doi,
                "abstract": _openalex_abstract(item.get("abstract_inverted_index")),
                "url": landing_url,
                "pdf_url": pdf_url,
                "pdf_url_status": "candidate_unverified" if has_open_pdf else "unavailable",
                "pdf_note": (
                    "OpenAlex open access link is a candidate URL and should be verified before treating it as a stable direct PDF download."
                    if has_open_pdf
                    else "No open access PDF URL returned by OpenAlex."
                ),
                "extra": {
                    "is_oa": bool(open_access.get("is_oa")),
                    "oa_status": str(open_access.get("oa_status") or "").strip(),
                    "type": str(item.get("type") or "").strip(),
                    "cited_by_count": int(item.get("cited_by_count") or 0),
                },
            }
        )
    return results


SEARCH_HANDLERS = {
    "pubmed": search_pubmed,
    "arxiv": search_arxiv,
    "crossref": search_crossref,
    "semanticscholar": search_semanticscholar,
    "openalex": search_openalex,
}


def _render_markdown(output: dict[str, Any]) -> str:
    lines = [
        "# 文献检索结果",
        "",
        f"- query: {output.get('query', '')}",
        f"- sources: {', '.join(output.get('sources', []))}",
        f"- has_results: {output.get('has_results', False)}",
        "",
        "## 执行状态",
        "",
        "| source | status | count | error |",
        "|---|---|---:|---|",
    ]
    for attempt in output.get("attempts", []):
        lines.append(
            f"| {attempt.get('source', '')} | {attempt.get('status', '')} | {attempt.get('count', 0)} | {str(attempt.get('error', ''))[:120]} |"
        )

    lines.extend(["", "## 结果", ""])
    results = output.get("results", []) or []
    if not results:
        lines.append("无命中文献。")
        return "\n".join(lines)

    for index, item in enumerate(results, start=1):
        authors = ", ".join(item.get("authors", [])[:8])
        lines.extend(
            [
                f"### {index}. {item.get('title', '')}",
                f"- source: {item.get('source', '')}",
                f"- source_id: {item.get('source_id', '')}",
                f"- year: {item.get('year', '')}",
                f"- journal: {item.get('journal', '')}",
                f"- doi: {item.get('doi', '')}",
                f"- url: {item.get('url', '')}",
                f"- pmc_id: {item.get('pmc_id', '')}",
                f"- pmc_url: {item.get('pmc_url', '')}",
                f"- pdf_url: {item.get('pdf_url', '')}",
                f"- pdf_url_status: {item.get('pdf_url_status', '')}",
                f"- pdf_note: {item.get('pdf_note', '')}",
                f"- authors: {authors}",
                f"- abstract: {str(item.get('abstract', '')).strip()[:600]}",
                "",
            ]
        )
    return "\n".join(lines)


def run_search(
    query: str,
    *,
    sources: list[str],
    max_results: int,
    start_year: int | None,
    end_year: int | None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for source in sources:
        handler = SEARCH_HANDLERS.get(source)
        if handler is None:
            attempts.append({"source": source, "status": "skipped", "count": 0, "error": "unsupported source"})
            continue
        try:
            source_results = handler(query, max_results)
            filtered = [
                item for item in source_results
                if _year_in_range(item.get("year"), start_year, end_year)
            ]
            attempts.append(
                {
                    "source": source,
                    "status": "ok" if filtered else "empty",
                    "count": len(filtered),
                    "error": "",
                }
            )
            results.extend(filtered)
        except Exception as exc:
            attempts.append(
                {
                    "source": source,
                    "status": "failed",
                    "count": 0,
                    "error": str(exc),
                }
            )

    results = _unique_results(results)[:max_results]
    successful_attempts = [item for item in attempts if item.get("status") in {"ok", "empty"}]
    return {
        "ok": bool(successful_attempts),
        "has_results": bool(results),
        "query": query,
        "sources": sources,
        "start_year": start_year,
        "end_year": end_year,
        "attempts": attempts,
        "result_count": len(results),
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="直接检索公开文献源，不依赖 MCP。")
    parser.add_argument("--query", required=True, help="检索词")
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCES),
        help="逗号分隔的数据源列表：pubmed,arxiv,crossref,semanticscholar,openalex",
    )
    parser.add_argument("--max-results", type=int, default=10, help="总体返回上限")
    parser.add_argument("--start-year", type=int, default=None, help="起始年份")
    parser.add_argument("--end-year", type=int, default=None, help="结束年份")
    parser.add_argument("--format", choices=("json", "markdown"), default="json", help="输出格式")
    parser.add_argument("--output", default="", help="可选输出文件路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sources = [item.strip() for item in str(args.sources or "").split(",") if item.strip()]
    if not sources:
        sources = list(DEFAULT_SOURCES)

    output = run_search(
        args.query,
        sources=sources,
        max_results=max(1, min(int(args.max_results or 10), 50)),
        start_year=args.start_year,
        end_year=args.end_year,
    )

    rendered = (
        json.dumps(output, ensure_ascii=False, indent=2)
        if args.format == "json"
        else _render_markdown(output)
    )

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")

    print(rendered)
    return 0 if output.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
