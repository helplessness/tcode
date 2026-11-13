"""Bounded adjacent source evidence; context alone never becomes a citation."""
import hashlib

from .bundle import digest


def text_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def consecutive_pages(previous, current):
    a, b = previous['locator'].get('page'), current['locator'].get('page')
    return a is None and b is None or isinstance(a, int) and isinstance(b, int) and b in (a, a + 1)


def adjacent_window(current, previous):
    """Keep original parts intact; no cross-document, skipped or growing windows."""
    if (previous is None or previous['source_id'] != current['source_id']
            or previous['source_revision'] != current['source_revision']
            or previous['object_sha256'] != current['object_sha256']
            or previous['source_ordinal'] + 1 != current['source_ordinal']
            or not consecutive_pages(previous, current)
            or previous['locator'].get('heading') != current['locator'].get('heading')
            or str(current['locator'].get('heading', '')).lower() == 'references'
            or len(previous['text']) + len(current['text']) + 1 > 8001):
        return current
    parts = [previous, current]
    text = '\n'.join(p['text'] for p in parts)
    return {**current, 'text': text, 'text_sha256': text_digest(text),
            'span_id': 'span:' + digest({'source': current['source_id'], 'revision': current['source_revision'],
                                        'parts': [p['span_id'] for p in parts]}),
            'evidence_parts': parts,
            'locator': {**current['locator'], 'evidence_window': [
                {'span_id': p['span_id'], **p['locator']} for p in parts]}}


def evidence_references(span, quote):
    parts = span.get('evidence_parts')
    if parts is None:
        return []  # Existing single-span drafts retain their original contract.
    if not isinstance(parts, list) or len(parts) != 2 or any('evidence_parts' in p for p in parts):
        raise ValueError('evidence_window_parts_invalid')
    for part in parts:
        if (not part.get('source_id') or not part.get('object_sha256')
                or not isinstance(part.get('source_revision'), int) or part['source_revision'] < 1):
            raise ValueError('evidence_part_source_invalid')
        if any(part.get(k) != span.get(k) for k in ('source_id', 'source_revision', 'object_sha256')):
            raise ValueError('evidence_window_source_mismatch')
        if part['text_sha256'] != text_digest(part['text']):
            raise ValueError('evidence_part_hash_mismatch')
        if part['span_id'] != 'span:' + digest({'source': part['source_id'], 'revision': part['source_revision'],
                                                'locator': part['locator'], 'text': part['text_sha256']}):
            raise ValueError('evidence_part_identity_mismatch')
    if (parts[0]['source_ordinal'] + 1 != parts[1]['source_ordinal']
            or not consecutive_pages(parts[0], parts[1])
            or parts[0]['locator'].get('heading') != parts[1]['locator'].get('heading')
            or str(parts[1]['locator'].get('heading', '')).lower() == 'references'):
        raise ValueError('evidence_window_not_adjacent')
    joined = '\n'.join(p['text'] for p in parts)
    if len(joined) > 8001 or joined != span['text'] or text_digest(joined) != span['text_sha256']:
        raise ValueError('evidence_window_text_mismatch')
    if not quote:
        return []
    start = joined.find(quote)
    if start < 0:
        raise ValueError('evidence_quote_not_in_window')
    end, offset, refs = start + len(quote), 0, []
    for part in parts:
        left, right = max(start, offset), min(end, offset + len(part['text']))
        if left < right:
            a, b = left - offset, right - offset
            locator = {**part['locator'],
                       'line_start': part['locator']['line_start'] + part['text'][:a].count('\n'),
                       'line_end': part['locator']['line_start'] + part['text'][:b - 1].count('\n')}
            refs.append({k: part[k] for k in ('span_id', 'source_id', 'source_revision', 'object_sha256', 'text_sha256')}
                        | {'locator': locator, 'start_char': a, 'end_char': b,
                           'quote_sha256': text_digest(part['text'][a:b])})
        offset += len(part['text']) + 1
    return refs
