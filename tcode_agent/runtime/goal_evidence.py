"""Private citation handoff for evidence-dependent goal chat replies."""
from __future__ import annotations

import json
import re

_OPEN = re.compile(r'(?im)^\s*<goal_evidence\b[^>\n]*>')
_CLOSE = '</goal_evidence>'
_MAX_ROWS = 12
_MAX_BLOCK = 16_000


def split_goal_evidence(content: str) -> tuple[str, list[dict[str, str]], bool]:
    """Remove the private suffix before emitting or persisting the reply."""
    start = _OPEN.search(content)
    if not start:
        return content, [], False
    visible = content[:start.start()].rstrip()
    tail = content[start.end():].strip()
    if len(tail) > _MAX_BLOCK or not tail.endswith(_CLOSE):
        return visible, [], True
    try:
        rows = json.loads(tail[:-len(_CLOSE)])
    except (ValueError, RecursionError):
        return visible, [], True
    if not isinstance(rows, list) or not 1 <= len(rows) <= _MAX_ROWS:
        return visible, [], True
    valid = [row for row in rows if isinstance(row, dict) and row.keys() == {'claim', 'call_id', 'quote'}
             and all(isinstance(row[key], str) and 0 < len(row[key].strip()) <= limit
                     for key, limit in (('claim', 240), ('call_id', 160), ('quote', 480)))]
    return visible, valid, len(valid) != len(rows)


def is_observed_record(call) -> bool:
    state = getattr(call, 'state', None)
    metadata = getattr(call, 'metadata', None) or {}
    return (getattr(state, 'value', state) == 'completed' and not getattr(call, 'error', None)
            and not metadata.get('execution_skipped')
            and metadata.get('semantic_disposition') != 'superseded_neutral')


def resolve_goal_evidence(candidate, rows, messages):
    """Resolve only against this goal's preserved, successful tool results."""
    records = {}
    for message in messages:
        if message.role != 'tool':
            continue
        for call in message.tool_calls or []:
            if call.call_id and is_observed_record(call):
                # A provider ID reused for different messages is ambiguous.
                records[call.call_id] = None if call.call_id in records else (call, str(message.content or ''))
    normalize = lambda value: ' '.join(value.split())
    visible = normalize(candidate)
    resolved, invalid, seen = [], False, set()
    for row in rows:
        claim, quote = normalize(row['claim']), normalize(row['quote'])
        ref = row['call_id'].strip()
        record = records.get(ref)
        if not record or not claim or claim not in visible or len(quote) < 24:
            invalid = True
            continue
        if (claim, ref, quote) in seen:
            continue
        seen.add((claim, ref, quote))
        call, body = record
        if quote not in normalize(body):
            invalid = True
            continue
        args = call.arguments or {}
        # A section index is not source prose. Abstract bindings may support
        # abstract-level claims; the verifier must not treat them as body text.
        section = str(args.get('section') or '')[:256] if call.tool_name == 'webfetch' else ''
        if call.tool_name == 'webfetch' and args.get('view') == 'sections':
            invalid = True
            continue
        if call.tool_name == 'webfetch' and args.get('view') == 'evidence':
            # Recover the heading of the exact returned paragraph, including
            # when the caller searched across all sections.
            headings = {match.group(1) for match in re.finditer(r'(?ms)^\[([^\]\n]+)\]\n(.*?)(?=\n\n\[|\Z)', body)
                        if quote in normalize(match.group(0))}
            if len(headings) == 1:
                section = next(iter(headings))[:256]
            elif len(headings) > 1:
                invalid = True
                continue
        resolved.append({'claim': claim, 'call_id': ref, 'quote': quote, 'section': section,
                         'source_tool': call.tool_name})
    return resolved, invalid
