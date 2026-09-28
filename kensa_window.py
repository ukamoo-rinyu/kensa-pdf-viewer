"""検査ビューアーのメインウィンドウ(指摘の記録・一覧・Excel書き出し)。

しおり階層の抽出は、mail-pdf-viewer の parser.py から取り込んだ pdf_sections.py を使う
(しおりパス DocSection.path_titles を、指摘の位置(アンカー)の軸にする)。

指摘は「PDF上をクリックして置いたマーカー(付箋ピン)」を軸に記録する。QPdfViewは複数ページを連続スクロール表示
する仕組みで、画面上のクリック位置が何ページ目のどこかを外部から正確に取り出す標準APIが
ないため、1ページを直接レンダリングしたQPixmapを自前で表示し、クリック位置をそのまま
ページ内の比率座標(0〜1)に変換する(MarkerPageView)。
"""
from __future__ import annotations

import os
import time

import fitz  # PyMuPDF(座標周辺テキストの抽出に使う)
from PySide6.QtCore import QModelIndex, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QImage, QPainter, QPen, QPixmap, QStandardItem, QStandardItemModel,
)
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDockWidget, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStatusBar, QTableWidget,
    QTableWidgetItem, QToolBar, QTreeView, QVBoxLayout, QWidget,
)

import pdf_sections as doc_parser
from export_excel import export as export_excel
from indications import CATEGORIES, STATUSES, Anchor, Indication, IndicationStore
from matching import match_anchor

SECTION_ROLE = Qt.ItemDataRole.UserRole + 1
MATCH_STATUS_LABELS = {"exact": "完全一致", "fuzzy": "あいまい一致", "text": "近傍テキスト一致",
                        "unresolved": "照合できず"}
CATEGORY_COLORS = {"要修正": "#E64980", "要確認": "#F08C00", "軽微": "#868E96", "質問": "#4C6EF5"}
DEFAULT_MARKER_COLOR = "#2F6FE4"
# モーダルの指摘ダイアログをOKで閉じた直後、Windows環境ではそのクリックが背後のPDF表示へ
# 「抜けて」しまい、同じ操作でもう一つマーカー配置ダイアログが開いてしまうことがある。
# ダイアログを閉じた直後のこの秒数は、マーカー配置由来のクリック/ダブルクリックを無視する。
POST_DIALOG_CLICK_GUARD_SEC = 0.4


# --------------------------------------------------------------------- しおり階層
class SectionTreeModel(QStandardItemModel):
    """一般資料PDFのしおり階層。"""

    def __init__(self):
        super().__init__()
        self.setColumnCount(1)

    def set_tree(self, rows: list["doc_parser.DocSection"]):
        self.clear()
        items_by_id: dict[int, QStandardItem] = {}
        root = self.invisibleRootItem()
        for row in rows:  # しおり順 = 常に親が子より先に来る
            item = QStandardItem(row.title)
            item.setEditable(False)
            item.setData(row, SECTION_ROLE)
            items_by_id[row.index] = item
            parent_item = items_by_id.get(row.parent_index) if row.parent_index is not None else None
            (parent_item or root).appendRow(item)

    def section_at(self, index: QModelIndex) -> "doc_parser.DocSection | None":
        item = self.itemFromIndex(index)
        return item.data(SECTION_ROLE) if item else None


def _nearby_text(pdf_path: str, page_1based: int, rel_x: float, rel_y: float, max_len: int = 60) -> str:
    """クリック位置周辺の単語を読み順に並べたスニペット。しおり名が変わった場合の再照合の保険。"""
    try:
        doc = fitz.open(pdf_path)
        page = doc[page_1based - 1]
        rect = page.rect
        words = page.get_text("words")  # (x0, y0, x1, y1, text, block_no, line_no, word_no)
        doc.close()
    except Exception:  # noqa: BLE001
        return ""
    if not words or rect.width <= 0 or rect.height <= 0:
        return ""
    target_x, target_y = rel_x * rect.width, rel_y * rect.height

    def _dist(w):
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        return (cx - target_x) ** 2 + (cy - target_y) ** 2

    nearest = sorted(words, key=_dist)[:10]
    nearest.sort(key=lambda w: (w[5], w[6], w[7]))  # 読み順(block, line, word)に戻す
    return " ".join(w[4] for w in nearest)[:max_len]


# --------------------------------------------------------------------- マーカー付きページビュー
class _ClickableLabel(QLabel):
    clicked = Signal(QPointF)
    doubleClicked = Signal(QPointF)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(QPointF(event.position()))
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit(QPointF(event.position()))
        super().mouseDoubleClickEvent(event)


class MarkerPageView(QScrollArea):
    """PDFを1ページずつ画像として表示し、クリックでマーカー(付箋ピン)を置けるビュー。

    表示上のサイズ(論理px)と、実際にレンダリングする解像度(物理px)をあえて分離する。
    表示幅そのままの解像度でレンダリングすると、高DPI画面(スケーリング125〜150%など)や
    拡大時に画素が荒く見えるため、常に表示サイズより高い解像度で描いてから縮小表示する
    (QPdfViewが内部で行っているのと同じ考え方)。
    """

    # 既存マーカーをクリックした場合は (0, 0, marker_id) 、空欄をクリックした場合は (rel_x, rel_y, None)
    marker_clicked = Signal(float, float, object)
    # ダブルクリックで空欄に新規マーカーを追加したい場合(配置モードのON/OFFに関わらず常に発火)
    marker_double_clicked = Signal(float, float)

    OVERSAMPLE = 2.5          # 表示サイズの何倍の解像度でいったん描いてから縮小するか
    MAX_RENDER_DIM = 4200     # レンダリング時の物理pxの上限(巨大ページでのメモリ・速度対策)
    MARKER_RADIUS = 8         # px。表示上の見た目の大きさ(近接する指摘でも重なりにくいよう小さめ)
    HIT_RADIUS = 10           # px。マーカー配置モードOFF時、クリックでマーカーに当たったとみなす半径
    HIGHLIGHT_RADIUS = 15     # px。一時ハイライト時の外側リングの大きさ
    HIGHLIGHT_MS = 1400       # 一時ハイライトを表示し続ける時間

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document: QPdfDocument | None = None
        self.page_index = 0  # 0始まり
        self._base_pixmap: QPixmap | None = None  # 常にlabelの表示サイズと同じpx数(dpr=1として扱う)
        self._markers: list[tuple[float, float, str, str, str]] = []  # rel_x, rel_y, color, label, marker_id
        self._placement_mode = False  # Trueの間は既存マーカーの位置に関わらず常に新規マーカーとして扱う
        self._fit_width = True
        self._render_width = 900
        self._highlight_id: str | None = None  # 一覧クリックで一時ハイライト中のマーカーid
        # 差分表示(フェーズ3)用の重ね描き: (rel_x0, rel_y0, rel_x1, rel_y1, 塗り色, 枠色)。現在ページ分のみ
        self._overlays: list[tuple[float, float, float, float, QColor, QColor]] = []

        self.label = _ClickableLabel()
        self.label.clicked.connect(self._on_label_clicked)
        self.label.doubleClicked.connect(self._on_label_double_clicked)
        self.setWidget(self.label)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

    # ------------------------------------------------------------ 文書・ページ
    def set_document(self, document: QPdfDocument):
        self.document = document
        self.page_index = 0
        self._base_pixmap = None

    def page_count(self) -> int:
        return self.document.pageCount() if self.document else 0

    def set_placement_mode(self, enabled: bool):
        self._placement_mode = enabled

    def current_page_1based(self) -> int:
        return self.page_index + 1

    def go_to_page(self, page_1based: int):
        if not self.document or self.document.pageCount() == 0:
            return
        self.page_index = max(0, min(page_1based - 1, self.document.pageCount() - 1))
        self._overlays = []  # 差分の重ね描きはページ固有なので、ページが変わったら消す
        self.render_current_page()

    def next_page(self):
        if self.document and self.page_index < self.document.pageCount() - 1:
            self.page_index += 1
            self._overlays = []
            self.render_current_page()

    def prev_page(self):
        if self.document and self.page_index > 0:
            self.page_index -= 1
            self._overlays = []
            self.render_current_page()

    # ------------------------------------------------------------ ズーム
    def _target_width(self) -> int:
        if self._fit_width:
            return max(300, self.viewport().width() - 4)
        return self._render_width

    def zoom_in(self):
        self._fit_width = False
        self._render_width = int(self._target_width() * 1.2)
        self.render_current_page()

    def zoom_out(self):
        self._fit_width = False
        self._render_width = max(200, int(self._target_width() * 0.8))
        self.render_current_page()

    def zoom_fit(self):
        self._fit_width = True
        self.render_current_page()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_width:
            self.render_current_page()

    # ------------------------------------------------------------ 描画
    def render_current_page(self):
        """表示解像度より高い解像度でいったん描いてから縮小し、self._base_pixmapへ格納する。

        QPixmap.setDevicePixelRatio()で「表示は小さく、データは高解像度」を実現する方法は
        環境(画面のスケーリング設定)によってQLabel側が解像度を想定外に解釈し、表示サイズと
        マーカー座標がずれる不具合を実機相当の環境で確認した。そのため、ここでは縮小処理を
        自前で行い、self._base_pixmapのwidth()/height()が常にlabelの表示サイズそのものと
        一致するようにする(devicePixelRatioは一切使わない)。
        """
        if not self.document or self.document.pageCount() == 0:
            return
        pt_size = self.document.pagePointSize(self.page_index)
        if pt_size.width() <= 0:
            return

        target_w = self._target_width()  # 表示サイズ(px)。この値でlabelにそのまま表示する
        target_h = max(1, round(target_w * pt_size.height() / pt_size.width()))

        render_scale = self.OVERSAMPLE
        render_w, render_h = target_w * render_scale, target_h * render_scale
        if render_w > self.MAX_RENDER_DIM or render_h > self.MAX_RENDER_DIM:
            render_scale *= self.MAX_RENDER_DIM / max(render_w, render_h)

        img_size = QSize(max(1, round(target_w * render_scale)), max(1, round(target_h * render_scale)))
        image = self.document.render(self.page_index, img_size)
        # QPdfDocument.render()は、ページ自身が何も描いていない領域をalpha=0(透明)で返す
        # (白ではない)。そのままQPixmapにすると、その透明部分がラベルの背景色を透かして見え、
        # ページと周囲の境目が分からなくなるため、先に不透明な白地へ合成しておく。
        opaque = QImage(image.size(), QImage.Format.Format_RGB32)
        opaque.fill(Qt.GlobalColor.white)
        painter = QPainter(opaque)
        painter.drawImage(0, 0, image)
        painter.end()
        high_res = QPixmap.fromImage(opaque)
        self._base_pixmap = high_res.scaled(
            target_w, target_h,
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)

        self.label.setFixedSize(self._base_pixmap.size())
        self._redraw()

    def set_markers(self, markers: list[tuple[float, float, str, str]]):
        """(rel_x, rel_y, color, label, marker_id)のリスト。現在ページ分だけ渡す。"""
        self._markers = markers
        self._redraw()

    def set_overlays(self, overlays: list[tuple[float, float, float, float, QColor, QColor]]):
        """差分箇所の矩形(相対座標)を重ねて描く。現在ページ分だけ渡す。ページ移動で自動的に消える。"""
        self._overlays = overlays
        self._redraw()

    def highlight_marker(self, marker_id: str, duration_ms: int | None = None):
        """指摘一覧の行クリックに応じて、該当マーカーを一時的に目立たせる。"""
        self._highlight_id = marker_id
        self._redraw()
        QTimer.singleShot(duration_ms or self.HIGHLIGHT_MS,
                           lambda: self._clear_highlight(marker_id))

    def _clear_highlight(self, marker_id: str):
        # ハイライト中に別の行がクリックされていたら、そちらを消さない
        if self._highlight_id == marker_id:
            self._highlight_id = None
            self._redraw()

    def _redraw(self):
        if self._base_pixmap is None:
            return
        pixmap = QPixmap(self._base_pixmap)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = pixmap.width(), pixmap.height()  # labelの表示サイズと同じpx数(dpr操作をしていないため)
        radius = self.MARKER_RADIUS
        font = painter.font()
        font.setBold(True)
        font.setPointSizeF(9)
        painter.setFont(font)
        # 差分の枠はマーカーより下に描く(マーカーが差分で隠れないように)
        for x0, y0, x1, y1, fill, border in self._overlays:
            pad = 2  # 1文字だけの変更でも見落とさないよう、少し外側に広げる
            rect = QRectF(x0 * w - pad, y0 * h - pad, (x1 - x0) * w + pad * 2, (y1 - y0) * h + pad * 2)
            painter.setBrush(fill)
            painter.setPen(QPen(border, 2))
            painter.drawRect(rect)
        for rel_x, rel_y, color, label, marker_id in self._markers:
            x, y = rel_x * w, rel_y * h
            if marker_id == self._highlight_id:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor("#FFD43B"), 3))
                painter.drawEllipse(QPointF(x, y), self.HIGHLIGHT_RADIUS, self.HIGHLIGHT_RADIUS)
            painter.setBrush(QColor(color))
            painter.setPen(QPen(QColor("#FFFFFF"), 2))
            painter.drawEllipse(QPointF(x, y), radius, radius)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(QRectF(x - radius, y - radius, radius * 2, radius * 2),
                              Qt.AlignmentFlag.AlignCenter, label)
        painter.end()
        self.label.setPixmap(pixmap)

    def _marker_at(self, pos: QPointF, w: int, h: int) -> "str | None":
        """クリック位置(論理px)が既存マーカーに乗っているか判定し、乗っていればmarker_idを返す。"""
        best_id = None
        best_dist2 = self.HIT_RADIUS ** 2
        for rel_x, rel_y, _color, _label, marker_id in self._markers:
            mx, my = rel_x * w, rel_y * h
            dist2 = (mx - pos.x()) ** 2 + (my - pos.y()) ** 2
            if dist2 <= best_dist2:
                best_dist2 = dist2
                best_id = marker_id
        return best_id

    def _on_label_clicked(self, pos: QPointF):
        if self._base_pixmap is None:
            return
        w, h = self.label.width(), self.label.height()  # クリック位置は論理px。表示サイズで正規化する
        if w <= 0 or h <= 0:
            return
        rel_x = min(max(pos.x() / w, 0.0), 1.0)
        rel_y = min(max(pos.y() / h, 0.0), 1.0)

        if self._placement_mode:
            # 配置モード中は、既存マーカーの近くをクリックしても必ず新規マーカーとして扱う。
            # (ヒット判定を優先すると、近接した箇所に2つ目を置こうとしても1つ目の編集に
            #  化けてしまい、「2番目が置けない」という不具合になるため)
            self.marker_clicked.emit(rel_x, rel_y, None)
            return

        hit_id = self._marker_at(pos, w, h)
        if hit_id is not None:
            self.marker_clicked.emit(0.0, 0.0, hit_id)

    def _on_label_double_clicked(self, pos: QPointF):
        """空欄のダブルクリックで、配置モードのON/OFFに関わらず新規マーカーを追加する。"""
        if self._base_pixmap is None:
            return
        w, h = self.label.width(), self.label.height()
        if w <= 0 or h <= 0:
            return
        if self._marker_at(pos, w, h) is not None:
            return  # 既存マーカーの上のダブルクリックは、直前のシングルクリックで編集ダイアログが開くため無視
        rel_x = min(max(pos.x() / w, 0.0), 1.0)
        rel_y = min(max(pos.y() / h, 0.0), 1.0)
        self.marker_double_clicked.emit(rel_x, rel_y)


# --------------------------------------------------------------------- 指摘ダイアログ
class IndicationDialog(QDialog):
    """指摘の追加・編集ダイアログ。"""

    def __init__(self, section_label: str, parent=None, existing: Indication | None = None,
                 defaults: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("指摘を編集" if existing else "指摘を追加")
        self.delete_requested = False

        self.category_combo = QComboBox()
        self.category_combo.addItems(CATEGORIES)
        self.status_combo = QComboBox()
        self.status_combo.addItems(STATUSES)
        self.round_edit = QLineEdit("初回")
        self.inspector_edit = QLineEdit()
        self.content_edit = QPlainTextEdit()

        if existing:
            self.category_combo.setCurrentText(existing.category)
            self.status_combo.setCurrentText(existing.status)
            self.round_edit.setText(existing.round_name)
            self.inspector_edit.setText(existing.inspector)
            self.content_edit.setPlainText(existing.content)
        elif defaults:
            # ビュー上部の「既定値」バーの現在値を初期値にする(区分・状態・回次・指摘者を毎回打たなくてよいように)
            self.category_combo.setCurrentText(defaults.get("category", self.category_combo.currentText()))
            self.status_combo.setCurrentText(defaults.get("status", self.status_combo.currentText()))
            self.round_edit.setText(defaults.get("round_name", self.round_edit.text()))
            self.inspector_edit.setText(defaults.get("inspector", self.inspector_edit.text()))

        form = QFormLayout()
        target_label = QLabel(section_label)
        target_label.setWordWrap(True)
        form.addRow("対象箇所", target_label)
        form.addRow("区分", self.category_combo)
        form.addRow("状態", self.status_combo)
        form.addRow("検査回次", self.round_edit)
        form.addRow("指摘者", self.inspector_edit)
        form.addRow("指摘内容", self.content_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        if existing:
            delete_btn = buttons.addButton("この指摘を削除", QDialogButtonBox.ButtonRole.DestructiveRole)
            delete_btn.clicked.connect(self._on_delete_clicked)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.resize(440, 400)

    def _on_delete_clicked(self):
        if QMessageBox.question(
                self, "指摘を削除", "この指摘を削除します。よろしいですか?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        # OKと同じacceptedルートに乗せ、削除の実処理は呼び出し側(_edit_indication)に任せる
        self.delete_requested = True
        self.accept()


# --------------------------------------------------------------------- メインウィンドウ
class KensaWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("検査PDFビューアー(試作) - 指摘の記録と一覧")
        self.resize(1400, 900)

        self.pdf_path: str | None = None
        self.indication_path: str | None = None
        self.store = IndicationStore()
        self.sections: list["doc_parser.DocSection"] = []
        self._unresolved_ids: set[str] = set()  # 別PDFへの再照合で位置が特定できなかった指摘のid
        self._last_dialog_closed_at = 0.0  # POST_DIALOG_CLICK_GUARD_SEC参照

        self.document = QPdfDocument(self)

        self.tree = QTreeView()
        self.tree_model = SectionTreeModel()
        self.tree.setModel(self.tree_model)
        self.tree.setHeaderHidden(True)
        self.tree.clicked.connect(self._on_section_clicked)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["No", "区分", "状態", "しおり", "内容", "指摘者"])
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.cellClicked.connect(self._on_table_clicked)
        self.table.cellDoubleClicked.connect(self._on_table_double_clicked)

        self.category_filter = QComboBox()
        self.category_filter.addItems(["(すべての区分)"] + CATEGORIES)
        self.status_filter = QComboBox()
        self.status_filter.addItems(["(すべての状態)"] + STATUSES)
        self.category_filter.currentIndexChanged.connect(self._refresh_table)
        self.status_filter.currentIndexChanged.connect(self._refresh_table)

        add_btn = QPushButton("選択中のしおり全体に指摘を追加(位置指定なし)")
        add_btn.clicked.connect(self._add_indication_for_section)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("しおり階層"))
        left_layout.addWidget(self.tree)
        left_layout.addWidget(add_btn)

        # ------------------------------------------------------- 中央: マーカー付きPDFビュー
        self.marker_view = MarkerPageView()
        self.marker_view.marker_clicked.connect(self._on_marker_clicked)
        self.marker_view.marker_double_clicked.connect(self._on_marker_double_clicked)

        self.marker_mode_btn = QPushButton("📍 マーカーを置いて指摘を追加")
        self.marker_mode_btn.setCheckable(True)
        self.marker_mode_btn.toggled.connect(self._on_marker_mode_toggled)

        # ------------------------------------------------- 既定値バー(区分・状態・検査回次・指摘者)
        # 検査回次や指摘者名は作業中は変わらないことが多いため、指摘のたびに入力し直さなくてよいよう
        # ビュー上部に常時表示する。ここでの値がIndicationDialogの初期値になり、ダイアログでの
        # 選択結果はこのバーへ書き戻される(「前回使った値」が次の既定値になる)。
        self.default_category_combo = QComboBox()
        self.default_category_combo.addItems(CATEGORIES)
        self.default_status_combo = QComboBox()
        self.default_status_combo.addItems(STATUSES)
        self.default_round_edit = QLineEdit("初回")
        self.default_round_edit.setFixedWidth(90)
        self.default_inspector_edit = QLineEdit()
        self.default_inspector_edit.setFixedWidth(120)

        defaults_bar = QHBoxLayout()
        defaults_bar.addWidget(QLabel("既定値(次に追加する指摘に使用):"))
        defaults_bar.addWidget(QLabel("区分"))
        defaults_bar.addWidget(self.default_category_combo)
        defaults_bar.addWidget(QLabel("状態"))
        defaults_bar.addWidget(self.default_status_combo)
        defaults_bar.addWidget(QLabel("検査回次"))
        defaults_bar.addWidget(self.default_round_edit)
        defaults_bar.addWidget(QLabel("指摘者"))
        defaults_bar.addWidget(self.default_inspector_edit)
        defaults_bar.addStretch(1)

        prev_btn = QPushButton("◀ 前へ")
        prev_btn.clicked.connect(lambda: self._prev_page())
        next_btn = QPushButton("次へ ▶")
        next_btn.clicked.connect(lambda: self._next_page())
        self.page_label = QLabel("- / -")
        zoom_out_btn = QPushButton("－")
        zoom_out_btn.clicked.connect(lambda: self.marker_view.zoom_out())
        zoom_fit_btn = QPushButton("幅に合わせる")
        zoom_fit_btn.clicked.connect(lambda: self.marker_view.zoom_fit())
        zoom_in_btn = QPushButton("＋")
        zoom_in_btn.clicked.connect(lambda: self.marker_view.zoom_in())

        center_toolbar = QHBoxLayout()
        center_toolbar.addWidget(self.marker_mode_btn)
        center_toolbar.addStretch(1)
        center_toolbar.addWidget(prev_btn)
        center_toolbar.addWidget(self.page_label)
        center_toolbar.addWidget(next_btn)
        center_toolbar.addStretch(1)
        center_toolbar.addWidget(zoom_out_btn)
        center_toolbar.addWidget(zoom_fit_btn)
        center_toolbar.addWidget(zoom_in_btn)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(4, 4, 4, 4)
        center_layout.addLayout(center_toolbar)
        center_layout.addLayout(defaults_bar)
        center_layout.addWidget(self.marker_view)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        filter_bar = QHBoxLayout()
        filter_bar.addWidget(self.category_filter)
        filter_bar.addWidget(self.status_filter)
        filter_bar.addStretch(1)
        right_layout.addLayout(filter_bar)
        right_layout.addWidget(self.table)
        right_layout.addWidget(QLabel("行をクリックで該当ページへ移動しマーカーを一時ハイライト、"
                                       "ダブルクリックで編集ダイアログを開きます。"))

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(center)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        self.setCentralWidget(splitter)

        # 指摘一覧はドックウィジェットにして、右側/下側への配置切り替えと非表示をQt標準機能で行えるようにする
        # (タイトルバーをドラッグして右または下のドック領域に移動できる。表示/非表示はツールバーのボタンから)
        self.list_dock = QDockWidget("指摘一覧", self)
        self.list_dock.setObjectName("indication_list_dock")
        self.list_dock.setWidget(right)
        self.list_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.list_dock)

        self._build_toolbar()
        self.setStatusBar(QStatusBar())

    # --------------------------------------------------------------- 初期化
    def _build_toolbar(self):
        tb = QToolBar("操作")
        self.addToolBar(tb)
        tb.addAction("PDFを開く…", self._open_pdf)
        tb.addSeparator()
        tb.addAction("指摘ファイルを開く…", self._open_indications)
        tb.addAction("指摘ファイルを保存…", self._save_indications)
        tb.addAction("指摘ファイルを取り込む(統合)…", self._merge_indications)
        tb.addSeparator()
        tb.addAction("Excelに書き出し…", self._export_excel)
        tb.addSeparator()
        tb.addAction("新旧2画面で比較…", self._open_compare_window)
        tb.addAction("新版PDFと照合(簡易レポート)…", self._match_against_new_version)
        tb.addSeparator()
        list_toggle = self.list_dock.toggleViewAction()
        list_toggle.setText("指摘一覧の表示/非表示")
        tb.addAction(list_toggle)

    # --------------------------------------------------------------- PDF読み込み
    def _open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "資料PDFを開く", "", "PDF Files (*.pdf)")
        if path:
            self.load_pdf(path)

    def load_pdf(self, path: str):
        mode = doc_parser.detect_mode(path)
        if mode != "document":
            QMessageBox.warning(
                self, "非対応のPDF",
                "しおり構成が「メール束PDF」と判定されました。このビューアーは\n"
                "「一般資料PDF(しおり階層)」のみを対象にしています。")
            return
        self.pdf_path = path
        self.document.load(path)
        self.sections = doc_parser.extract_document_sections(path, include_body=False)
        self.tree_model.set_tree(self.sections)
        self.tree.expandAll()
        self.marker_view.set_document(self.document)
        self.marker_mode_btn.setChecked(False)
        self.store = IndicationStore(source_pdf_name=os.path.basename(path))
        self.indication_path = None
        self._unresolved_ids = set()
        self._goto_page(1)
        self._refresh_table()
        self.statusBar().showMessage(f"開きました: {path}", 5000)

    # --------------------------------------------------------------- ページ移動
    def _on_section_clicked(self, index: QModelIndex):
        section = self.tree_model.section_at(index)
        if section:
            self._goto_page(section.start_page)

    def _goto_page(self, page_1based: int):
        self.marker_view.go_to_page(page_1based)
        self._after_page_change()

    def _next_page(self):
        self.marker_view.next_page()
        self._after_page_change()

    def _prev_page(self):
        self.marker_view.prev_page()
        self._after_page_change()

    def _after_page_change(self):
        total = self.marker_view.page_count()
        cur = self.marker_view.current_page_1based()
        self.page_label.setText(f"{cur} / {total}" if total else "- / -")
        self._refresh_markers()

    def _current_section(self) -> "doc_parser.DocSection | None":
        idx = self.tree.currentIndex()
        return self.tree_model.section_at(idx) if idx.isValid() else None

    def _section_for_page(self, page_1based: int) -> "doc_parser.DocSection | None":
        """指定ページを含む最も範囲の狭い(=最も深い)しおり区間を返す。"""
        candidates = [s for s in self.sections if s.start_page <= page_1based <= s.end_page]
        if not candidates:
            return None
        return min(candidates, key=lambda s: s.end_page - s.start_page)

    # --------------------------------------------------------------- マーカー表示
    def _refresh_markers(self):
        page = self.marker_view.current_page_1based()
        markers = []
        for ind in self.store.indications:
            a = ind.anchor
            if a.rel_x is None or a.rel_y is None or a.absolute_page != page:
                continue
            color = CATEGORY_COLORS.get(ind.category, DEFAULT_MARKER_COLOR)
            markers.append((a.rel_x, a.rel_y, color, str(ind.serial), ind.id))
        self.marker_view.set_markers(markers)

    def _on_marker_mode_toggled(self, checked: bool):
        self.marker_view.set_placement_mode(checked)
        self.marker_view.setCursor(Qt.CursorShape.CrossCursor if checked else Qt.CursorShape.ArrowCursor)
        if checked:
            self.statusBar().showMessage(
                "PDF上をクリックした位置にマーカーを置いて指摘を入力します。"
                "もう一度ボタンを押すと解除できます。", 6000)
        else:
            self.statusBar().clearMessage()

    def _note_dialog_closed(self):
        """指摘ダイアログを閉じた直後の時刻を記録する(POST_DIALOG_CLICK_GUARD_SEC参照)。"""
        self._last_dialog_closed_at = time.monotonic()

    def _suppress_marker_click(self) -> bool:
        """ダイアログを閉じた直後のクリック抜け対策。真ならこのクリックは無視してよい。"""
        return (time.monotonic() - self._last_dialog_closed_at) < POST_DIALOG_CLICK_GUARD_SEC

    def _on_marker_clicked(self, rel_x: float, rel_y: float, hit_id):
        if self._suppress_marker_click():
            return
        # 既存マーカーをクリックした場合は、マーカー配置モードの状態に関わらず内容を表示する
        if hit_id is not None:
            indication = self.store.get(hit_id)
            if indication is not None:
                self._edit_indication(indication)
            return

        if not self.marker_mode_btn.isChecked():
            return
        self._add_marker_indication_at(rel_x, rel_y)

    def _on_marker_double_clicked(self, rel_x: float, rel_y: float):
        """PDF上の空欄をダブルクリックしたときに、配置モードのON/OFFに関わらずマーカーを追加する。"""
        if self._suppress_marker_click():
            return
        self._add_marker_indication_at(rel_x, rel_y)

    def _add_marker_indication_at(self, rel_x: float, rel_y: float):
        if not self.pdf_path:
            return
        page = self.marker_view.current_page_1based()
        section = self._section_for_page(page)
        if section is None:
            QMessageBox.warning(self, "しおりが見つかりません",
                                 "このページに対応するしおり区間が見つかりませんでした。")
            return

        label = f"{self._section_label(section.path_titles, section.title)}　(p.{page})"
        dialog = IndicationDialog(label, self, defaults=self._current_defaults())
        # マーカー配置モードは維持する(続けて複数箇所にマーカーを置けるようにするため)。
        # 解除はツールボタンをもう一度押すか、他の操作に切り替えたときにユーザーが行う。
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self._note_dialog_closed()
        if not accepted:
            return

        relative_page = max(1, page - section.start_page + 1) if section.start_page else 1
        anchor = Anchor(
            path_titles=section.path_titles,
            section_title=section.title,
            relative_page=relative_page,
            absolute_page=page,
            section_start_page=section.start_page,
            section_end_page=section.end_page,
            rel_x=rel_x,
            rel_y=rel_y,
            nearby_text=_nearby_text(self.pdf_path, page, rel_x, rel_y),
        )
        indication = Indication.new(
            anchor,
            category=dialog.category_combo.currentText(),
            status=dialog.status_combo.currentText(),
            content=dialog.content_edit.toPlainText(),
            inspector=dialog.inspector_edit.text(),
            round_name=dialog.round_edit.text(),
        )
        self.store.add(indication)
        self._sync_defaults_from_dialog(dialog)
        self._refresh_table()
        self.statusBar().showMessage(
            f"マーカー #{indication.serial} を追加しました。続けてクリックまたはダブルクリックすると追加できます。", 5000)

    # --------------------------------------------------------------- 既定値バー(区分・状態・回次・指摘者)
    def _current_defaults(self) -> dict:
        return {
            "category": self.default_category_combo.currentText(),
            "status": self.default_status_combo.currentText(),
            "round_name": self.default_round_edit.text(),
            "inspector": self.default_inspector_edit.text(),
        }

    def _sync_defaults_from_dialog(self, dialog: "IndicationDialog"):
        """ダイアログで実際に選ばれた値を次回以降の既定値にする(「前回使った値」が既定になる)。"""
        self.default_category_combo.setCurrentText(dialog.category_combo.currentText())
        self.default_status_combo.setCurrentText(dialog.status_combo.currentText())
        self.default_round_edit.setText(dialog.round_edit.text())
        self.default_inspector_edit.setText(dialog.inspector_edit.text())

    # --------------------------------------------------------------- 指摘の追加(位置指定なし)・編集
    def _section_label(self, path_titles: str, title: str) -> str:
        return f"{path_titles} › {title}" if path_titles else title

    def _add_indication_for_section(self):
        section = self._current_section()
        if section is None:
            QMessageBox.information(self, "しおりを選択してください",
                                     "左のしおり階層から、指摘の対象区間を選んでください。")
            return
        dialog = IndicationDialog(self._section_label(section.path_titles, section.title), self,
                                   defaults=self._current_defaults())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        current_page = self.marker_view.current_page_1based()
        if section.start_page and section.start_page <= current_page <= section.end_page:
            absolute_page = current_page
        else:
            absolute_page = section.start_page
        relative_page = max(1, absolute_page - section.start_page + 1) if section.start_page else 1

        anchor = Anchor(
            path_titles=section.path_titles,
            section_title=section.title,
            relative_page=relative_page,
            absolute_page=absolute_page,
            section_start_page=section.start_page,
            section_end_page=section.end_page,
        )
        indication = Indication.new(
            anchor,
            category=dialog.category_combo.currentText(),
            status=dialog.status_combo.currentText(),
            content=dialog.content_edit.toPlainText(),
            inspector=dialog.inspector_edit.text(),
            round_name=dialog.round_edit.text(),
        )
        self.store.add(indication)
        self._sync_defaults_from_dialog(dialog)
        self._refresh_table()

    def _edit_indication(self, indication: Indication):
        label = self._section_label(indication.anchor.path_titles, indication.anchor.section_title)
        dialog = IndicationDialog(label, self, existing=indication)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self._note_dialog_closed()
        if not accepted:
            return
        if dialog.delete_requested:
            serial = indication.serial
            self.store.delete(indication.id)
            self._unresolved_ids.discard(indication.id)
            self._refresh_table()
            self.statusBar().showMessage(f"指摘 #{serial} を削除しました。", 5000)
            return
        self.store.update(
            indication.id,
            category=dialog.category_combo.currentText(),
            status=dialog.status_combo.currentText(),
            content=dialog.content_edit.toPlainText(),
            inspector=dialog.inspector_edit.text(),
            round_name=dialog.round_edit.text(),
        )
        self._refresh_table()

    # --------------------------------------------------------------- 一覧
    def _filtered_indications(self) -> list[Indication]:
        cat = self.category_filter.currentText()
        st = self.status_filter.currentText()
        rows = self.store.indications
        if cat != "(すべての区分)":
            rows = [i for i in rows if i.category == cat]
        if st != "(すべての状態)":
            rows = [i for i in rows if i.status == st]
        return sorted(rows, key=lambda i: i.serial)

    def _refresh_table(self):
        rows = self._filtered_indications()
        self.table.setRowCount(len(rows))
        for r, ind in enumerate(rows):
            values = [str(ind.serial), ind.category, ind.status, ind.anchor.full_path,
                      ind.content[:60], ind.inspector]
            unresolved = ind.id in self._unresolved_ids
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, ind.id)
                if unresolved:
                    item.setForeground(QColor("#C0392B"))
                    item.setToolTip("現在のPDFに位置を合わせられませんでした(元の位置のまま表示しています)")
                self.table.setItem(r, c, item)
        self._refresh_markers()

    def _indication_for_row(self, row: int) -> "Indication | None":
        item = self.table.item(row, 0)
        if item is None:
            return None
        return self.store.get(item.data(Qt.ItemDataRole.UserRole))

    def _on_table_clicked(self, row: int, _col: int):
        """行をクリックしただけで該当ページへ移動し、座標指定ありの指摘ならPDF上のマーカーを一時ハイライトする
        (番号を見つけやすくするため)。"""
        indication = self._indication_for_row(row)
        if indication is None:
            return
        page = indication.anchor.absolute_page or indication.anchor.section_start_page
        if page:
            self._goto_page(page)
        if indication.anchor.rel_x is not None and indication.anchor.rel_y is not None:
            self.marker_view.highlight_marker(indication.id)

    def _on_table_double_clicked(self, row: int, _col: int):
        indication = self._indication_for_row(row)
        if indication is None:
            return
        page = indication.anchor.absolute_page or indication.anchor.section_start_page
        if page:
            self._goto_page(page)
        self._edit_indication(indication)

    # --------------------------------------------------------------- ファイル入出力
    def _open_indications(self):
        """指摘ファイルを開く。記録時と別のPDF(再提出版など)を開いている場合は、
        matching.pyの照合ロジックで現在のPDFのしおり構成に位置を合わせ直す。"""
        path, _ = QFileDialog.getOpenFileName(self, "指摘ファイルを開く", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            loaded = IndicationStore.load(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "読み込みに失敗しました", str(e))
            return

        current_name = os.path.basename(self.pdf_path) if self.pdf_path else ""
        if self.pdf_path and self.sections and loaded.source_pdf_name and loaded.source_pdf_name != current_name:
            unresolved = self._reanchor_to_current_pdf(loaded)
            self.store = loaded
            self.indication_path = None  # 別PDF向けの内容なので、保存時は新しい名前を確認させる
            self._unresolved_ids = unresolved
            msg = (f"「{loaded.source_pdf_name}」用の指摘ファイルを現在のPDFに合わせて読み込みました"
                   f"({len(loaded.indications) - len(unresolved)}件を再照合")
            msg += f"、{len(unresolved)}件は照合できず元の位置のままです)。" if unresolved else "、全件照合できました)。"
            QMessageBox.information(self, "指摘ファイルを読み込みました(別PDF)", msg)
        else:
            self.store = loaded
            self.indication_path = path
            self._unresolved_ids = set()
            self.statusBar().showMessage(f"指摘ファイルを読み込みました: {path}", 5000)
        self._refresh_table()

    def _reanchor_to_current_pdf(self, store: IndicationStore) -> set[str]:
        """storeの各指摘アンカーを、現在開いているPDFのしおり構成に合わせて上書きする。
        座標(rel_x/rel_y)はページ内比率なので、しおりの新しい開始ページに合わせて絶対ページだけ
        再計算すればよい。照合できなかったものは動かさず、idを戻り値の集合に入れて呼び出し側で
        明示できるようにする(matching.pyの方針: 分からないものは分からないと示す。動かさない)。"""
        sections = doc_parser.extract_document_sections(self.pdf_path)
        unresolved: set[str] = set()
        for ind in store.indications:
            result = match_anchor(ind.anchor, sections)
            if result.section is None:
                unresolved.add(ind.id)
                continue
            s = result.section
            a = ind.anchor
            span = max(1, s.end_page - s.start_page + 1)
            relative_page = min(a.relative_page or 1, span)
            a.path_titles = s.path_titles
            a.section_title = s.title
            a.section_start_page = s.start_page
            a.section_end_page = s.end_page
            a.relative_page = relative_page
            a.absolute_page = s.start_page + relative_page - 1
        store.source_pdf_name = os.path.basename(self.pdf_path)
        return unresolved

    def _save_indications(self):
        if not self.pdf_path:
            QMessageBox.information(self, "PDFが開かれていません", "先にPDFを開いてください。")
            return
        default_name = os.path.splitext(os.path.basename(self.pdf_path))[0] + "_指摘.json"
        default_path = self.indication_path or os.path.join(os.path.dirname(self.pdf_path), default_name)
        path, _ = QFileDialog.getSaveFileName(self, "指摘ファイルを保存", default_path, "JSON Files (*.json)")
        if not path:
            return
        self.store.source_pdf_name = os.path.basename(self.pdf_path)
        self.store.save(path)
        self.indication_path = path
        self.statusBar().showMessage(f"保存しました: {path}", 5000)

    def _merge_indications(self):
        path, _ = QFileDialog.getOpenFileName(self, "取り込む指摘ファイルを選択(統合)", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            other = IndicationStore.load(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "読み込みに失敗しました", str(e))
            return

        current_name = os.path.basename(self.pdf_path) if self.pdf_path else ""
        unresolved: set[str] = set()
        if self.pdf_path and self.sections and other.source_pdf_name and other.source_pdf_name != current_name:
            unresolved = self._reanchor_to_current_pdf(other)

        added_ids = {i.id for i in other.indications} - {i.id for i in self.store.indications}
        added = self.store.merge(other)
        self._unresolved_ids |= (unresolved & added_ids)
        self._refresh_table()
        msg = f"{added}件の指摘を新たに取り込みました。"
        if unresolved & added_ids:
            msg += f"\nうち{len(unresolved & added_ids)}件は現在のPDFに位置を合わせられず、元の位置のままです。"
        QMessageBox.information(self, "取り込み完了", msg)

    def _export_excel(self):
        if not self.store.indications:
            QMessageBox.information(self, "指摘がありません", "書き出す指摘がまだありません。")
            return
        default_name = (os.path.splitext(os.path.basename(self.pdf_path))[0] + "_指摘一覧.xlsx"
                         if self.pdf_path else "指摘一覧.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "Excelに書き出し", default_name, "Excel Files (*.xlsx)")
        if not path:
            return
        export_excel(self.store.indications, path, document_title=self.store.source_pdf_name)
        self.statusBar().showMessage(f"書き出しました: {path}", 5000)

    # --------------------------------------------------------------- フェーズ2: 新旧2画面表示
    def _open_compare_window(self):
        if not self.pdf_path:
            QMessageBox.information(self, "PDFが開かれていません", "先に旧版のPDFを開いてください。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "比較する新版PDFを選択", "", "PDF Files (*.pdf)")
        if not path:
            return
        if doc_parser.detect_mode(path) != "document":
            QMessageBox.warning(self, "非対応のPDF", "一般資料PDF(しおり階層)のみ比較できます。")
            return
        from compare_window import CompareWindow  # kensa_windowとの循環importを避けるための遅延import
        self._compare_window = CompareWindow(self.pdf_path, path, self.store, parent=None)
        self._compare_window.show()

    # --------------------------------------------------------------- 照合(簡易レポート)
    def _match_against_new_version(self):
        if not self.store.indications:
            QMessageBox.information(self, "指摘がありません", "照合する指摘がまだありません。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "照合する新版PDFを選択(試験機能)", "", "PDF Files (*.pdf)")
        if not path:
            return
        if doc_parser.detect_mode(path) != "document":
            QMessageBox.warning(self, "非対応のPDF", "一般資料PDF(しおり階層)のみ照合できます。")
            return
        sections = doc_parser.extract_document_sections(path)
        results = [(ind, match_anchor(ind.anchor, sections))
                   for ind in sorted(self.store.indications, key=lambda i: i.serial)]
        self._show_match_report(os.path.basename(path), results)

    def _show_match_report(self, target_name: str, results):
        dialog = QDialog(self)
        dialog.setWindowTitle(f"照合結果(試験機能): {target_name}")
        dialog.resize(780, 480)

        table = QTableWidget(len(results), 5)
        table.setHorizontalHeaderLabels(["No", "しおりパス", "結果", "詳細", "照合先ページ"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        for r, (ind, result) in enumerate(results):
            page = str(result.section.start_page) if result.section else "―"
            values = [str(ind.serial), ind.anchor.full_path, MATCH_STATUS_LABELS[result.status],
                      result.note, page]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if result.status == "unresolved":
                    item.setForeground(QColor("#C0392B"))
                table.setItem(r, c, item)

        layout = QVBoxLayout(dialog)
        note = QLabel("差分表示ではなく、指摘の位置合わせの当たりをつける試験機能です。"
                       "「照合できず」は必ず人が確認してください。")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(table)
        dialog.exec()
