"""指摘一覧をExcel(.xlsx)として書き出す。

受注者に渡す指摘事項一覧の実物になる。
"""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from indications import Indication

_HEADERS = ["指摘番号", "区分", "状態", "しおりパス", "ページ(参考)", "指摘内容",
            "指摘者", "検査回次", "関連指摘", "記録日時"]
_COLUMN_WIDTHS = [8, 8, 8, 44, 10, 50, 10, 12, 10, 18]


def export(indications: list[Indication], dest_path: str, document_title: str = "") -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "指摘一覧"

    header_row = 1
    if document_title:
        ws.cell(row=1, column=1, value=document_title).font = Font(bold=True, size=12)
        header_row = 2

    for col, text in enumerate(_HEADERS, start=1):
        cell = ws.cell(row=header_row, column=col, value=text)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2F6FE4")

    ordered = sorted(indications, key=lambda i: i.serial)
    for i, ind in enumerate(ordered, start=1):
        r = header_row + i
        a = ind.anchor
        values = [
            ind.serial, ind.category, ind.status, a.full_path,
            str(a.absolute_page) if a.absolute_page else "",
            ind.content, ind.inspector, ind.round_name, ind.related_id or "", ind.created_at,
        ]
        for col, v in enumerate(values, start=1):
            ws.cell(row=r, column=col, value=v)

    for col, width in enumerate(_COLUMN_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width

    last_row = header_row + len(ordered)
    if last_row > header_row:
        table_ref = f"A{header_row}:{get_column_letter(len(_HEADERS))}{last_row}"
        table = Table(displayName="Indications", ref=table_ref)
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(table)

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    wb.save(dest_path)
