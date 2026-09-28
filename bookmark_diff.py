"""しおり構成の差分(追加された章・削除された章・名前が変わった章)。

新旧比較画面で、旧版と新版のしおり構成の違いを一覧にする。ページ番号ではなく、しおりパス(祖先の見出し+自分の見出し)を軸に比較する。

差分は「見るべき場所の当たりをつける補助」であり、検査の根拠にはしない。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from matching import normalize_title, strip_page_suffix

if TYPE_CHECKING:
    from pdf_sections import DocSection

RENAME_THRESHOLD = 0.6  # match_anchorのFUZZY_THRESHOLDより緩め(一覧表示用の当たりをつける用途のため)


def full_path(section: "DocSection") -> str:
    return f"{section.path_titles} › {section.title}" if section.path_titles else section.title


def section_key(section: "DocSection") -> str:
    """旧新で同じ章かを判定するためのキー。KojiPDFのページ番号の付記(_開始_ページ数)を除いたしおりパス。"""
    return strip_page_suffix(full_path(section))


def keyed_sections(sections: list["DocSection"]) -> dict[str, "DocSection"]:
    """section_key → 章。付記を除くと同名になる章(例: 同じ親の下の「●」が複数)は、
    文書内で何番目に現れたかを付けて区別する(旧新とも同じ順に並んでいれば同じキーになる)。"""
    seen: dict[str, int] = {}
    out: dict[str, "DocSection"] = {}
    for s in sections:
        key = section_key(s)
        n = seen.get(key, 0)
        seen[key] = n + 1
        out[key if n == 0 else f"{key} #{n + 1}"] = s
    return out


@dataclass
class SectionDiff:
    added: list       # DocSection。新版のみに存在
    removed: list      # DocSection。旧版のみに存在
    renamed: list       # (旧DocSection, 新DocSection)のタプル。見出しが変わったと推定されるもの


def diff_sections(old_sections: list["DocSection"], new_sections: list["DocSection"]) -> SectionDiff:
    old_by_path = keyed_sections(old_sections)
    new_by_path = keyed_sections(new_sections)

    common = old_by_path.keys() & new_by_path.keys()
    old_only = [p for p in old_by_path if p not in common]
    new_only = [p for p in new_by_path if p not in common]

    renamed: list[tuple] = []
    matched_new: set[str] = set()
    for op in list(old_only):
        best_path, best_ratio = None, 0.0
        for np in new_only:
            if np in matched_new:
                continue
            ratio = difflib.SequenceMatcher(None, normalize_title(op), normalize_title(np)).ratio()
            if ratio > best_ratio:
                best_ratio, best_path = ratio, np
        if best_path is not None and best_ratio >= RENAME_THRESHOLD:
            renamed.append((old_by_path[op], new_by_path[best_path]))
            matched_new.add(best_path)
            old_only.remove(op)

    new_only = [p for p in new_only if p not in matched_new]

    return SectionDiff(
        added=[new_by_path[p] for p in new_only],
        removed=[old_by_path[p] for p in old_only],
        renamed=renamed,
    )
