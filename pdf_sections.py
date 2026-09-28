"""しおり(TOC)付きPDFを「資料の区切り(しおり区間)」の階層として読み取る。

mail-pdf-viewer(https://github.com/ukamoo-rinyu/mail-pdf-viewer)の parser.py から、
このビューアーに必要な部分(一般資料PDFのしおり階層の抽出と、メール束PDFの判定)だけを
取り込んだもの。

しおりの読み取り結果はファイルに保存しない(元アプリは PDF の横に索引ファイルを作るが、
検査対象の PDF が置かれた共有フォルダに余計なファイルを残さないため)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

# KojiPDF のメール束PDFで、各メールの1ページ目に並ぶ固定ラベル
_MAIL_LABELS = ["件名", "差出人", "宛先", "CC", "添付ファイル", "受信日時", "送信日時"]


@dataclass
class DocSection:
    """しおり1エントリ分の区切り。

    しおりが実際に指すページ番号と、次のしおり出現位置から算出した終了ページを持つ。
    """
    index: int
    parent_index: int | None
    level: int
    title: str
    start_page: int
    end_page: int
    path_titles: str  # 祖先のタイトルを " › " で連結したパンくず
    body_text: str    # 子を持たない区間の全文(照合の保険に使う)。include_body=Falseなら空


def extract_document_sections(pdf_path: str, include_body: bool = True) -> list[DocSection]:
    """しおりを階層構造のまま「資料の区切り」として抽出する。

    終了ページは、次に現れる同レベル以上のしおりの直前ページとする。
    子を持つ見出し(章など)には本文を持たせない(子の区間側が持つため、二重に持たせない)。
    """
    doc = pymupdf.open(pdf_path)
    try:
        toc = doc.get_toc(simple=False)
        total_pages = doc.page_count

        ends: list[int] = []
        for i, (level, _title, page, _extra) in enumerate(toc):
            end = total_pages
            for j in range(i + 1, len(toc)):
                if toc[j][0] <= level:
                    end = toc[j][2] - 1
                    break
            ends.append(max(end, page))

        sections: list[DocSection] = []
        stack: list[tuple[int, int]] = []  # (level, sections内インデックス)
        for i, (level, title, page, _extra) in enumerate(toc):
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent_index = stack[-1][1] if stack else None
            path_titles = " › ".join(sections[idx].title for _, idx in stack)

            node_index = len(sections)
            sections.append(DocSection(
                index=node_index, parent_index=parent_index, level=level,
                title=title.strip() or f"(無題 {node_index + 1})",
                start_page=page, end_page=ends[i],
                path_titles=path_titles, body_text="",
            ))
            stack.append((level, node_index))

        if include_body:
            has_child = [False] * len(sections)
            for s in sections:
                if s.parent_index is not None:
                    has_child[s.parent_index] = True
            for i, s in enumerate(sections):
                if not has_child[i]:
                    s.body_text = "\n".join(doc[p - 1].get_text() for p in range(s.start_page, s.end_page + 1))

        return sections
    finally:
        doc.close()


# --------------------------------------------------------------------- メール束PDFの判定
def _title_matches_mail_pattern(title: str) -> bool:
    parts = title.rsplit("_", 2)
    return len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit()


def _mail_header_hits(page_text: str) -> int:
    """ページ先頭に「件名/差出人/受信日時」のラベルと値が揃っている数。"""
    stripped = [line.strip() for line in page_text.split("\n")]
    positions = []
    seen: set[str] = set()
    for i, line in enumerate(stripped):
        if line in _MAIL_LABELS and line not in seen:
            positions.append((line, i))
            seen.add(line)
    positions.sort(key=lambda x: x[1])
    hits = 0
    for idx, (label, pos) in enumerate(positions):
        end = positions[idx + 1][1] if idx + 1 < len(positions) else pos + 2
        if label in ("件名", "差出人", "受信日時") and any(stripped[j] for j in range(pos + 1, end)):
            hits += 1
    return hits


def detect_mode(pdf_path: str) -> str:
    """しおりの構造から「メール束PDF("mail")」か「一般資料PDF("document")」かを判定する。

    ルートしおりの大半が "名前_開始ページ_ページ数" 形式で、かつ先頭メールの1ページ目に
    件名/差出人/受信日時のラベルが揃っていればメール束PDFとみなす。それ以外は一般資料PDF。
    """
    doc = pymupdf.open(pdf_path)
    try:
        roots = [t for t in doc.get_toc(simple=False) if t[0] == 1]
        if not roots:
            return "document"
        matched = sum(1 for _, title, _page, _extra in roots if _title_matches_mail_pattern(title))
        if matched / len(roots) < 0.8:
            return "document"
        first_page = roots[0][2]
        if not (1 <= first_page <= doc.page_count):
            return "document"
        return "mail" if _mail_header_hits(doc[first_page - 1].get_text()) >= 2 else "document"
    finally:
        doc.close()
