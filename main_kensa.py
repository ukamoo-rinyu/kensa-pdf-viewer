"""検査PDFビューアーのエントリポイント。

使い方: python main_kensa.py [開くPDFのパス]
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from kensa_window import KensaWindow


def main():
    app = QApplication(sys.argv)
    win = KensaWindow()
    win.showMaximized()
    if len(sys.argv) > 1:
        win.load_pdf(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
