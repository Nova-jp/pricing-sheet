"""
評価日のマーケット(カーブの元になるレート一式)と、プライシング・デルタ計算で共有する
カーブ一式(基準カーブと+1bpしたカーブ)を管理するモジュール。

■ 評価日によるデータ元の切り替え(udf._market が判定)
  - 評価日が今日: RealTimeシートのライブのレート(B列フラグ≠0のテナー)
  - 評価日が過去: ヒストリカルDB(data/historical.db)に取り込み済みのその日のレートのうち、
    RealTimeシートのフラグで選んだテナー(今日と同じグリッドにするため)。O/Nはその日の
    TONA実績。DBにない日はエラー
  どちらも同じ MarketSnapshot(評価日+レート一式)にしてから、同じ関数でカーブを組む。

■ カーブ一式の共有(CurveSet)
  pricingシートの全行・全UDFで、同じスナップショットなら同じカーブを使い回す。
  - base: 基準カーブ
  - parallel(): カーブに使っている全テナーを+1bpしたカーブ(SwapDelta用)
  - bucket(ラベル): そのテナーだけ+1bpしたカーブ(RiskGrid用)
  バンプしたカーブは初めて必要になったときに1回だけ作る。バンプするのは実際にカーブに
  使っているテナーだけ(フラグ0のテナーや会合スワップのような未使用のテナーは作らない)。
  以前は行ごと・UDFごとにバンプしたカーブを作り直していた(SwapDeltaで1行2回、
  行ごとのバケットデルタで1行42回のブートストラップ)。

  QuantLibのカーブは評価日(グローバル設定)に連動しており、ヒストリカル計算などで評価日が
  切り替わった後の最初の利用時には再計算される(値は同じ。price_swapが入口で評価日を戻す)。
"""

from collections import OrderedDict
from datetime import date
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from swap_pricing.curve import BootstrapResult, bootstrap_curve, normalize_tenor
from swap_pricing.curve_store import CurveStore

BUMP_BP = 1.0
ON_LABEL = "o/n"
_CACHE_SIZE = 4  # 今日のRealTime・過去日など、直近のスナップショットだけ保持する


class MarketSnapshot(NamedTuple):
    valuation_date: date
    quotes: Tuple[Tuple[str, float], ...]  # (テナーのラベル(小文字), レート%)。キャッシュのキーにする
    source: str  # "RealTime" / "DB"


def realtime_snapshot(valuation_date: date, rates: Dict[str, float]) -> MarketSnapshot:
    """rates: RealTimeシートでフラグ≠0のテナーのレート(udf._rates_from_range の結果)。"""
    return MarketSnapshot(valuation_date, tuple(sorted(rates.items())), "RealTime")


def db_snapshot(valuation_date: date, labels: Sequence[str], store: CurveStore) -> MarketSnapshot:
    """過去の評価日: DBのその日のレートのうち labels(RealTimeのフラグで選んだテナー)だけ。
    O/Nはその日のTONA実績。DBに値が無いテナーは外す(あるテナーだけでカーブを引く)。"""
    day_quotes = {k.strip().lower(): v for k, v in store.quotes_on(valuation_date).items()}
    if not day_quotes:
        raise ValueError(
            f"評価日{valuation_date}のレートがヒストリカルDBにありません(休日か、取り込み期間外)"
        )
    fixing = store.fixings().get(valuation_date)
    quotes = {}
    for label in labels:
        key = label.strip().lower()
        value = fixing if key == ON_LABEL else day_quotes.get(key)
        if value is not None:
            quotes[key] = value
    return MarketSnapshot(valuation_date, tuple(sorted(quotes.items())), "DB")


class CurveSet:
    """1つのスナップショットに対する基準カーブと、+1bpしたカーブ(必要になったら作る)。"""

    def __init__(self, snapshot: MarketSnapshot):
        self.snapshot = snapshot
        self.valuation_date = snapshot.valuation_date
        self._quotes = dict(snapshot.quotes)
        self.base: BootstrapResult = bootstrap_curve(self.valuation_date, self._quotes)
        used = set(self.base.used_tenors)
        # バンプの対象 = 実際にカーブに使っているテナーだけ(入力のラベルのまま)
        self.bump_labels: List[str] = [k for k in self._quotes if normalize_tenor(k) in used]
        self._parallel: Optional[BootstrapResult] = None
        self._bucket: Dict[str, BootstrapResult] = {}

    def _bumped(self, labels) -> BootstrapResult:
        bumped = dict(self._quotes)
        for label in labels:
            bumped[label] += BUMP_BP / 100.0
        return bootstrap_curve(self.valuation_date, bumped)

    def parallel(self) -> BootstrapResult:
        if self._parallel is None:
            self._parallel = self._bumped(self.bump_labels)
        return self._parallel

    def bucket(self, label: str) -> BootstrapResult:
        if label not in self._bucket:
            if label not in self.bump_labels:
                raise ValueError(f"カーブに使っていないテナーはバンプできません: {label}")
            self._bucket[label] = self._bumped([label])
        return self._bucket[label]


_cache: "OrderedDict[MarketSnapshot, CurveSet]" = OrderedDict()


def curve_set(snapshot: MarketSnapshot) -> CurveSet:
    """スナップショットのカーブ一式(同じ内容なら使い回す)。"""
    if snapshot in _cache:
        _cache.move_to_end(snapshot)
        return _cache[snapshot]
    result = CurveSet(snapshot)
    _cache[snapshot] = result
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return result
