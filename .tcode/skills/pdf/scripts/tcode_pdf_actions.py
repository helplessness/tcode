"""TCode adapter: write PDF extraction results to a workspace JSON artifact."""

import argparse
import json
from pathlib import Path


def text_result(path):
    from pypdf import PdfReader

    pages = []
    for number, page in enumerate(PdfReader(path).pages, 1):
        text = (page.extract_text() or "").strip()
        pages.append(dict(page=number, char_count=len(text), text=text))
    return dict(pdf_path=str(path), page_count=len(pages), pages=pages,
                full_text="\n\n".join(p["text"] for p in pages if p["text"]))


def write_result(extract):
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = extract(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k in ("page_count", "table_count")}))


if __name__ == "__main__":
    write_result(text_result)
