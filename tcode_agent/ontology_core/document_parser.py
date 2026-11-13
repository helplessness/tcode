"""Isolated, bounded text extraction. PDF block numbers refer to extracted text, not geometry."""
from __future__ import annotations
import hashlib
import json
import re
import sys
from pathlib import Path


PARSER_VERSION = 'text-lines-pdf-pages-v2-coverage'


def text_spans(lines, *, page=None, max_chars=4000, initial_heading=''):
    buffer, start, size, heading, block, last = [], 1, 0, initial_heading, 0, 0
    section = re.compile(r'^(abstract|introduction|results?|discussion|methods?|materials and methods|conclusions?|references|data availability|acknowledgements?)$', re.I)

    def emit(end):
        nonlocal buffer, start, size, block
        if not buffer or not any(line.strip() for line in buffer):
            buffer, size = [], 0
            return None
        block += 1
        text = '\n'.join(buffer)
        value = {'text': text, 'locator': {'page': page, 'block': block, 'line_start': start,
                 'line_end': end, 'heading': heading,
                 'sentence_count': len(re.findall(r'(?<=[.!?])(?:\s+|$)', text))}}
        buffer, size = [], 0
        return value

    for number, line in enumerate(lines, 1):
        line = line.rstrip('\r\n')
        if len(line) > max_chars:
            raise ValueError('line_too_long: split the source line explicitly')
        exact_heading = line.strip() if section.fullmatch(line.strip()) else ''
        paragraph_break = not line.strip()
        if buffer and (size + len(line) + 1 > max_chars or len(buffer) >= 32 or exact_heading or paragraph_break):
            if value := emit(number - 1):
                yield value
        if not buffer:
            start = number
            if exact_heading:
                heading = exact_heading
            elif line.startswith('#'):
                heading = line.lstrip('#').strip()
        if paragraph_break:
            continue
        buffer.append(line)
        last = number
        size += len(line) + 1
    if buffer:
        if value := emit(last):
            yield value


def parse(source, output, media):
    count, empty_pages, pages = 0, [], {}
    with Path(output).open('w', encoding='utf-8') as out:
        def emit(spans):
            nonlocal count
            for span in spans:
                if not span['text'].strip():
                    continue
                count += 1
                page = span['locator'].get('page')
                coverage = pages.setdefault(str(page or 0), {'spans': 0, 'characters': 0, 'lines': 0})
                coverage['spans'] += 1
                coverage['characters'] += len(span['text'])
                coverage['lines'] += len(span['text'].splitlines())
                span['text_sha256'] = hashlib.sha256(span['text'].encode()).hexdigest()
                span['extractor_version'] = PARSER_VERSION
                out.write(json.dumps(span, ensure_ascii=False) + '\n')
        if media == 'pdf':
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise ValueError('pdf_dependency_missing') from exc
            with Path(source).open('rb') as stream:
                reader = PdfReader(stream)
                if reader.is_encrypted:
                    raise ValueError('encrypted_pdf')
                heading = ''
                for index, page in enumerate(reader.pages, 1):
                    text = page.extract_text() or ''
                    if not text.strip():
                        empty_pages.append(index)
                    else:
                        spans = list(text_spans(text.splitlines(), page=index, initial_heading=heading))
                        if spans:
                            heading = spans[-1]['locator']['heading']
                        emit(spans)
        else:
            with Path(source).open(encoding='utf-8-sig', errors='strict') as stream:
                emit(text_spans(stream))
    if not count:
        raise ValueError('ocr_required' if media == 'pdf' else 'empty_document')
    warnings = ['pages_without_text'] if empty_pages else []
    if media == 'pdf':
        warnings += ['pdf_text_layer_only', 'figures_and_table_geometry_not_extracted', 'supplementary_files_not_included']
    return {'spans': count, 'empty_pages': empty_pages, 'pages': pages,
            'characters': sum(row['characters'] for row in pages.values()),
            'lines': sum(row['lines'] for row in pages.values()),
            'coverage': 'bounded_text_extraction_not_semantic_completeness', 'warnings': warnings}


if __name__ == '__main__':
    # Limits apply only to this parser child, not the TCode process.
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    try:
        result = parse(sys.argv[1], sys.argv[2], sys.argv[3])
        Path(sys.argv[2] + '.report.json').write_text(json.dumps(result))
    except Exception as exc:
        # No document contents in stderr/logs.
        known = ('line_too_long', 'encrypted_pdf', 'ocr_required', 'empty_document', 'pdf_dependency_missing')
        code = str(exc).split(':', 1)[0] if isinstance(exc, ValueError) and str(exc).startswith(known) else type(exc).__name__
        print(code[:160], file=sys.stderr)
        sys.exit(1)
