"""差分の可視化(ページ単位の文字比較・画素差)。

機能:
- テキストPDF: 文字を比較し、変更箇所を色付け
- 画像PDF: ページを重ねて画素差を表示
- 「変更のあった箇所」だけを順に送るナビゲーション

旧新のページの対応は、ページ番号ではなく「しおり区間+区間内の相対ページ」で決める(pair_pages)。

1ページの比較(PageDiffer.diff)は次の2本立て:
- 文字差分: 双方のページから1文字ずつの文字と位置を取り出し、difflibで比較する。
  日本語は単語区切りがないため、単語単位ではなく文字単位で比べる。
- 画素差: 双方を小さな格子(縮小画像)に描き、濃さの差が大きいマスを差分とする。
  **文字のある領域は除外する**(文字は文字差分で見ており、改行位置がずれただけで
  全面が差分になるのを避けるため)。写真の差し替えのように文字が同じままの変更を拾う役割。

差分は「見るべき場所の当たりをつける補助」であり、検査の根拠ではない。
スキャン資料はずれ・傾き・濃度の違いで誤検出が出やすく、逆に「差分なし」でも
修正されていないとは限らない。この前提は画面側で常に明示する。
"""
from __future__ import annotations

import difflib
import hashlib
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import fitz

from bookmark_diff import SectionDiff, keyed_sections

if TYPE_CHECKING:
    from pdf_sections import DocSection

GRID_W = 120                # 画素差の格子の横マス数(縦はページ比率に合わせる)
PIXEL_THRESHOLD = 48        # 0〜255の濃さの差がこれを超えたマスを差分とみなす
MIN_REGION_CELLS = 2        # これより小さい塊はノイズとして捨てる
REGION_JOIN_CELLS = 4      # このマス数以内に近接する変更マスは1つの塊(矩形)にまとめる
TEXT_MIN_CHARS = 20         # 双方のページにこれ以上の文字があれば文字差分を行う
MAX_DIFF_CHARS = 30000      # これを超える文字数のページは文字差分を省略する(処理時間対策)
PAGE_MATCH_THRESHOLD = 0.5  # 区間内のページ対応付けで「同じページ」とみなす類似度の下限
IDENTICAL_SIMILARITY = 0.999  # 区間の先頭・末尾を「同一ページ」として先に対応させる類似度
MAX_ALIGN_CELLS = 40000    # 旧ページ数×新ページ数がこれを超える区間は内容での対応付けを諦める
THUMB_W = 24                # ページ類似度を画像で見るときの縮小画像の横幅

# 相対座標(0〜1)の矩形: (x0, y0, x1, y1)
RelRect = tuple[float, float, float, float]


# --------------------------------------------------------------------- ページの対応付け
@dataclass
class PagePair:
    old_page: int | None    # 1始まり。Noneは旧版に対応ページなし(追加されたページ)
    new_page: int | None    # 1始まり。Noneは新版に対応ページなし(削除されたページ)


def _deepest_section(sections: list["DocSection"], page: int) -> "DocSection | None":
    best = None
    for s in sections:
        if s.start_page <= page <= s.end_page:
            if best is None or (s.level, s.start_page) >= (best.level, best.start_page):
                best = s
    return best


def _own_pages(sections: list["DocSection"], sec: "DocSection", count: int) -> list[int]:
    """区間secに属するページのうち、より深い子区間に属さないもの(=secが最も深い区間であるページ)。"""
    return [p for p in range(sec.start_page, min(sec.end_page, count) + 1)
            if _deepest_section(sections, p) is sec]


def _align(old_pages: list[int], new_pages: list[int],
           similarity: Callable[[int, int], float] | None) -> dict[int, int]:
    """同じ区間の旧新ページ列を対応付ける。

    ページ数が同じなら先頭から順に対応させる(大半の再提出はこれ)。ページ数が違う場合は、
    途中でページが挿入・削除されたとみなし、ページ内容の似ている度合いで順序を保った
    対応を取る(重み付きLCS)。似ていないページ同士は対応させない(推測で指さない)。
    """
    if len(old_pages) == len(new_pages) or similarity is None:
        return {o: n for o, n in zip(old_pages, new_pages)} if len(old_pages) == len(new_pages) else {}

    # 先頭・末尾から同一内容のページを先に対応させ、全組み合わせの比較は真ん中の残りだけで行う
    # (大きな区間に1ページ挟まっただけ、という典型的な再提出を速く処理するため)
    mapping: dict[int, int] = {}
    while old_pages and new_pages and similarity(old_pages[0], new_pages[0]) >= IDENTICAL_SIMILARITY:
        mapping[old_pages[0]] = new_pages[0]
        old_pages, new_pages = old_pages[1:], new_pages[1:]
    while old_pages and new_pages and similarity(old_pages[-1], new_pages[-1]) >= IDENTICAL_SIMILARITY:
        mapping[old_pages[-1]] = new_pages[-1]
        old_pages, new_pages = old_pages[:-1], new_pages[:-1]
    if not old_pages or not new_pages:
        return mapping
    if len(old_pages) == len(new_pages):
        mapping.update(zip(old_pages, new_pages))
        return mapping
    mapping.update(_align_by_similarity(old_pages, new_pages, similarity))
    return mapping


def _align_by_similarity(old_pages: list[int], new_pages: list[int],
                         similarity: Callable[[int, int], float]) -> dict[int, int]:
    n_old, n_new = len(old_pages), len(new_pages)
    if n_old * n_new > MAX_ALIGN_CELLS:
        return {}
    sim = [[similarity(o, n) for n in new_pages] for o in old_pages]
    # score[i][j] = old[i:]とnew[j:]の最良の合計類似度
    score = [[0.0] * (n_new + 1) for _ in range(n_old + 1)]
    for i in range(n_old - 1, -1, -1):
        for j in range(n_new - 1, -1, -1):
            best = max(score[i + 1][j], score[i][j + 1])
            if sim[i][j] >= PAGE_MATCH_THRESHOLD:
                best = max(best, sim[i][j] + score[i + 1][j + 1])
            score[i][j] = best
    mapping: dict[int, int] = {}
    i = j = 0
    while i < n_old and j < n_new:
        if sim[i][j] >= PAGE_MATCH_THRESHOLD and score[i][j] == sim[i][j] + score[i + 1][j + 1]:
            mapping[old_pages[i]] = new_pages[j]
            i += 1
            j += 1
        elif score[i][j] == score[i + 1][j]:
            i += 1
        else:
            j += 1
    return mapping


def pair_pages(old_sections: list["DocSection"], new_sections: list["DocSection"],
               old_count: int, new_count: int, diff: SectionDiff,
               similarity: Callable[[int, int], float] | None = None) -> list[PagePair]:
    """旧新のページを、しおり区間+区間内の位置で対応付ける。

    - 旧ページが属する最も深いしおり区間を探し、新版の同じ区間(しおりパス完全一致、
      またはbookmark_diffが「名前が変わった章」と推定したもの)の中で対応させる(_align)
    - 対応する区間が新版にない、または区間内で対応が取れないページは「対応なし」
      (推測で別のページを指さない)
    - しおりより前のページ(表紙など)は、双方にある範囲で先頭から順に対応させる
    - 旧版のどのページとも対応しなかった新ページは「追加されたページ」として間に挟む
    """
    new_by_path = keyed_sections(new_sections)
    old_keys = {id(s): k for k, s in keyed_sections(old_sections).items()}
    renamed_to = {old_keys[id(o)]: n for o, n in diff.renamed}

    old_first = min((s.start_page for s in old_sections), default=old_count + 1)
    new_first = min((s.start_page for s in new_sections), default=new_count + 1)

    mapping: dict[int, int] = {}
    for p in range(1, min(old_first, new_first, old_count + 1, new_count + 1)):
        mapping[p] = p
    for sec in old_sections:
        key = old_keys[id(sec)]
        target = new_by_path.get(key) or renamed_to.get(key)
        if target is None:
            continue
        mapping.update(_align(_own_pages(old_sections, sec, old_count),
                              _own_pages(new_sections, target, new_count), similarity))

    pairs: list[PagePair] = []
    used_new: set[int] = set()
    for op in range(1, old_count + 1):
        np_ = mapping.get(op)
        if np_ is not None and np_ in used_new:
            np_ = None
        if np_ is not None:
            used_new.add(np_)
        pairs.append(PagePair(op, np_))

    # 対応しなかった新ページを、新ページ番号の順を保つ位置へ挟み込む
    unpaired_new = [p for p in range(1, new_count + 1) if p not in used_new]
    merged: list[PagePair] = []
    k = 0
    for pair in pairs:
        if pair.new_page is not None:
            while k < len(unpaired_new) and unpaired_new[k] < pair.new_page:
                merged.append(PagePair(None, unpaired_new[k]))
                k += 1
        merged.append(pair)
    merged.extend(PagePair(None, p) for p in unpaired_new[k:])
    return merged


# --------------------------------------------------------------------- 1ページの比較
@dataclass
class PageDiffResult:
    old_rects: list[RelRect] = field(default_factory=list)          # 旧版側の文字の変更箇所
    new_rects: list[RelRect] = field(default_factory=list)          # 新版側の文字の変更箇所
    image_rects: list[RelRect] = field(default_factory=list)        # 画素差(旧新共通の位置)
    text_changes: int = 0       # 文字の変更のまとまり数
    text_compared: bool = False  # 文字差分を行ったか(文字の少ないページ・スキャン資料では行わない)
    # 片方にだけ文字情報がある(文字のページがスキャン画像に置き換わった、またはその逆)
    text_layer_changed: str = ""  # "lost"(新版で文字情報がなくなった) / "gained" / ""
    note: str = ""

    @property
    def has_changes(self) -> bool:
        return bool(self.text_changes or self.image_rects or self.text_layer_changed)


@dataclass
class _Char:
    c: str
    rect: RelRect
    line_key: tuple[int, int]


def _page_chars(page: "fitz.Page") -> list[_Char]:
    """ページ上の文字を読み順に、相対座標付きで取り出す(空白は比較対象にしない)。"""
    w, h = page.rect.width, page.rect.height
    if w <= 0 or h <= 0:
        return []
    ox, oy = page.rect.x0, page.rect.y0
    chars: list[_Char] = []
    # 画像ブロックは不要(含めると画像データの読み出しで大幅に遅くなる)
    raw = page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)
    for b_no, block in enumerate(raw.get("blocks", [])):
        for l_no, line in enumerate(block.get("lines", [])):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    # 全角/半角などの表記の違い(「１」と「1」)は差分として扱わない。
                    # 作成ソフトの違いで変わりやすく、検査上は意味のない差のため
                    c = unicodedata.normalize("NFKC", ch.get("c", ""))
                    if not c or c.isspace():
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    chars.append(_Char(c, ((x0 - ox) / w, (y0 - oy) / h, (x1 - ox) / w, (y1 - oy) / h),
                                       (b_no, l_no)))
    return chars


def _merge_char_rects(chars: list[_Char]) -> list[RelRect]:
    """連続する変更文字を、同じ行の中で1つの矩形にまとめる。"""
    rects: list[RelRect] = []
    cur: list[float] | None = None
    cur_key = None
    for ch in chars:
        if cur is not None and ch.line_key == cur_key:
            cur[0] = min(cur[0], ch.rect[0])
            cur[1] = min(cur[1], ch.rect[1])
            cur[2] = max(cur[2], ch.rect[2])
            cur[3] = max(cur[3], ch.rect[3])
        else:
            if cur is not None:
                rects.append(tuple(cur))
            cur, cur_key = list(ch.rect), ch.line_key
    if cur is not None:
        rects.append(tuple(cur))
    return rects


def _gray_grid(page: "fitz.Page", cols: int, rows: int) -> bytes:
    """ページを cols×rows の灰色画像に縮小して描く。縮小時の平均化で細かなずれを吸収する。"""
    r = page.rect
    matrix = fitz.Matrix(cols / r.width, rows / r.height)
    pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csGRAY, alpha=False)
    if pix.width == cols and pix.height == rows and pix.stride == cols:
        return bytes(pix.samples)
    # 丸めで1px程度ずれた場合は、最近傍で cols×rows に詰め直す
    out = bytearray(cols * rows)
    samples = pix.samples
    for y in range(rows):
        sy = min(pix.height - 1, y * pix.height // rows)
        for x in range(cols):
            sx = min(pix.width - 1, x * pix.width // cols)
            out[y * cols + x] = samples[sy * pix.stride + sx]
    return bytes(out)


def _text_mask(chars: list[_Char], cols: int, rows: int, mask: bytearray):
    for ch in chars:
        x0, y0, x1, y1 = ch.rect
        cx0, cx1 = max(0, int(x0 * cols) - 1), min(cols - 1, int(x1 * cols) + 1)
        cy0, cy1 = max(0, int(y0 * rows) - 1), min(rows - 1, int(y1 * rows) + 1)
        for y in range(cy0, cy1 + 1):
            row = y * cols
            for x in range(cx0, cx1 + 1):
                mask[row + x] = 1


def _pixel_diff_regions(a: bytes, b: bytes, cols: int, rows: int, mask: bytearray) -> list[RelRect]:
    """格子同士を比べ、差の大きいマスの塊を矩形で返す。

    旧側の各マスを、新側の同じマスと上下左右1マス以内のうち最も近い濃さと比べる
    (わずかな位置ずれで輪郭が全部差分になるのを抑える)。逆向き(新→旧)も同様に見る。
    """
    changed = bytearray(cols * rows)

    def mark(src: bytes, dst: bytes):
        for y in range(rows):
            y0, y1 = max(0, y - 1), min(rows - 1, y + 1)
            for x in range(cols):
                i = y * cols + x
                if mask[i]:
                    continue
                v = src[i]
                if abs(v - dst[i]) <= PIXEL_THRESHOLD:
                    continue
                x0, x1 = max(0, x - 1), min(cols - 1, x + 1)
                best = min(abs(v - dst[yy * cols + xx]) for yy in range(y0, y1 + 1) for xx in range(x0, x1 + 1))
                if best > PIXEL_THRESHOLD:
                    changed[i] = 1

    mark(a, b)
    mark(b, a)

    # 塊ごとに外接矩形にまとめる。REGION_JOIN_CELLSマス以内に近接する変更マスは同じ塊とみなす
    # (写真の差し替えなどが細かい矩形に砕けて、画面が枠だらけになるのを防ぐ)
    boxes: list[list[int]] = []  # マス単位の外接矩形 [x0, y0, x1, y1](両端含む)
    seen = bytearray(cols * rows)
    for start in range(cols * rows):
        if not changed[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        cells = 0
        minx = miny = 10 ** 9
        maxx = maxy = -1
        while stack:
            i = stack.pop()
            cells += 1
            y, x = divmod(i, cols)
            minx, maxx, miny, maxy = min(minx, x), max(maxx, x), min(miny, y), max(maxy, y)
            for yy in range(y - REGION_JOIN_CELLS, y + REGION_JOIN_CELLS + 1):
                if 0 <= yy < rows:
                    for xx in range(x - REGION_JOIN_CELLS, x + REGION_JOIN_CELLS + 1):
                        if 0 <= xx < cols:
                            j = yy * cols + xx
                            if changed[j] and not seen[j]:
                                seen[j] = 1
                                stack.append(j)
        if cells >= MIN_REGION_CELLS:
            boxes.append([minx, miny, maxx, maxy])

    # 近接・重なり合う外接矩形同士もまとめる(連結しきれなかった断片の後始末)
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                j_ = REGION_JOIN_CELLS + 1  # 隙間がREGION_JOIN_CELLSマス以下ならまとめる
                if (a[0] - j_ <= b[2] and b[0] - j_ <= a[2]
                        and a[1] - j_ <= b[3] and b[1] - j_ <= a[3]):
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return [(x0 / cols, y0 / rows, (x1 + 1) / cols, (y1 + 1) / rows) for x0, y0, x1, y1 in boxes]


class PageDiffer:
    """旧新2つのPDFを開いたまま持ち、ページ対の差分を計算してキャッシュする。"""

    def __init__(self, old_path: str, new_path: str):
        self.old_doc = fitz.open(old_path)
        self.new_doc = fitz.open(new_path)
        self._cache: dict[tuple[int | None, int | None], PageDiffResult] = {}
        self._chars_cache: dict[tuple[str, int], object] = {}  # 文字列(list[_Char])と縮小画像(bytes)

    def close(self):
        self.old_doc.close()
        self.new_doc.close()

    def _chars(self, side: str, page_1based: int) -> list[_Char]:
        key = (side, page_1based)
        if key not in self._chars_cache:
            doc = self.old_doc if side == "old" else self.new_doc
            self._chars_cache[key] = _page_chars(doc[page_1based - 1])
        return self._chars_cache[key]

    def _grid(self, side: str, page_1based: int, cols: int, rows: int) -> bytes:
        """ページの灰色格子。描画は重い(特に大きな画像を含むページ)ので、同じ寸法なら使い回す。"""
        key = (f"grid-{side}-{cols}x{rows}", page_1based)
        if key not in self._chars_cache:
            doc = self.old_doc if side == "old" else self.new_doc
            self._chars_cache[key] = _gray_grid(doc[page_1based - 1], cols, rows)
        return self._chars_cache[key]

    def _thumb(self, side: str, page_1based: int) -> bytes:
        """類似度用の小さな縮小画像。差分用の格子(GRID_W幅)を間引いて作り、描画を1回で済ませる。"""
        key = ("thumb-" + side, page_1based)
        if key not in self._chars_cache:
            doc = self.old_doc if side == "old" else self.new_doc
            r = doc[page_1based - 1].rect
            rows = max(1, round(GRID_W * r.height / r.width)) if r.width > 0 else GRID_W
            grid = self._grid(side, page_1based, GRID_W, rows)
            step = GRID_W // THUMB_W
            t_rows = rows // step
            self._chars_cache[key] = bytes(grid[(y * step) * GRID_W + x * step]
                                           for y in range(t_rows) for x in range(THUMB_W))
        return self._chars_cache[key]

    def _bigrams(self, side: str, page_1based: int) -> Counter:
        key = ("bigram-" + side, page_1based)
        if key not in self._chars_cache:
            s = "".join(c.c for c in self._chars(side, page_1based))
            self._chars_cache[key] = Counter(s[i:i + 2] for i in range(len(s) - 1))
        return self._chars_cache[key]

    def page_similarity(self, old_page: int, new_page: int) -> float:
        """区間内のページ対応付け(pair_pagesのsimilarity)に使う、0〜1の類似度。

        双方に十分な文字があれば「2文字の組」の重なり(Dice係数)で、なければ縮小画像の濃さで比べる。
        大きな区間では全ページ対を比べるため、difflibより桁違いに軽い方法にしている。
        """
        if self._identical(old_page, new_page):
            return 1.0
        a, b = self._chars("old", old_page), self._chars("new", new_page)
        if len(a) >= TEXT_MIN_CHARS and len(b) >= TEXT_MIN_CHARS:
            ba, bb = self._bigrams("old", old_page), self._bigrams("new", new_page)
            total = sum(ba.values()) + sum(bb.values())
            return 2 * sum((ba & bb).values()) / total if total else 0.0
        ta, tb = self._thumb("old", old_page), self._thumb("new", new_page)
        if len(ta) != len(tb) or not ta:
            return 0.0
        return 1.0 - sum(abs(x - y) for x, y in zip(ta, tb)) / (255 * len(ta))

    def _fingerprint(self, side: str, page_1based: int) -> str:
        """ページの中身の指紋: 描画命令(コンテンツストリーム)+参照している画像・フォームの生データ。

        画像は展開せず圧縮されたままのバイト列を使うので速い。再提出版の大半のページは旧版と
        まったく同じ中身のため、指紋が一致すれば重い比較(文字の取り出し・描画)を丸ごと省ける。
        一致しない場合は通常どおり比較する(指紋の違い=差分、とはみなさない)。
        """
        key = ("fp-" + side, page_1based)
        if key not in self._chars_cache:
            doc = self.old_doc if side == "old" else self.new_doc
            page = doc[page_1based - 1]
            h = hashlib.sha1()
            h.update(repr((tuple(page.rect), page.rotation)).encode())
            h.update(page.read_contents())
            for img in page.get_images(full=True):
                h.update(b"img")
                h.update(doc.xref_stream_raw(img[0]) or b"")
            for xo in page.get_xobjects():
                h.update(b"form")
                h.update(doc.xref_stream(xo[0]) or b"")
            self._chars_cache[key] = h.hexdigest()
        return self._chars_cache[key]

    def _identical(self, old_page: int, new_page: int) -> bool:
        try:
            return self._fingerprint("old", old_page) == self._fingerprint("new", new_page)
        except Exception:  # noqa: BLE001  指紋が取れない壊れたページ等は、通常の比較に回す
            return False

    def diff(self, old_page: int | None, new_page: int | None) -> PageDiffResult:
        key = (old_page, new_page)
        if key not in self._cache:
            self._cache[key] = self._compute(old_page, new_page)
        return self._cache[key]

    def _compute(self, old_page: int | None, new_page: int | None) -> PageDiffResult:
        if old_page is None and new_page is None:
            return PageDiffResult()
        if old_page is None:
            return PageDiffResult(new_rects=[(0.0, 0.0, 1.0, 1.0)], text_changes=1,
                                  note="旧版に対応するページがありません(追加されたページ)")
        if new_page is None:
            return PageDiffResult(old_rects=[(0.0, 0.0, 1.0, 1.0)], text_changes=1,
                                  note="新版に対応するページがありません(削除されたページ)")

        if self._identical(old_page, new_page):
            return PageDiffResult(note="旧版と同一内容のページです")

        result = PageDiffResult()
        old_chars = self._chars("old", old_page)
        new_chars = self._chars("new", new_page)

        # --- 文字差分
        if (len(old_chars) >= TEXT_MIN_CHARS and len(new_chars) >= TEXT_MIN_CHARS
                and len(old_chars) + len(new_chars) <= MAX_DIFF_CHARS):
            result.text_compared = True
            sm = difflib.SequenceMatcher(None, [c.c for c in old_chars], [c.c for c in new_chars],
                                         autojunk=False)
            old_changed: list[_Char] = []
            new_changed: list[_Char] = []
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    continue
                result.text_changes += 1
                old_changed.extend(old_chars[i1:i2])
                new_changed.extend(new_chars[j1:j2])
            result.old_rects = _merge_char_rects(old_changed)
            result.new_rects = _merge_char_rects(new_changed)
        elif len(old_chars) >= TEXT_MIN_CHARS and not new_chars:
            # 見た目が似ていても、ページが差し替えられたことを示す重要な変化(再スキャン・画像化など)
            result.text_layer_changed = "lost"
            result.note = "新版では文字情報がありません(スキャン画像などに置き換わった可能性)。文字の比較は行っていません"
        elif len(new_chars) >= TEXT_MIN_CHARS and not old_chars:
            result.text_layer_changed = "gained"
            result.note = "旧版には文字情報がありません(スキャン画像から置き換わった可能性)。文字の比較は行っていません"
        elif old_chars or new_chars:
            result.note = "文字が少ない(または多すぎる)ため、文字の比較は行っていません"

        # --- 画素差(文字のある領域を除く)
        old_p, new_p = self.old_doc[old_page - 1], self.new_doc[new_page - 1]
        if old_p.rect.width > 0 and old_p.rect.height > 0 and new_p.rect.width > 0 and new_p.rect.height > 0:
            cols = GRID_W
            rows = max(1, round(GRID_W * old_p.rect.height / old_p.rect.width))
            mask = bytearray(cols * rows)
            if result.text_compared:
                _text_mask(old_chars, cols, rows, mask)
                _text_mask(new_chars, cols, rows, mask)
            a = self._grid("old", old_page, cols, rows)
            b = self._grid("new", new_page, cols, rows)
            result.image_rects = _pixel_diff_regions(a, b, cols, rows, mask)
        return result
