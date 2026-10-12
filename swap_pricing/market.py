"""
評価日のマーケット(カーブの元になるレート一式)と、プライシング・デルタ計算で共有する
カーブ一式(基準カーブと+1bpしたカーブ)を管理するモジュール。

■ 評価日によるデータ元の切り替え(udf._market が判定)
  - 評価日が今日: RealTimeシートのライブのレート(B列フラグ≠0のテナー)
  - 評価日が過去: ヒストリカルDB(data/historical.db)に取り込み済みのその日のレートのうち、
    RealTimeシートのフラグで選んだテナー(今日と同じグリッドにするため)。O/Nはその日の
    TONA実績。DBにない日はエラー
  どちらも同じ MarketSnapshot(評価日+レート一式+TONA実績)にしてから、同じ関数でカーブを組む。
  TONA実績(過去起算の計算に使う)はどちらの場合もヒストリカルDBから読む(db_fixings。
  DBファイルが変わるまで=朝のバッチまで使い回す。DBが無ければ空で、過去起算だけがエラーになる)。

■ カーブ一式の共有(CurveSet)
  pricingシートの全行・全UDFで、同じスナップショットなら同じカーブを使い回す。
  計算に渡す市場データ(market_data.Market=カーブ+実績)を返す。バンプしても実績は同じ。
  - base: 基準カーブの市場データ
  - parallel(): カーブに使っている全テナーを+1bpしたもの(SwapDelta用)
  - bucket(ラベル): そのテナーだけ+1bpしたもの(RiskGrid用)
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
from swap_pricing.historical_store import HistoricalStore
from swap_pricing.market_data import Market

BUMP_BP = 1.0
ON_LABEL = "o/n"
_CACHE_SIZE = 4  # 今日のRealTime・過去日など、直近のスナップショットだけ保持する


class MarketSnapshot(NamedTuple):
    valuation_date: date
    quotes: Tuple[Tuple[str, float], ...]  # (テナーのラベル(小文字), レート%)。キャッシュのキーにする
    source: str  # "RealTime" / "DB"
    fixings: Tuple[Tuple[date, float], ...] = ()  # TONA実績(評価日より前)。キャッシュのキーにも入る


def _before(fixings: Dict[date, float], valuation_date: date) -> Tuple[Tuple[date, float], ...]:
    return tuple(sorted((d, v) for d, v in fixings.items() if d < valuation_date))


_db_fixings_cache: Dict[tuple, Dict[date, float]] = {}


def db_fixings(store: HistoricalStore) -> Dict[date, float]:
    """DBのTONA実績(全期間)。DBファイルが変わるまで使い回す(全UDFが呼ぶため)。DBが無ければ空。"""
    if not store.exists():
        return {}
    stat = store.db_path.stat()
    key = (str(store.db_path), stat.st_mtime_ns, stat.st_size)
    if key not in _db_fixings_cache:
        _db_fixings_cache.clear()  # 直近1件だけ保持
        _db_fixings_cache[key] = store.fixings()
    return _db_fixings_cache[key]


def realtime_snapshot(
    valuation_date: date, rates: Dict[str, float], fixings: Optional[Dict[date, float]] = None,
) -> MarketSnapshot:
    """rates: RealTimeシートでフラグ≠0のテナーのレート(udf._rates_from_range の結果)。
    fixings: TONA実績(通常は db_fixings)。過去起算の計算にだけ使う。"""
    return MarketSnapshot(
        valuation_date, tuple(sorted(rates.items())), "RealTime", _before(fixings or {}, valuation_date),
    )


def db_snapshot(valuation_date: date, labels: Sequence[str], store: HistoricalStore) -> MarketSnapshot:
    """過去の評価日: DBのその日のレートのうち labels(RealTimeのフラグで選んだテナー)だけ。
    O/Nはその日のTONA実績。DBに値が無いテナーは外す(あるテナーだけでカーブを引く)。"""
    day_quotes = {k.strip().lower(): v for k, v in store.quotes_on(valuation_date).items()}
    if not day_quotes:
        raise ValueError(
            f"評価日{valuation_date}のレートがヒストリカルDBにありません(休日か、取り込み期間外)"
        )
    all_fixings = db_fixings(store)
    fixing = all_fixings.get(valuation_date)
    quotes = {}
    for label in labels:
        key = label.strip().lower()
        value = fixing if key == ON_LABEL else day_quotes.get(key)
        if value is not None:
            quotes[key] = value
    return MarketSnapshot(
        valuation_date, tuple(sorted(quotes.items())), "DB", _before(all_fixings, valuation_date),
    )


class CurveSet:
    """1つのスナップショットに対する基準カーブと、+1bpしたカーブ(必要になったら作る)。
    計算には、カーブにスナップショットのTONA実績を添えた市場データ(Market)を渡す。"""

    def __init__(self, snapshot: MarketSnapshot):
        self.snapshot = snapshot
        self.valuation_date = snapshot.valuation_date
        self._quotes = dict(snapshot.quotes)
        self._fixings = dict(snapshot.fixings)
        self.base_curve: BootstrapResult = bootstrap_curve(self.valuation_date, self._quotes)
        self.spot_date = self.base_curve.spot_date
        self.base: Market = self._market(self.base_curve)
        used = set(self.base_curve.used_tenors)
        # バンプの対象 = 実際にカーブに使っているテナーだけ(入力のラベルのまま)
        self.bump_labels: List[str] = [k for k in self._quotes if normalize_tenor(k) in used]
        self._parallel: Optional[Market] = None
        self._bucket: Dict[str, Market] = {}

    def _market(self, boot: BootstrapResult) -> Market:
        return Market(self.valuation_date, boot.curve, boot.index, self._fixings)

    def _bumped(self, labels) -> Market:
        bumped = dict(self._quotes)
        for label in labels:
            bumped[label] += BUMP_BP / 100.0
        return self._market(bootstrap_curve(self.valuation_date, bumped))

    def parallel(self) -> Market:
        if self._parallel is None:
            self._parallel = self._bumped(self.bump_labels)
        return self._parallel

    def bucket(self, label: str) -> Market:
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
