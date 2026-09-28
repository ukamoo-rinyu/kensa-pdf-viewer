import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
KENSA = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, KENSA)

import fitz
import pdf_sections as doc_parser
from bookmark_diff import diff_sections
from indications import IndicationStore
from matching import match_anchor
from page_diff import PageDiffer, pair_pages

OLD = os.path.join(ROOT, "京都市.pdf")
NEW = os.path.join(HERE, "out", "kyoto_new.pdf")
# 仕込んだ変更(旧ページ, 新ページ): 種類
EXPECTED = {(9, 9): "文言", (77, 78): "文言", (125, 126): "文言", (None, 31): "追加",
            (140, None): "削除", (63, 64): "図", (62, 63): "再スキャン(地図)", (100, 101): "再スキャン(文字)"}  # 仕込みの内容は make_kyoto_new.py の冒頭を参照

t0 = time.perf_counter()
os_, ns = doc_parser.extract_document_sections(OLD), doc_parser.extract_document_sections(NEW)
d = diff_sections(os_, ns)
t1 = time.perf_counter()
print(f"しおり差分: 追加{len(d.added)} 削除{len(d.removed)} 名称変更{len(d.renamed)} ({t1 - t0:.1f}s)")
for a, b in d.renamed[:5]:
    print("   ~", a.title, "→", b.title)
if d.added:
    print("   + 例:", [s.title for s in d.added[:3]])
if d.removed:
    print("   - 例:", [s.title for s in d.removed[:3]])

differ = PageDiffer(OLD, NEW)
t2 = time.perf_counter()
pairs = pair_pages(os_, ns, fitz.open(OLD).page_count, fitz.open(NEW).page_count, d, differ.page_similarity)
t3 = time.perf_counter()
print(f"ページ対応付け: {len(pairs)}組 ({t3 - t2:.1f}s)")
offs = [(p.old_page, p.new_page) for p in pairs
        if p.old_page and p.new_page and p.new_page - p.old_page != (0 if p.old_page <= 30 else (1 if p.old_page < 140 else 0))]
print("   想定と違う対応:", offs[:10], "..." if len(offs) > 10 else "")
print("   対応なし:", [(p.old_page, p.new_page) for p in pairs if not (p.old_page and p.new_page)])

hits, false_pos = [], []
for p in pairs:
    r = differ.diff(p.old_page, p.new_page)
    key = (p.old_page, p.new_page)
    if key in EXPECTED:
        hits.append((key, EXPECTED[key], r.has_changes, r.text_changes, len(r.image_rects), r.text_compared))
    elif r.has_changes:
        false_pos.append((key, r.text_changes, len(r.image_rects)))
t4 = time.perf_counter()
print(f"全ページ比較: {t4 - t3:.1f}s")
print("仕込んだ変更:")
for key, kind, found, tc, ic, cmp_ in hits:
    print(f"   {'検出' if found else '見逃し'} {key} {kind}: 文字{tc} 画像{ic} 文字比較={'あり' if cmp_ else 'なし'}")
missing = set(EXPECTED) - {h[0] for h in hits}
print("   ページ対応に現れなかった:", missing)
print(f"誤検出(仕込んでいないのに差分あり): {len(false_pos)}件", false_pos[:15])

# 既存の指摘ファイルの照合
store = IndicationStore.load(os.path.join(ROOT, "京都市_指摘1.json"))
old_to_new = {p.old_page: p.new_page for p in pairs if p.old_page}
print("指摘の照合:")
for ind in store.indications:
    m = match_anchor(ind.anchor, ns)
    print(f"   #{ind.serial} 旧p{ind.anchor.absolute_page} → {m.status} "
          f"{m.section.title if m.section else ''} (区間先頭p{m.section.start_page if m.section else '-'}, "
          f"ページ対応p{old_to_new.get(ind.anchor.absolute_page)}) {m.note}")
