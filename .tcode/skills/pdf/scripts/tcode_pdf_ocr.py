"""TCode adapter: produce a page-labelled text artifact from a scanned PDF."""

import argparse
from pathlib import Path

from pdf2image import convert_from_path
from pytesseract import image_to_string


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--language", default="eng")
    args = parser.parse_args()
    pages = convert_from_path(args.input, dpi=200)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        for number, image in enumerate(pages, 1):
            print(f"--- Page {number} ---", image_to_string(image, lang=args.language).strip(),
                  "", sep="\n", file=output)
