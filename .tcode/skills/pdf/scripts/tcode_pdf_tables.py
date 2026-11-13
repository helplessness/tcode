"""TCode adapter: format detected table grids for the existing evidence flow."""

from tcode_pdf_actions import write_result


def table_result(path):
    import pdfplumber

    tables = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for index, table in enumerate(page.find_tables(), 1):
                matrix = [
                    [" ".join((cell or "").split()) for cell in row]
                    for row in table.extract()
                ]
                matrix = [row for row in matrix if any(row)]
                if not matrix:
                    continue
                header, *rows = matrix
                width = len(header)
                markdown = "\n".join(
                    "| " + " | ".join(cell.replace("|", r"\|") for cell in row) + " |"
                    for row in [header, ["---"] * width, *rows]
                )
                tables.append(dict(
                    table_id=f"page_{page.page_number}_table_{index}",
                    page=page.page_number, table_index=index, row_count=len(matrix),
                    column_count=width, header=header, rows=rows, matrix=matrix,
                    markdown=markdown, extraction_method="pdfplumber_default",
                    bbox=list(table.bbox), bbox_coordinate_system="pdfplumber_top_left_points",
                    page_width=page.width, page_height=page.height,
                ))
    return dict(
        schema_version=2, pdf_path=str(path), table_count=len(tables), tables=tables,
        extraction_methods=["pdfplumber_default"] if tables else [],
        warnings=[] if tables else ["No table grids detected; inspect page images for unruled tables."],
    )


if __name__ == "__main__":
    write_result(table_result)
