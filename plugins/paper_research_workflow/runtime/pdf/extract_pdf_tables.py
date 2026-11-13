import json
import re
import sys
from pathlib import Path
from typing import Any

import pdfplumber


CID_REPLACEMENTS = {
    "(cid:1)": "~",
    "(cid:3)": "-",
    "(cid:4)": "−",
    "(cid:6)": "±",
    "(cid:7)": "±",
    "(cid:8)": "×",
    "(cid:9)": "[",
    "(cid:10)": "]",
}
SUPERSCRIPT_DIGITS = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")
TABLE_LABEL_RE = re.compile(r"\bTABLE\s*(\d+[A-Z]?)\b", re.IGNORECASE)


def _normalize_pdf_text(value: Any) -> str:
    """统一 PDF 字形占位符、科学计数法和多余空白。"""

    text = "" if value is None else str(value)
    for source, target in CID_REPLACEMENTS.items():
        text = text.replace(source, target)
    text = re.sub(
        r"×\s*10(-?\d+)",
        lambda match: "× 10" + match.group(1).translate(SUPERSCRIPT_DIGITS),
        text,
    )
    text = re.sub(
        r"10-\s*(\d+)",
        lambda match: "10" + ("-" + match.group(1)).translate(SUPERSCRIPT_DIGITS),
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def _normalize_row(row):
    """规范化单行单元格，同时保留空单元格的列位置。"""

    return [_normalize_pdf_text(cell) for cell in row]


def _cluster_horizontal_rule_regions(page: Any) -> list[tuple[float, float, float, float]]:
    """Find scientific tables that use horizontal rules without a full grid."""
    page_width = float(page.width or 0)
    candidates = []
    for line in page.lines or []:
        x0 = float(line.get("x0") or 0)
        x1 = float(line.get("x1") or 0)
        top = float(line.get("top") or 0)
        bottom = float(line.get("bottom") or top)
        if abs(bottom - top) > 1.5 or (x1 - x0) < max(80.0, page_width * 0.25):
            continue
        candidates.append((x0, x1, top))

    groups: list[dict[str, Any]] = []
    for x0, x1, top in sorted(candidates, key=lambda item: (item[0], item[2])):
        group = next(
            (
                item
                for item in groups
                if abs(float(item["x0"]) - x0) <= 3.0
                and abs(float(item["x1"]) - x1) <= 3.0
            ),
            None,
        )
        if group is None:
            group = {"x0": x0, "x1": x1, "tops": []}
            groups.append(group)
        group["tops"].append(top)

    regions = []
    page_x0, page_top, page_x1, page_bottom = [float(value) for value in page.bbox]
    for group in groups:
        tops = sorted({round(float(value), 2) for value in group["tops"]})
        if len(tops) < 2 or tops[-1] - tops[0] < 18.0:
            continue
        x0 = max(page_x0, float(group["x0"]) - 1.0)
        x1 = min(page_x1, float(group["x1"]) + 1.0)
        top = max(page_top, tops[0])
        bottom = min(page_bottom, tops[-1] + 1.0)
        caption_top = max(page_top, top - 80.0)
        caption_text = _normalize_pdf_text(
            page.crop((x0, caption_top, x1, min(page_bottom, top + 3.0))).extract_text(
                x_tolerance=1,
                y_tolerance=2,
            )
            or ""
        )
        if TABLE_LABEL_RE.search(caption_text):
            regions.append((x0, top, x1, bottom))
    return regions


def _layout_rows(page: Any, bbox: tuple[float, float, float, float]) -> list[list[str]]:
    """按视觉行和稳定列锚点重建无竖线科学表格。

    横线型论文表格通常依靠横坐标表达列关系，多行表头与空单元格会让
    仅按文本间距切分的结果发生整体左移。本函数先找出单元格最多的视觉
    行作为列锚点，再将每一行的单元格分配到最近锚点，从源头保留空列。
    """

    x0, top, x1, bottom = bbox
    words = [
        word
        for word in (page.extract_words(x_tolerance=1, y_tolerance=2) or [])
        if float(word.get("x0") or 0) >= x0
        and float(word.get("x1") or 0) <= x1
        and float(word.get("top") or 0) >= top
        and float(word.get("bottom") or 0) <= bottom
    ]
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(
        words,
        key=lambda item: (float(item.get("top") or 0), float(item.get("x0") or 0)),
    ):
        if not lines or abs(float(word.get("top") or 0) - float(lines[-1][0].get("top") or 0)) > 2.5:
            lines.append([word])
        else:
            lines[-1].append(word)

    raw_rows: list[list[tuple[float, str]]] = []
    for line in lines:
        cells: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for word in sorted(line, key=lambda item: float(item.get("x0") or 0)):
            if current and float(word.get("x0") or 0) - float(current[-1].get("x1") or 0) > 10.0:
                cells.append(current)
                current = []
            current.append(word)
        if current:
            cells.append(current)
        row = [
            (
                float(cell[0].get("x0") or 0),
                _normalize_pdf_text(
                    " ".join(str(word.get("text") or "") for word in cell)
                ),
            )
            for cell in cells
        ]
        if any(text for _x0, text in row):
            raw_rows.append(row)
    if not raw_rows:
        return []

    anchor_row = max(raw_rows, key=len)
    anchors = sorted(float(x0) for x0, _text in anchor_row)
    rows: list[list[str]] = []
    for raw_row in raw_rows:
        aligned = [""] * len(anchors)
        for cell_x0, text in raw_row:
            column_index = min(
                range(len(anchors)),
                key=lambda index: abs(anchors[index] - cell_x0),
            )
            aligned[column_index] = " ".join(
                part for part in (aligned[column_index], text) if part
            )
        rows.append(aligned)
    return rows


def _normalize_header_cell(value: Any) -> str:
    """把多行表头中的下标恢复为可检索的常见指标名称。"""

    text = _normalize_pdf_text(value)
    text = re.sub(r"\bK\s+\((mM)\)\s+m\b", r"Km (\1)", text)
    text = re.sub(r"\bk\s+\((s[^)]*)\)\s+cat\b", r"kcat (\1)", text)
    return text


def _split_layout_header(rows: list[list[str]]) -> tuple[list[str], list[list[str]], int]:
    """合并多行表头，并返回表头、正文行及表头视觉行数。

    数据行至少应包含两个以数字开头的单元格。无法可靠识别正文起点时，
    退回第一行表头；调用方仍可根据列数一致性决定是否生成结构化事实。
    """

    if not rows:
        return [], [], 0
    body_start = next(
        (
            index
            for index, row in enumerate(rows)
            if sum(
                1
                for cell in row
                if re.match(r"^[+−-]?\d", str(cell or "").strip())
            )
            >= 2
        ),
        1,
    )
    if body_start <= 0:
        body_start = 1
    width = max((len(row) for row in rows), default=0)
    header_rows = [row + [""] * (width - len(row)) for row in rows[:body_start]]
    header = [
        _normalize_header_cell(
            " ".join(
                str(row[column_index] or "").strip()
                for row in header_rows
                if str(row[column_index] or "").strip()
            )
        )
        for column_index in range(width)
    ]
    body = [row + [""] * (width - len(row)) for row in rows[body_start:]]
    return header, body, body_start


def _extract_horizontal_rule_tables(page: Any, page_index: int) -> list[dict[str, Any]]:
    """抽取当前页中由横线分隔、缺少完整网格的科学表格。"""

    tables: list[dict[str, Any]] = []
    _, page_top, _, page_bottom = [float(value) for value in page.bbox]
    for fallback_index, bbox in enumerate(_cluster_horizontal_rule_regions(page), start=1):
        x0, top, x1, _ = bbox
        caption_top = max(page_top, top - 80.0)
        caption = _normalize_pdf_text(
            page.crop((x0, caption_top, x1, min(page_bottom, top + 3.0))).extract_text(
                x_tolerance=1,
                y_tolerance=2,
            )
            or ""
        )
        label_match = TABLE_LABEL_RE.search(caption)
        label = f"Table {label_match.group(1)}" if label_match else ""
        rows = _layout_rows(page, bbox)
        if not rows:
            continue
        header, body, header_row_count = _split_layout_header(rows)
        matrix = [header, *body]
        table_id = (
            f"table_{label_match.group(1).lower()}_page_{page_index}"
            if label_match
            else f"page_{page_index}_layout_table_{fallback_index}"
        )
        tables.append(
            {
                "table_id": table_id,
                "label": label,
                "caption": caption,
                "page": page_index,
                "page_span": [page_index, page_index],
                "table_index": fallback_index,
                "row_count": len(matrix),
                "column_count": len(header),
                "header": header,
                "rows": body,
                "matrix": matrix,
                "markdown": _to_markdown(matrix),
                "bbox": [float(value) for value in bbox],
                "bbox_coordinate_system": "pdfplumber_top_left_points",
                "page_width": float(page.width or 0),
                "page_height": float(page.height or 0),
                "extraction_method": "horizontal_rule_layout",
                "column_alignment": {
                    "status": "aligned",
                    "column_count": len(header),
                    "header_row_count": header_row_count,
                    "method": "nearest_x_anchor",
                },
            }
        )
    return tables


def extract_tables(pdf_path: Path) -> dict:
    """抽取 PDF 表格，并为结构化列关系记录可靠性元数据。"""

    tables = []
    warnings = []
    methods = set()
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            extracted = page.extract_tables() or []
            for table_index, table in enumerate(extracted, start=1):
                if not table:
                    continue
                normalized_rows = [_normalize_row(row or []) for row in table]
                non_empty_rows = [
                    row for row in normalized_rows if any(str(cell or "").strip() for cell in row)
                ]
                if not non_empty_rows:
                    continue
                header = non_empty_rows[0]
                body = non_empty_rows[1:] if len(non_empty_rows) > 1 else []
                width = max((len(row) for row in non_empty_rows), default=0)
                header = header + [""] * (width - len(header))
                body = [row + [""] * (width - len(row)) for row in body]
                matrix = [header, *body]
                tables.append(
                    {
                        "table_id": f"page_{page_index}_table_{table_index}",
                        "page": page_index,
                        "table_index": table_index,
                        "row_count": len(matrix),
                        "column_count": width,
                        "header": header,
                        "rows": body,
                        "matrix": matrix,
                        "markdown": _to_markdown(matrix),
                        "extraction_method": "pdfplumber_default",
                        "column_alignment": {
                            "status": "aligned",
                            "column_count": width,
                            "header_row_count": 1,
                            "method": "pdfplumber_grid",
                        },
                    }
                )
                methods.add("pdfplumber_default")

            fallback_tables = _extract_horizontal_rule_tables(page, page_index)
            if fallback_tables and (not extracted or len(fallback_tables) > len(extracted)):
                if extracted:
                    tables = [item for item in tables if int(item.get("page") or 0) != page_index]
                tables.extend(fallback_tables)
                methods.add("horizontal_rule_layout")
                warnings.append(
                    f"page {page_index}: default table grid extraction was incomplete; "
                    "used horizontal-rule layout fallback"
                )

    return {
        "schema_version": 2,
        "pdf_path": str(pdf_path),
        "table_count": len(tables),
        "tables": tables,
        "extraction_methods": sorted(methods),
        "warnings": warnings,
    }


def _to_markdown(rows):
    if not rows:
        return ""
    width = max((len(row) for row in rows), default=0)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    separator = ["---"] * width
    body = normalized[1:] if len(normalized) > 1 else []
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 2:
        print(
            json.dumps(
                {
                    "error": "Usage: extract_pdf_tables.py <pdf_path>",
                },
                ensure_ascii=False,
            )
        )
        return 1

    pdf_path = Path(sys.argv[1]).expanduser().resolve()
    if not pdf_path.is_file():
        print(
            json.dumps(
                {
                    "error": f"PDF not found: {pdf_path}",
                },
                ensure_ascii=False,
            )
        )
        return 2

    result = extract_tables(pdf_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
