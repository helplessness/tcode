"""Extractive reducers. Selected rows are verbatim, never generated facts."""
import csv
import ast
import io
import json
import math
import re
import statistics
import time
from dataclasses import dataclass


@dataclass
class Reduced:
    kind: str
    view: dict


def decode(text: str):
    """Decode structured formats; ordinary prose is deliberately not guessed."""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return "json", json.loads(text)
        except (ValueError, RecursionError):
            # Existing MCP integrations serialize structuredContent with str().
            # literal_eval accepts data only (no calls or name evaluation).
            try:
                value = ast.literal_eval(text)
                if isinstance(value, (dict, list)):
                    json.dumps(value, allow_nan=False)
                    return "json", value
            except (ValueError, TypeError, SyntaxError, RecursionError):
                pass
            return "text", text.splitlines()
    lines = text.splitlines()
    if len(lines) >= 3 and ("," in lines[0] or "\t" in lines[0] or "|" in lines[0]):
        delimiter = "\t" if "\t" in lines[0] else "|" if "|" in lines[0] else ","
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
            if len(rows[0]) >= 2 and all(len(r) == len(rows[0]) for r in rows):
                return "table", rows
        except csv.Error:
            pass
    if len(lines) >= 3 and (len(set(lines)) < len(lines) * .8 or any(re.search(r"\b(ERROR|WARN|INFO|DEBUG|Traceback)\b", x) for x in lines)):
        return "log", lines
    return "text", lines


def _protected(field, value):
    if field in {'error', 'errors', 'exception', 'traceback', 'warning', 'warnings',
                 'contradictions', 'counter_evidence', 'counter_evidence_refs',
                 'hard_conflict', 'knowledge_gaps', 'verification_note'}:
        return bool(value)
    if field in {'ok', 'success'}:
        return value is False
    if field in {'selected', 'recommended'}:
        return value is True
    return field in {'status', 'state', 'support_status'} and str(value).lower() in {
        'failed', 'error', 'cancelled', 'canceled', 'timeout', 'blocked',
        'retracted', 'withdrawn', 'superseded', 'stale', 'rejected', 'refuted', 'conflicting',
    }


def _selected_rows(rows, deadline, columns=None):
    """Keep boundaries, explicit failures, type minorities and numeric extremes."""
    keep = set(range(min(3, len(rows)))) | set(range(max(0, len(rows)-3), len(rows)))
    numeric = {}
    types = {}
    categories = {}
    previous_states = {}
    for i, row in enumerate(rows):
        if time.monotonic() > deadline:
            raise TimeoutError("reducer_timeout")
        fields = row if isinstance(row, dict) else dict(enumerate(row)) if isinstance(row, list) else {"value": row}
        for key, value in fields.items():
            field_name = str(columns[key] if columns is not None and isinstance(key, int) else key).strip().lower()
            types.setdefault(key, {}).setdefault(type(value).__name__, []).append(i)
            if _protected(field_name, value):
                keep.add(i)
            if (value is None or value == "") and field_name not in {"error", "errors", "warning", "warnings", "exception", "traceback"}:
                keep.add(i)
            if field_name in {"unit", "units", "status", "state", "job_id", "support_status", "evidence_scope", "identity_level", "polarity"}:
                signature = json.dumps(value, sort_keys=True, ensure_ascii=False)
                seen = categories.setdefault(key, set())
                if signature not in seen:
                    keep.add(i)
                    seen.add(signature)
                if field_name in {"status", "state"}:
                    if key in previous_states and previous_states[key] != signature:
                        keep.update((max(0, i-1), i))
                    previous_states[key] = signature
            # Nested failures must not disappear merely because the top-level
            # row has no error field. Bound traversal and abort on excess depth.
            pending = [(value, 0)] if isinstance(value, (dict, list)) else []
            while pending:
                nested, depth = pending.pop()
                if depth > 20 or time.monotonic() > deadline:
                    raise TimeoutError("nested_result_budget")
                if isinstance(nested, dict):
                    if any(_protected(str(k).strip().lower(), v) for k, v in nested.items()):
                        keep.add(i)
                    pending.extend((v, depth+1) for v in nested.values() if isinstance(v, (dict, list)))
                elif isinstance(nested, list):
                    pending.extend((v, depth+1) for v in nested if isinstance(v, (dict, list)))
            try:
                number = float(value) if not isinstance(value, (bool, dict, list)) else None
            except (TypeError, ValueError, OverflowError):
                number = None
            if number is not None:
                if math.isfinite(number):
                    numeric.setdefault(key, []).append((number, i))
                else:
                    keep.add(i)
    stats = {}
    for key, values in numeric.items():
        low, high = min(values), max(values)
        keep.update((low[1], high[1]))
        median = statistics.median(x[0] for x in values)
        mad = statistics.median(abs(x[0] - median) for x in values)
        keep.update(i for value, i in values if abs(value - median) > 6 * mad)
        stats[str(key)] = {"min": low[0], "max": high[0], "count": len(values)}
        field_name = str(columns[key] if columns is not None else key).strip().lower()
        if field_name in {"score", "relevance", "relevance_score", "confidence"}:
            keep.update(i for _, i in sorted(values, reverse=True)[:5])
        elif field_name in {'rank', 'ranking'}:
            keep.update(i for _, i in sorted(values)[:5])
    for variants in types.values():
        present = {i for indices in variants.values() for i in indices}
        if len(present) != len(rows):
            keep.update(set(range(len(rows))) - present if len(present) > len(rows)/2 else present)
        if len(variants) > 1:
            majority = max(variants, key=lambda k: len(variants[k]))
            for name, indices in variants.items():
                if name != majority:
                    keep.update(indices)
    return sorted(keep), stats


def reduce_content(text: str, max_chars: int, deadline: float) -> Reduced | None:
    kind, data = decode(text)
    if kind == "text":
        return None
    if kind == "json":
        path = ""
        rows = data if isinstance(data, list) else None
        surrounding = {}
        if isinstance(data, dict):
            arrays = [(k, v) for k, v in data.items() if isinstance(v, list)]
            if arrays:
                key, rows = max(arrays, key=lambda pair: len(pair[1]))
                path = "/" + key.replace("~", "~0").replace("/", "~1")
                surrounding = {k: v for k, v in data.items() if k != key}
        if rows is None or len(rows) < 12:
            return None
        indices, stats = _selected_rows(rows, deadline)
        # Exact duplicates can share a representative; conflicting sources or
        # differing scientific values remain distinct even when DOI is equal.
        counts = {}
        signatures = []
        for row in rows:
            if time.monotonic() > deadline:
                raise TimeoutError("reducer_timeout")
            signature = json.dumps(row, sort_keys=True, ensure_ascii=False)
            signatures.append(signature)
            counts[signature] = counts.get(signature, 0) + 1
        seen = set()
        representatives = []
        for i in indices:
            if signatures[i] not in seen:
                representatives.append(i)
                seen.add(signatures[i])
        indices = representatives
        fields = {key for row in rows if isinstance(row, dict) for key in row}
        if fields & {"doi", "pmid", "arxiv_id"}:
            kind = "research"
        elif "job_id" in fields and fields & {"status", "state"}:
            kind = "async_job"
        view = {"array_pointer": path, "surrounding_fields": surrounding,
                "total": len(rows), "omitted": len(rows)-len(indices),
                "exact_duplicate_rows": len(rows)-len(counts),
                "numeric_statistics": stats,
                "items": [{"index": i, "duplicate_count": counts[signatures[i]], "value": rows[i]} for i in indices]}
    elif kind == "table":
        rows = data[1:]
        indices, stats = _selected_rows(rows, deadline, columns=data[0])
        view = {"header": data[0], "total": len(rows), "omitted": len(rows)-len(indices),
                "numeric_statistics_by_column": stats,
                "items": [{"index": i, "value": rows[i]} for i in indices]}
    else:
        keep = set(range(min(3, len(data)))) | set(range(max(0, len(data)-3), len(data)))
        previous = None
        for i, line in enumerate(data):
            if time.monotonic() > deadline:
                raise TimeoutError("reducer_timeout")
            if line != previous:
                keep.add(i)
            if re.search(r"\b(ERROR|WARN|FATAL|Traceback|Exception)\b", line):
                keep.update(range(max(0, i-3), min(len(data), i+12)))
            previous = line
        view = {"total_lines": len(data), "omitted": len(data)-len(keep),
                "lines": [{"index": i, "value": data[i]} for i in sorted(keep)]}
    # Protected fields may exhaust the budget: decline instead of truncating them.
    if len(json.dumps(view, ensure_ascii=False)) > max_chars or time.monotonic() > deadline:
        return None
    return Reduced(kind, view)
