"""新旧比較画面(旧版と新版を左右に並べる)。

主な機能:
- 旧版・新版を左右に並べて開く
- しおりパスの照合で同期する(ページ番号では同期しない) → matching.match_anchorを使う
- しおり構成の差分を表示:追加された章／削除された章／名前が変わった章 → bookmark_diff.diff_sections
- 指摘一覧の1行をクリックすると、旧・新の両方が該当箇所を開く

差分の可視化もこのウィンドウに載せる(page_diff.py):
- 表示中の旧新ページの文字の変更箇所・図や画像の変化を色付きの枠で重ねて表示
- 「ページ送りを連動」: 片側をめくると、しおり区間で対応付けたもう片側のページへ追従
- 「前の差分/次の差分」: 変更のあったページだけを順に送る

差分・照合は「見るべき場所の当たりをつける補助」であり、検査の根拠ではない。
「照合できず」の場合は新版側を動かさず、はっきりそれと分かる表示にする(推測で別の場所を
指さない)。差分が検出されなかったページも「修正されていない」とは表示しない。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QProgressDialog, QPushButton, QSizePolicy, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

import pdf_sections as doc_parser
from bookmark_diff import SectionDiff, diff_sections, full_path
from indications import Indication, IndicationStore
from kensa_window import MarkerPageView
from matching import MatchResult, match_anchor
from page_diff import PageDiffer, PageDiffResult, PagePair, pair_pages

MATCH_STATUS_LABELS = {"exact": "完全一致", "fuzzy": "あいまい一致", "text": "近傍テキスト一致",
                        "unresolved": "照合できず"}
MATCH_STATUS_COLORS = {"exact": "#2F9E44", "fuzzy": "#F08C00", "text": "#F08C00", "unresolved": "#C0392B"}

# 差分の重ね描きの色(塗りは半透明、枠は不透明)。旧=赤(消えた・変わった)、新=緑(増えた・変わった)、
# 図や画像の変化=紫(旧新共通の位置)
OLD_TEXT_COLORS = (QColor(224, 49, 49, 55), QColor("#E03131"))
NEW_TEXT_COLORS = (QColor(47, 158, 68, 55), QColor("#2F9E44"))
IMAGE_COLORS = (QColor(174, 62, 201, 35), QColor("#AE3EC9"))
WHOLE_PAGE_COLORS = {"old": (QColor(224, 49, 49, 25), QColor("#E03131")),
                     "new": (QColor(47, 158, 68, 25), QColor("#2F9E44"))}


class _PageColumn(QWidget):
    """新旧それぞれの側: ページ送り + マーカー付きページビュー。"""

    page_changed = Signal(int)  # 表示ページが変わった(1始まり)

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.view = MarkerPageView()

        self.page_label = QLabel("- / -")
        prev_btn = QPushButton("◀")
        prev_btn.clicked.connect(lambda: self._nav(self.view.prev_page))
        next_btn = QPushButton("▶")
        next_btn.clicked.connect(lambda: self._nav(self.view.next_page))
        zoom_out_btn = QPushButton("－")
        zoom_out_btn.clicked.connect(lambda: self.view.zoom_out())
        zoom_fit_btn = QPushButton("幅に合わせる")
        zoom_fit_btn.clicked.connect(lambda: self.view.zoom_fit())
        zoom_in_btn = QPushButton("＋")
        zoom_in_btn.clicked.connect(lambda: self.view.zoom_in())

        # 左右2列+左パネルを1画面に収めるため、ボタンは文字幅ぶんだけにし、長いファイル名は省略可にする
        for btn in (prev_btn, next_btn, zoom_out_btn, zoom_in_btn):
            btn.setFixedWidth(btn.fontMetrics().horizontalAdvance(btn.text()) + 24)
        zoom_fit_btn.setText("幅")
        zoom_fit_btn.setToolTip("幅に合わせる")
        zoom_fit_btn.setFixedWidth(zoom_fit_btn.fontMetrics().horizontalAdvance("幅") + 24)
        title_label = QLabel(f"<b>{title}</b>")
        title_label.setToolTip(title)
        title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_label.setMinimumWidth(60)

        bar = QHBoxLayout()
        bar.addWidget(title_label, 2)
        bar.addStretch(1)
        bar.addWidget(prev_btn)
        bar.addWidget(self.page_label)
        bar.addWidget(next_btn)
        bar.addStretch(1)
        bar.addWidget(zoom_out_btn)
        bar.addWidget(zoom_fit_btn)
        bar.addWidget(zoom_in_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(bar)
        layout.addWidget(self.view)

    def set_document(self, document: QPdfDocument):
        self.view.set_document(document)

    def go_to_page(self, page_1based: int | None):
        if not page_1based:
            return
        self.view.go_to_page(page_1based)
        self._update_label()

    def _nav(self, fn):
        fn()
        self._update_label()

    def _update_label(self):
        total = self.view.page_count()
        cur = self.view.current_page_1based()
        self.page_label.setText(f"{cur} / {total}" if total else "- / -")
        if total:
            self.page_changed.emit(cur)


class CompareWindow(QMainWindow):
    """旧版(左)・新版(右)を並べ、指摘一覧やしおり差分から同期ジャンプする独立ウィンドウ。"""

    def __init__(self, old_path: str, new_path: str, store: IndicationStore, parent=None):
        super().__init__(parent)
        self.old_path = old_path
        self.new_path = new_path
        self.store = store

        self.setWindowTitle(f"新旧比較(試作): {os.path.basename(old_path)} → {os.path.basename(new_path)}")
        self.resize(1700, 950)

        self.old_sections = doc_parser.extract_document_sections(old_path)
        self.new_sections = doc_parser.extract_document_sections(new_path)
        self.diff: SectionDiff = diff_sections(self.old_sections, self.new_sections)

        self.old_document = QPdfDocument(self)
        self.old_document.load(old_path)
        self.new_document = QPdfDocument(self)
        self.new_document.load(new_path)

        # フェーズ3: ページ単位の差分。旧新ページの対応はしおり区間で決める(page_diff.pair_pages)
        self.differ = PageDiffer(old_path, new_path)
        self.pairs: list[PagePair] = pair_pages(
            self.old_sections, self.new_sections, self.old_document.pageCount(),
            self.new_document.pageCount(), self.diff, similarity=self.differ.page_similarity)
        self.old_to_new = {p.old_page: p.new_page for p in self.pairs if p.old_page is not None}
        self.new_to_old = {p.new_page: p.old_page for p in self.pairs if p.new_page is not None}
        self._current_pair: PagePair | None = None  # 差分を表示している旧新ページの組
        self._syncing = False  # プログラムからページを動かしている間は、連動の再帰を止める

        self.old_column = _PageColumn(f"旧版: {os.path.basename(old_path)}")
        self.new_column = _PageColumn(f"新版: {os.path.basename(new_path)}")
        self.old_column.set_document(self.old_document)
        self.new_column.set_document(self.new_document)
        self.old_column.page_changed.connect(lambda p: self._on_column_page_changed("old", p))
        self.new_column.page_changed.connect(lambda p: self._on_column_page_changed("new", p))

        self.match_status_label = QLabel("")
        self.match_status_label.setStyleSheet("font-weight: bold; padding: 4px;")
        self.match_status_label.setWordWrap(True)

        left = self._build_left_panel()
        left.setMinimumWidth(260)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.old_column)
        splitter.addWidget(self.new_column)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([380, 660, 660])

        banner = QLabel(
            "差分・照合は「見るべき場所の当たりをつける補助」です。検査の根拠にはせず、"
            "必ず人が並べて確認してください。「照合できず」は新版側を動かしません。"
            "差分が出ないページも「修正されていない」とは限りません(スキャン資料は誤検出も出ます)。")
        banner.setWordWrap(True)
        banner.setStyleSheet("background:#FFF3CD; color:#7A5B00; padding:6px;")
        banner.setMaximumHeight(56)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(banner, 0)
        central_layout.addWidget(self._build_diff_bar(), 0)
        central_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        first = self.pairs[0] if self.pairs else PagePair(1, 1)
        self._show_pair(PagePair(first.old_page or 1, first.new_page or 1))

    def closeEvent(self, event):
        self.differ.close()
        super().closeEvent(event)

    # --------------------------------------------------------------- 差分表示(フェーズ3)
    def _build_diff_bar(self) -> QWidget:
        self.show_diff_check = QCheckBox("差分を表示")
        self.show_diff_check.setChecked(True)
        self.show_diff_check.toggled.connect(lambda _on: self._refresh_diff())
        self.sync_check = QCheckBox("ページ送りを連動")
        self.sync_check.setChecked(True)
        self.sync_check.setToolTip("片側をめくると、しおりで対応付けたもう片側のページへ追従します")
        prev_btn = QPushButton("◀ 前の差分")
        prev_btn.clicked.connect(lambda: self._goto_next_diff(-1))
        next_btn = QPushButton("次の差分 ▶")
        next_btn.clicked.connect(lambda: self._goto_next_diff(+1))

        legend = QLabel(
            "<span style='color:#E03131'>■</span> 旧版で消えた・変わった文字　"
            "<span style='color:#2F9E44'>■</span> 新版で増えた・変わった文字　"
            "<span style='color:#AE3EC9'>■</span> 図・画像の変化")
        legend.setWordWrap(True)
        # 状態表示は長くなるため折り返す(折り返さないと、その文の長さがウィンドウの最小幅になり、
        # 画面からはみ出す)
        self.diff_status_label = QLabel("")
        self.diff_status_label.setStyleSheet("font-weight: bold;")
        self.diff_status_label.setWordWrap(True)
        self.diff_status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        text_box.addWidget(self.diff_status_label)
        text_box.addWidget(legend)

        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addWidget(self.show_diff_check)
        layout.addWidget(self.sync_check)
        layout.addWidget(prev_btn)
        layout.addWidget(next_btn)
        layout.addSpacing(12)
        layout.addLayout(text_box, 1)
        return bar

    def _show_pair(self, pair: PagePair | None, move_old: int | None = None, move_new: int | None = None):
        """旧新のページを(連動を止めたまま)開き、pairの差分を表示する。

        move_old/move_newを省略すると、pairのページ(Noneでない側)へ移動する。
        pair=Noneは「差分を出さない」(照合できなかった指摘へのジャンプなど)。
        """
        if pair is not None:
            move_old = move_old if move_old is not None else pair.old_page
            move_new = move_new if move_new is not None else pair.new_page
        self._syncing = True
        try:
            if move_old:
                self.old_column.go_to_page(move_old)
            if move_new:
                self.new_column.go_to_page(move_new)
        finally:
            self._syncing = False
        self._current_pair = pair
        self._refresh_diff()

    def _on_column_page_changed(self, side: str, page: int):
        if self._syncing:
            return
        if not self.sync_check.isChecked():
            # 連動なし: 表示中のページ同士をそのまま比べる
            self._current_pair = PagePair(self.old_column.view.current_page_1based(),
                                          self.new_column.view.current_page_1based())
            self._refresh_diff()
            return
        if side == "old":
            self._show_pair(PagePair(page, self.old_to_new.get(page)))
        else:
            self._show_pair(PagePair(self.new_to_old.get(page), page))

    def _refresh_markers(self):
        """旧版側に、表示中のページにある指摘のピンを描く(ページが変わるたびに描き直す)。

        新版側は座標の裏付けがないため、ピンは出さずページ単位の案内にとどめる。
        """
        page = self.old_column.view.current_page_1based()
        markers = [(i.anchor.rel_x, i.anchor.rel_y, "#2F6FE4", str(i.serial), i.id)
                   for i in self.store.indications
                   if i.anchor.absolute_page == page and i.anchor.rel_x is not None and i.anchor.rel_y is not None]
        self.old_column.view.set_markers(markers)
        self.new_column.view.set_markers([])

    def _refresh_diff(self):
        self._refresh_markers()
        old_view, new_view = self.old_column.view, self.new_column.view
        pair = self._current_pair
        if not self.show_diff_check.isChecked() or pair is None:
            old_view.set_overlays([])
            new_view.set_overlays([])
            self.diff_status_label.setText("" if pair is not None else "差分表示なし(対応ページが特定できません)")
            self.diff_status_label.setStyleSheet("font-weight: bold; color: #868E96;")
            return

        result = self.differ.diff(pair.old_page, pair.new_page)
        old_shown = pair.old_page is not None and old_view.current_page_1based() == pair.old_page
        new_shown = pair.new_page is not None and new_view.current_page_1based() == pair.new_page

        def overlays(rects, colors):
            return [(x0, y0, x1, y1, colors[0], colors[1]) for x0, y0, x1, y1 in rects]

        whole_page = pair.old_page is None or pair.new_page is None
        old_colors = WHOLE_PAGE_COLORS["old"] if whole_page else OLD_TEXT_COLORS
        new_colors = WHOLE_PAGE_COLORS["new"] if whole_page else NEW_TEXT_COLORS
        old_view.set_overlays(overlays(result.old_rects, old_colors) + overlays(result.image_rects, IMAGE_COLORS)
                              if old_shown else [])
        new_view.set_overlays(overlays(result.new_rects, new_colors) + overlays(result.image_rects, IMAGE_COLORS)
                              if new_shown else [])
        self._set_diff_status(pair, result)

    def _set_diff_status(self, pair: PagePair, result: PageDiffResult):
        def p(n):
            return f"p.{n}" if n is not None else "(なし)"

        head = f"旧 {p(pair.old_page)} ↔ 新 {p(pair.new_page)}: "
        if pair.old_page is None or pair.new_page is None:
            text, color = head + result.note, "#C0392B"
        elif result.has_changes:
            parts = []
            if result.text_changes:
                parts.append(f"文字の変更 {result.text_changes}箇所")
            if result.text_layer_changed:
                parts.append("文字情報の有無が変化")
            if result.image_rects:
                parts.append(f"図・画像の変化 {len(result.image_rects)}箇所")
            text, color = head + " / ".join(parts), "#C0392B"
        else:
            text = head + "差分は検出されませんでした(修正されていない、という意味ではありません)"
            color = "#495057"
        if result.note and pair.old_page is not None and pair.new_page is not None:
            text += f"　※{result.note}"
        if (self.sync_check.isChecked() is False and pair.old_page is not None
                and self.old_to_new.get(pair.old_page) != pair.new_page):
            text += "　※しおりで対応付けたページ同士ではありません"
        self.diff_status_label.setText(text)
        self.diff_status_label.setStyleSheet(f"font-weight: bold; color: {color};")

    def _current_pair_index(self) -> int:
        cur = self._current_pair
        if cur is not None:
            for i, pr in enumerate(self.pairs):
                if (cur.old_page is not None and pr.old_page == cur.old_page) or \
                        (cur.old_page is None and pr.new_page == cur.new_page):
                    return i
        old_p = self.old_column.view.current_page_1based()
        for i, pr in enumerate(self.pairs):
            if pr.old_page == old_p:
                return i
        return -1

    def _goto_next_diff(self, step: int):
        """変更のあったページの組だけを順に送る。"""
        start = self._current_pair_index()
        indices = range(start + 1, len(self.pairs)) if step > 0 else range(start - 1, -1, -1)
        indices = list(indices)
        progress = QProgressDialog("差分のあるページを探しています…", "中止", 0, len(indices), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        found = None
        for n, i in enumerate(indices):
            progress.setValue(n)
            if progress.wasCanceled():
                break
            pr = self.pairs[i]
            if self.differ.diff(pr.old_page, pr.new_page).has_changes:
                found = pr
                break
        progress.close()
        if found is None:
            where = "後" if step > 0 else "前"
            self.diff_status_label.setText(f"これより{where}に差分が検出されたページはありません")
            self.diff_status_label.setStyleSheet("font-weight: bold; color: #495057;")
            return
        self.show_diff_check.setChecked(True)
        self._show_pair(found)

    # --------------------------------------------------------------- 左パネル
    def _build_left_panel(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_indication_tab(), f"指摘一覧 ({len(self.store.indications)})")
        tabs.addTab(self._build_diff_tab(), self._diff_tab_title())

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(self.match_status_label)
        layout.addWidget(tabs)
        return panel

    def _diff_tab_title(self) -> str:
        d = self.diff
        return f"しおり差分 (追加{len(d.added)}/削除{len(d.removed)}/変更{len(d.renamed)})"

    def _build_indication_tab(self) -> QWidget:
        # しおりパスは長く、列にすると横スクロールで番号や内容が隠れるため、行のツールチップに出す
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["No", "区分", "状態", "指摘内容"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setWordWrap(True)
        table.verticalHeader().setVisible(False)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = table.horizontalHeader()
        for c in range(3):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.cellClicked.connect(self._on_indication_row_clicked)

        rows = sorted(self.store.indications, key=lambda i: i.serial)
        table.setRowCount(len(rows))
        for r, ind in enumerate(rows):
            tip = f"しおり: {ind.anchor.full_path}\n旧版 p.{ind.anchor.absolute_page}"
            values = [str(ind.serial), ind.category, ind.status, ind.content or "(内容未記入)"]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setToolTip(tip + (f"\n\n{ind.content}" if ind.content else ""))
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, ind.id)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 3 and not ind.content:
                    item.setForeground(QColor("#868E96"))
                table.setItem(r, c, item)
        table.resizeRowsToContents()
        self._indication_table = table
        return table

    def _build_diff_tab(self) -> QWidget:
        listw = QListWidget()

        def add_header(text: str):
            item = QListWidgetItem(text)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            item.setForeground(QColor("#495057"))
            listw.addItem(item)

        add_header(f"■ 追加された章 ({len(self.diff.added)})")
        for s in self.diff.added:
            item = QListWidgetItem(f"　+ {full_path(s)}")
            item.setForeground(QColor("#2F9E44"))
            item.setData(Qt.ItemDataRole.UserRole, ("added", s))
            listw.addItem(item)

        add_header(f"■ 削除された章 ({len(self.diff.removed)})")
        for s in self.diff.removed:
            item = QListWidgetItem(f"　- {full_path(s)}")
            item.setForeground(QColor("#C0392B"))
            item.setData(Qt.ItemDataRole.UserRole, ("removed", s))
            listw.addItem(item)

        add_header(f"■ 名前が変わった章 ({len(self.diff.renamed)})")
        for old_s, new_s in self.diff.renamed:
            item = QListWidgetItem(f"　~ 旧: {full_path(old_s)}\n　   新: {full_path(new_s)}")
            item.setForeground(QColor("#F08C00"))
            item.setData(Qt.ItemDataRole.UserRole, ("renamed", (old_s, new_s)))
            listw.addItem(item)

        listw.setWordWrap(True)
        listw.itemClicked.connect(self._on_diff_item_clicked)
        return listw

    # --------------------------------------------------------------- ジャンプ
    def _on_indication_row_clicked(self, row: int, _col: int):
        item = self._indication_table.item(row, 0)
        if item is None:
            return
        indication = self.store.get(item.data(Qt.ItemDataRole.UserRole))
        if indication is None:
            return
        self._goto_indication(indication)

    def _goto_indication(self, indication: Indication):
        old_page = indication.anchor.absolute_page or indication.anchor.section_start_page
        a = indication.anchor

        result: MatchResult = match_anchor(indication.anchor, self.new_sections)
        color = MATCH_STATUS_COLORS[result.status]
        label = MATCH_STATUS_LABELS[result.status]
        detail = f"　({result.note})" if result.note else ""
        self.match_status_label.setText(f"指摘 #{indication.serial} の照合結果: {label}{detail}")
        self.match_status_label.setStyleSheet(f"font-weight: bold; padding: 4px; color: {color};")

        if result.section is None:
            # 照合できず: 新版側は動かさない(推測で別の場所を指さない)
            self._show_pair(None, move_old=old_page)
        else:
            # 照合した区間の中に、ページ対応付けで旧ページの相手があればそこへ。なければ区間の先頭
            partner = self.old_to_new.get(old_page)
            sec = result.section
            if partner is not None and sec.start_page <= partner <= sec.end_page:
                self._show_pair(PagePair(old_page, partner))
            else:
                self._show_pair(PagePair(old_page, sec.start_page))

        if a.rel_x is not None and a.rel_y is not None:
            self.old_column.view.highlight_marker(indication.id)

    def _on_diff_item_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data is None:
            return
        kind, payload = data
        self.match_status_label.setText("")
        if kind == "added":
            self._show_pair(PagePair(self.new_to_old.get(payload.start_page), payload.start_page))
        elif kind == "removed":
            self._show_pair(PagePair(payload.start_page, self.old_to_new.get(payload.start_page)))
        elif kind == "renamed":
            old_s, new_s = payload
            self._show_pair(PagePair(old_s.start_page, new_s.start_page))
