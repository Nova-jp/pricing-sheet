"""
分析用に、ヒストリカルDB(data/historical.db)の生データを「日付 → {銘柄: 値}」の表として読む。

■ データセット
  - swap: quotes(Midシートの全テナーの生ミッド%。M1〜M8を含む)。O/N はその日のTONA実績
          (fixings、フィキシング対象日=その日)。カーブは使わない(PCAの対象は市場の気配そのもの)。
  - vol : vol_quotes(スワップションATMノーマルvol、bp/年)。銘柄は「満期x原資産」(例 1Yx10Y)。
  - どちらも日本の非営業日(ql.Japan)の行は使わない(カーブDBと同じ。祝日にもLSEGの値が
    一部入っているため)。
  - 銘柄名は突き合わせ用に大文字に揃える(12M、1YX10Y等)。表示はExcelから渡された表記を使う。

■ キャッシュ
  DBファイルの更新時刻が変わるまで、読み込んだ表をプロセス内に持つ
  (朝のバッチでDBが更新されれば次の呼び出しで読み直す)。
"""

from datetime import date
from typing import Dict, Optional, Tuple

import QuantLib as ql

from swap_pricing.conventions import CALENDAR
from swap_pricing.curve_store import CurveStore

ON_TENOR = "O/N"
SWAP = "SWAP"
VOL = "VOL"
_DATASETS = {"SWAP": SWAP, "スワップ": SWAP, "VOL": VOL, "ボラ": VOL}

# 値の単位 → bp の倍率(固有値・得点・差分をbpで表すため)
UNIT_BP = {SWAP: 100.0, VOL: 1.0}

History = Dict[date, Dict[str, float]]

_cache: Dict[Tuple[str, str, int], History] = {}


def normalize_label(label) -> str:
    return str(label).strip().upper()


def parse_dataset(value) -> str:
    dataset = _DATASETS.get(normalize_label(value or SWAP))
    if dataset is None:
        raise ValueError(f"データセットは swap / vol です: {value!r}")
    return dataset


def _raw(dataset: str, store: CurveStore) -> History:
    if dataset == VOL:
        return store.vol_quotes()
    quotes = store.quotes()
    fixings = store.fixings()
    raw: History = {}
    for d in set(quotes) | set(fixings):
        row = dict(quotes.get(d, {}))
        if d in fixings:
            row[ON_TENOR] = fixings[d]
        raw[d] = row
    return raw


def load_history(dataset: str = SWAP, store: Optional[CurveStore] = None) -> History:
    """日付(日本の営業日のみ)→ {銘柄(大文字): 値}。"""
    store = store or CurveStore()
    if not store.exists():
        raise ValueError(
            f"ヒストリカルDBがありません: {store.db_path}(python -m swap_pricing.curve_batch で作成)"
        )
    key = (dataset, str(store.db_path), store.db_path.stat().st_mtime_ns)
    if key not in _cache:
        history: History = {}
        for d, row in sorted(_raw(dataset, store).items()):
            if row and CALENDAR.isBusinessDay(ql.Date.from_date(d)):
                history[d] = {normalize_label(k): v for k, v in row.items()}
        for k in [k for k in _cache if k[0] == dataset]:
            del _cache[k]  # データセットごとに直近の1つだけ保持
        _cache[key] = history
    return _cache[key]
