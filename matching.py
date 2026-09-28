"""再提出版PDFに対する指摘アンカーの照合。

指摘の位置(アンカー)をページ番号ではなくしおりパスで持つため、再提出版では次の順で照合する。

1. しおりパスが完全一致 → 同じ箇所とみなす
2. 一致しない → しおり名のあいまい一致(表記ゆれ・枝番の変化を吸収)
3. それでも不明 → 近傍テキストを新版の全文から検索
4. すべて失敗 → 「照合できず」として明示する(推測で別の場所を指さない)
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from indications import Anchor

if TYPE_CHECKING:
    from pdf_sections import DocSection

FUZZY_THRESHOLD = 0.72
MIN_SNIPPET_LEN = 6

_NUMBERING_RE = re.compile(
    r"^[\s　]*([0-9０-９]+[.．、)）]|第[0-9０-９一二三四五六七八九十]+[章節項]|[①-⑳])[\s　]*"
)


# KojiPDFが付けるしおり名末尾の「_開始ページ_ページ数」(例: "京都駅周辺_77_1")。
# 再提出でページが増減すると、見出しそのものは同じでもこの部分だけが変わる。
_PAGE_SUFFIX_RE = re.compile(r"_\d+_\d+(?=\s*(?: › |$))")


def strip_page_suffix(title_or_path: str) -> str:
    """しおり名(またはパンくず)から、KojiPDFのページ番号の付記を取り除く。

    付記は見出しの一部ではなくページ位置の記録なので、「しおりパスの完全一致」もこれを除いて判定する。
    除かないと、途中に1ページ挟まっただけで後ろの全章が「名称変更」扱いになってしまう。
    """
    return _PAGE_SUFFIX_RE.sub("", title_or_path or "")


def normalize_title(title: str) -> str:
    """先頭の章番号・枝番などの表記ゆれを吸収する(あいまい一致の下地。完全一致の判定には使わない)。

    bookmark_diff.pyの名称変更検出でも同じ正規化を使う。
    """
    stripped = _NUMBERING_RE.sub("", strip_page_suffix(title))
    return re.sub(r"\s+", "", stripped).strip()


@dataclass
class MatchResult:
    status: str  # "exact" | "fuzzy" | "text" | "unresolved"
    section: "DocSection | None"
    note: str = ""


def match_anchor(anchor: Anchor, sections: list["DocSection"]) -> MatchResult:
    # 1. しおりパスの完全一致(KojiPDFのページ番号の付記は除いて比べる)
    target_path = strip_page_suffix(anchor.path_titles)
    target_title = strip_page_suffix(anchor.section_title)
    for s in sections:
        if strip_page_suffix(s.path_titles) == target_path and strip_page_suffix(s.title) == target_title:
            return MatchResult("exact", s)

    # 2. あいまい一致(正規化した見出しの類似度)
    target_full = normalize_title(anchor.full_path)
    best: "DocSection | None" = None
    best_ratio = 0.0
    for s in sections:
        full = f"{s.path_titles} › {s.title}" if s.path_titles else s.title
        ratio = difflib.SequenceMatcher(None, target_full, normalize_title(full)).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, s
    if best is not None and best_ratio >= FUZZY_THRESHOLD:
        return MatchResult("fuzzy", best, note=f"類似度 {best_ratio:.2f}")

    # 3. 近傍テキストの全文検索(しおり名が変わってしまった場合の保険)
    snippet = (anchor.nearby_text or "").strip()
    if len(snippet) >= MIN_SNIPPET_LEN:
        for s in sections:
            if snippet in (s.body_text or ""):
                return MatchResult("text", s, note="近傍テキスト一致")

    # 4. 照合できず。握りつぶさず、はっきり「不明」として返す。
    return MatchResult("unresolved", None, note="一致するしおりが見つかりませんでした")
