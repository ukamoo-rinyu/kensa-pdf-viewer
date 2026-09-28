"""指摘（検査コメント）のデータモデルと保存/読込。

設計方針:
- 原本PDFは一切書き換えない。指摘一式は独立したJSONファイルとして持ち回りでき、
  複数検査員が個別に記録したファイルを後から統合できる。
- 位置はページ番号ではなく、しおりパス(DocSection.path_titles)を軸にしたアンカーで持つ
  (差し替えでページが増減しても、指摘が別の場所を指さないようにするため)。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime

FORMAT_VERSION = 1

CATEGORIES = ["要修正", "要確認", "軽微", "質問"]
STATUSES = ["未対応", "修正済", "確認済", "再指摘"]


@dataclass
class Anchor:
    """指摘の位置(ページ番号に依存しない)。"""
    path_titles: str             # 祖先しおりのパンくず(DocSection.path_titlesそのまま)
    section_title: str           # 対象しおり自身のタイトル
    relative_page: int           # しおり区間の先頭から何ページ目か(1始まり)
    nearby_text: str = ""        # 座標周辺のテキスト(しおり名が変わった場合の再照合の保険)
    rel_x: float | None = None   # ページ内の位置(幅に対する0〜1の比率)。座標指定なしの指摘はNone
    rel_y: float | None = None   # 同上(高さに対する比率)
    absolute_page: int = 0       # 記録時点の実ページ番号(表示・印刷用の参考値。照合には使わない)
    section_start_page: int = 0  # 記録時点のしおり区間(参考値)
    section_end_page: int = 0

    @property
    def full_path(self) -> str:
        return f"{self.path_titles} › {self.section_title}" if self.path_titles else self.section_title


@dataclass
class Indication:
    """1件の指摘。"""
    id: str
    anchor: Anchor
    category: str = "要確認"
    status: str = "未対応"
    content: str = ""
    inspector: str = ""
    created_at: str = ""
    round_name: str = "初回"
    related_id: str | None = None  # 再指摘の場合、元の指摘id
    serial: int = 0                # 表示用の通し番号。IndicationStoreがまとめて採番する

    @staticmethod
    def new(anchor: Anchor, **kwargs) -> "Indication":
        return Indication(
            id=uuid.uuid4().hex[:12],
            anchor=anchor,
            created_at=datetime.now().isoformat(timespec="seconds"),
            **kwargs,
        )


class IndicationStore:
    """1提出物ぶんの指摘一式。JSONファイル1本で持ち回りできる。"""

    def __init__(self, source_pdf_name: str = ""):
        self.source_pdf_name = source_pdf_name
        self.indications: list[Indication] = []

    # ------------------------------------------------------------ CRUD
    def add(self, indication: Indication) -> None:
        self.indications.append(indication)
        self.recompute_serials()

    def update(self, indication_id: str, **changes) -> None:
        ind = self.get(indication_id)
        if ind is None:
            return
        for key, value in changes.items():
            setattr(ind, key, value)

    def delete(self, indication_id: str) -> None:
        self.indications = [i for i in self.indications if i.id != indication_id]
        self.recompute_serials()

    def get(self, indication_id: str) -> Indication | None:
        return next((i for i in self.indications if i.id == indication_id), None)

    def recompute_serials(self) -> None:
        """絶対ページ→記録日時の順で通し番号を振り直す。受注者に渡す指摘番号として使う。"""
        ordered = sorted(self.indications, key=lambda i: (i.anchor.absolute_page, i.created_at))
        for n, ind in enumerate(ordered, start=1):
            ind.serial = n

    # ------------------------------------------------------------ 統合
    def merge(self, other: "IndicationStore") -> int:
        """他の検査員のファイルを取り込む。idで重複判定するため、同じファイルを
        何度取り込んでも安全(冪等)。戻り値は新規に追加された件数。"""
        existing_ids = {i.id for i in self.indications}
        added = 0
        for ind in other.indications:
            if ind.id not in existing_ids:
                self.indications.append(ind)
                existing_ids.add(ind.id)
                added += 1
        self.recompute_serials()
        return added

    # ------------------------------------------------------------ 保存/読込
    def save(self, path: str) -> None:
        data = {
            "format_version": FORMAT_VERSION,
            "source_pdf_name": self.source_pdf_name,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "indications": [asdict(i) for i in self.indications],
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    @staticmethod
    def load(path: str) -> "IndicationStore":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        store = IndicationStore(source_pdf_name=data.get("source_pdf_name", ""))
        for row in data.get("indications", []):
            anchor = Anchor(**row["anchor"])
            store.indications.append(Indication(**{**row, "anchor": anchor}))
        store.recompute_serials()
        return store
