# Third-party notices / 使用ライブラリのライセンス

この文書は kensa-pdf-viewer の依存物を確認するための一覧です。`LICENSE` は kensa-pdf-viewer 本体に適用する GNU AGPL v3 の全文です。依存物の権利は各権利者に帰属し、それぞれのライセンスが適用されます。本書は各ライセンスの全文の代わりにはなりません。

このリポジトリはソースコードのみを配布しており、以下のライブラリは同梱していません。利用者が `pip install -r requirements.txt` で各配布元から取得します。

確認対象: `requirements.txt`、アプリの import、ローカル環境のパッケージメタデータ(2026-09-28)。`requirements.txt` は下限指定なので、実際に入る版は環境により変わります。

| 用途 | パッケージ・確認した版 | ライセンスと確認先 |
| --- | --- | --- |
| PDF の解析・描画・文字抽出 | PyMuPDF 1.27.2 / MuPDF | [AGPL v3 または Artifex 商用ライセンス](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)。本体を AGPL v3 としているのはこのため。商用ライセンスを取得済みであるとの表明ではありません。 |
| GUI と PDF 表示 | PySide6 6.11.2 (Qt for Python) | パッケージのメタデータは `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`。[Qt for Python](https://doc.qt.io/qtforpython-6/)。使用する Qt PDF は [LGPL v3 / GPL v2 / 商用](https://doc.qt.io/qt-6/qtpdf-licensing.html)で、PDFium などの第三者コードを含みます。 |
| Excel 書き出し | openpyxl 3.1.5 | [MIT](https://foss.heptapod.net/openpyxl/openpyxl) |
| Python 実行環境 | CPython 3.10 以降 | [PSF License](https://docs.python.org/3/license.html) |

`pdf_sections.py` は、同じ作者による [mail-pdf-viewer](https://github.com/ukamoo-rinyu/mail-pdf-viewer) の `parser.py` から必要な部分を取り込んだものです。

この一覧は確認した資料に基づく整理であり、個々の利用・配布形態の適法性を断定するものではありません。
