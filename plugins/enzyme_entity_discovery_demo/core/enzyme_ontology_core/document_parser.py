"""Isolated, bounded text extraction. PDF block numbers refer to extracted text, not geometry."""
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path


PARSER_VERSION = 'text-lines-pdf-pages-v1'


def text_spans(lines, *, page=None, max_chars=4000):
    buffer, start, size, heading, block = [], 1, 0, '', 0
    for number, line in enumerate(lines, 1):
        line = line.rstrip('\r\n')
        if len(line) > max_chars:
            raise ValueError('line_too_long: split the source line explicitly')
        if buffer and (size + len(line) + 1 > max_chars or len(buffer) >= 32):
            block += 1
            yield {'text': '\n'.join(buffer), 'locator': {'page': page, 'block': block, 'line_start': start, 'line_end': number - 1, 'heading': heading}}
            buffer, size, start = [], 0, number
        if not buffer:
            start = number
            if line.startswith('#'):
                heading = line.lstrip('#').strip()
        buffer.append(line)
        size += len(line) + 1
    if buffer:
        block += 1
        yield {'text': '\n'.join(buffer), 'locator': {'page': page, 'block': block, 'line_start': start, 'line_end': start + len(buffer) - 1, 'heading': heading}}


def parse(source, output, media):
    count, empty_pages = 0, []
    with Path(output).open('w', encoding='utf-8') as out:
        def emit(spans):
            nonlocal count
            for span in spans:
                if not span['text'].strip():
                    continue
                count += 1
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
                for index, page in enumerate(reader.pages, 1):
                    text = page.extract_text() or ''
                    if not text.strip():
                        empty_pages.append(index)
                    else:
                        emit(text_spans(text.splitlines(), page=index))
        else:
            with Path(source).open(encoding='utf-8-sig', errors='strict') as stream:
                emit(text_spans(stream))
    if not count:
        raise ValueError('ocr_required' if media == 'pdf' else 'empty_document')
    return {'spans': count, 'empty_pages': empty_pages, 'warnings': ['pages_without_text'] if empty_pages else []}


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
