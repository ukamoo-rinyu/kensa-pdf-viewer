"""京都市.pdf(旧版)から、再提出を模した新版 kyoto_new.pdf を作る。

仕込む変更(旧版のページ番号で記載):
  A. 文言修正   p9「4 つの役割」→「5 つの役割」 / p77「文字どおり」→「まさに」 / p125「不断に点検し」→「定期的に点検し」
  B. ページ追加 p30 の後ろに1枚(しおりのない大きな区間 honpen_02 の途中)
  C. ページ削除 p140(honpen_10 の途中)
  D. しおり名変更「（３）京都の特性をいかした都市づくり」→「（３）京都の特性を活かした都市づくり」
  E. 図の修正   p63 の図の一部を塗りつぶし(図の差し替えを模擬)
  F. 再スキャン p62(地図)と p100(文字ページ)を、わずかな傾き・ずれ・濃度差のある画像に置き換え
  G. しおり名末尾の「_開始ページ_ページ数」を新しいページ番号で振り直す(KojiPDFの出力を再現)
"""
import os
import re
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))  # 京都市.pdf などのある親フォルダ
OUT_DIR = os.path.join(HERE, "out")
os.makedirs(OUT_DIR, exist_ok=True)
SRC = os.path.join(ROOT, "京都市.pdf")
DST = os.path.join(OUT_DIR, "kyoto_new.pdf")

TEXT_EDITS = {9: ("4 つの役割", "5 つの役割"), 77: ("文字どおり", "まさに"), 125: ("不断に点検し", "定期的に点検し")}
INSERT_AFTER = 30
DELETE_PAGE = 140
RENAME = ("（３）京都の特性をいかした都市づくり", "（３）京都の特性を活かした都市づくり")
FIGURE_EDIT_PAGE = 63
RESCAN_PAGES = (62, 100)


def replace_text(page, old, new):
    rects = page.search_for(old)
    if not rects:
        raise SystemExit(f"p{page.number + 1}: 『{old}』が見つかりません")
    r = rects[0]
    # 元の文字の大きさに合わせる
    size = 10.5
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if fitz.Rect(s["bbox"]).intersects(r):
                    size = s["size"]
    page.add_redact_annot(r, fill=(1, 1, 1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    page.insert_text((r.x0, r.y1 - size * 0.15), new, fontname="japan", fontsize=size)


def rescan(doc, page_no):
    """ページを150dpiの画像にし、0.4度傾け・数px平行移動・濃度を少し変えて貼り直す。"""
    src = doc[page_no - 1]
    pix = src.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72).prerotate(0.4), colorspace=fitz.csGRAY)
    # 濃度差(全体を少し暗く)と平行移動は、貼り付け位置と画素値で表現する
    samples = bytearray(pix.samples)
    for i in range(len(samples)):
        samples[i] = max(0, samples[i] - 12)
    darker = fitz.Pixmap(fitz.csGRAY, pix.width, pix.height, bytes(samples), False)
    rect = src.rect
    new_page = doc.new_page(pno=page_no - 1, width=rect.width, height=rect.height)
    new_page.insert_image(fitz.Rect(3, 2, rect.width + 3, rect.height + 2), pixmap=darker)
    doc.delete_page(page_no)  # 元のページ(1つ後ろにずれている)を消す


def main():
    doc = fitz.open(SRC)
    toc = doc.get_toc(simple=False)
    old_count = doc.page_count

    # --- 旧版のページ番号のまま行う編集
    for p, (old, new) in TEXT_EDITS.items():
        replace_text(doc[p - 1], old, new)
    fig = doc[FIGURE_EDIT_PAGE - 1]
    imgs = fig.get_image_info()
    target = max((fitz.Rect(i["bbox"]) & fig.rect for i in imgs), key=lambda r: r.get_area(), default=fig.rect)
    cover = fitz.Rect(target.x0 + target.width * 0.55, target.y0 + target.height * 0.55,
                      target.x0 + target.width * 0.85, target.y0 + target.height * 0.85)
    fig.draw_rect(cover, color=(0.8, 0, 0), fill=(1, 0.85, 0.85), width=1.5)
    fig.insert_text((cover.x0 + 6, cover.y0 + 16), "区域変更", fontname="japan", fontsize=11, color=(0.8, 0, 0))
    for p in RESCAN_PAGES:
        rescan(doc, p)

    # --- ページ削除・追加(後ろから行い、前のページ番号をずらさない)
    doc.delete_page(DELETE_PAGE - 1)
    added = doc.new_page(pno=INSERT_AFTER, width=595, height=842)
    added.insert_text((60, 90), "【追加資料】補足説明", fontname="japan", fontsize=16)
    for i, line in enumerate(["前回検査の指摘を受け、本ページを追加しました。",
                              "都市計画の基本方針に関する補足説明です。",
                              "詳細は別添資料を参照してください。"]):
        added.insert_text((60, 140 + i * 24), line, fontname="japan", fontsize=11)

    # --- しおり: 新しいページ番号へ付け替え、名称末尾の「_開始_ページ数」も振り直す
    def new_page_of(old_p):
        if old_p > DELETE_PAGE:
            old_p -= 1
        elif old_p == DELETE_PAGE:
            pass  # 削除したページ自体を指すしおりは次のページへ
        if old_p > INSERT_AFTER:
            old_p += 1
        return old_p

    new_count = doc.page_count
    starts = [new_page_of(e[2]) for e in toc]
    new_toc = []
    for i, (level, title, _p, extra) in enumerate(toc):
        if title.startswith(RENAME[0]):
            title = RENAME[1] + title[len(RENAME[0]):]
        # 元の「ページ数」部分は、区間に追加/削除ページを含むときだけ±1する
        m = re.search(r"_(\d+)_(\d+)$", title)
        if m:
            old_start, count = int(m.group(1)), int(m.group(2))
            old_end = old_start + count - 1
            if count and old_start <= INSERT_AFTER < old_end + 1 and INSERT_AFTER != old_end:
                count += 1
            elif count and old_end == INSERT_AFTER and level == 1:
                count += 1
            if count and old_start <= DELETE_PAGE <= old_end:
                count -= 1
            title = title[:m.start()] + f"_{starts[i]}_{count}"
        new_toc.append([level, title, starts[i]])
    doc.set_toc(new_toc)
    doc.save(DST, garbage=3, deflate=True)
    print(f"旧 {old_count}ページ → 新 {new_count}ページ: {DST}", file=sys.stderr)


if __name__ == "__main__":
    main()
