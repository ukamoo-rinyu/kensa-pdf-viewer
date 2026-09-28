"""新旧比較画面を、ファイル選択なしで直接開く(動作確認用)。

使い方: python open_compare.py [旧版PDF] [新版PDF] [指摘ファイル(JSON)]
引数を省略すると、京都市.pdf と make_kyoto_new.py で作った新版、京都市_指摘1.json を開く。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..")))

from PySide6.QtWidgets import QApplication  # noqa: E402

from compare_window import CompareWindow  # noqa: E402
from indications import IndicationStore  # noqa: E402


def main():
    args = sys.argv[1:]
    old = args[0] if len(args) > 0 else os.path.join(ROOT, "京都市.pdf")
    new = args[1] if len(args) > 1 else os.path.join(HERE, "out", "kyoto_new.pdf")
    ind = args[2] if len(args) > 2 else os.path.join(ROOT, "京都市_指摘1.json")
    if not os.path.exists(new):
        sys.exit(f"新版がありません: {new}\n先に make_kyoto_new.py を実行してください")

    app = QApplication(sys.argv)
    store = IndicationStore.load(ind) if ind and os.path.exists(ind) else IndicationStore()
    win = CompareWindow(old, new, store)
    win.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
