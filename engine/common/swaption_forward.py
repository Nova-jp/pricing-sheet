"""
スワップションの原資産のFwd(DBの swaption_forwards)を、保存済みの日次カーブから計算する(朝バッチの⑧)。

■ 何を持つか
  日付×銘柄(満期x原資産、格子の全銘柄)ごとに、その日の契約(engine/common/swaption.underlying。volの換算と
  同じ契約)の満期日・起算日・原資産の満期日・営業日数・Fwd(%)・年金と、同じ契約を前の日付のカーブで評価した
  Fwd(forward_prev)。日次変化 = forward − forward_prev は同じ契約どうしの差なのでロールダウンを含まない
  (VolHVのHV)。前の日付はDBでカーブのある直前の日(最初の日付は NULL)。
  回帰(VolLevel)・窓の集計(VolHV)は読む側(Excelの数式)で行い、ここには置かない。

■ 計算し直す日
  入力のハッシュ(計算方法・その日と前の日付のカーブの入力ハッシュ・銘柄)が変わった日だけ。カーブを
  作り直した日は、その日と次の日付(forward_prev が変わる)が計算し直しになる。

■ 速くするための工夫
  engine.common.forward.ForwardPricer で同じ契約のスワップを使い回す(結果は swap_pricer.par_rate と同じ)。
  カーブは日付ごとに必要な期間(until)だけ読む。
"""

import hashlib
import json
from datetime import date, timedelta
from typing import Dict, List, Optional, Protocol, Sequence

from engine.common.forward import ForwardPricer, last_date
from engine.common.historical_store import SwaptionForwardRow
from engine.common.swaption import Underlying, annuity, underlying

# 計算方法の名前(契約の決め方・列の定義を変えたら変える。全日付を計算し直す)
FORWARD_METHOD = "swaption-underlying-v1"
_HORIZON_MARGIN = timedelta(days=40)  # カーブを読む範囲の余裕(満期日の休日調整分)


class CurveSource(Protocol):
    def dates(self) -> List[date]: ...

    def load(self, as_of_date: date, until: Optional[date] = None): ...


def input_hash(curve_hash: str, prev_curve_hash: Optional[str], labels: Sequence[str]) -> str:
    payload = json.dumps({"method": FORWARD_METHOD, "curve": curve_hash, "prev": prev_curve_hash,
                          "labels": sorted(labels)})
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_day(
    curves: CurveSource, as_of_date: date, prev_date: Optional[date], labels: Sequence[str],
) -> List[SwaptionForwardRow]:
    """as_of_date の契約の行。満期日を過ぎた契約(営業日数 0 以下)は無いので、格子の銘柄は全て行になる。"""
    contracts: List[Underlying] = [underlying(as_of_date, label) for label in labels]
    until = max(last_date(u.terms) for u in contracts) + _HORIZON_MARGIN
    pricer = ForwardPricer()

    prev: List[Optional[float]] = [None] * len(contracts)
    if prev_date is not None:
        pricer.set_curve(prev_date, curves.load(prev_date, until=until).curve)
        prev = [pricer.rate(u.terms) for u in contracts]

    curve = curves.load(as_of_date, until=until).curve
    pricer.set_curve(as_of_date, curve)
    rows = []
    for u, p in zip(contracts, prev):
        swap = pricer.swap(u.terms)
        rows.append(SwaptionForwardRow(
            u.label, u.expiry, u.terms.start, swap.maturityDate().to_date(), u.days,
            swap.fairRate() * 100.0, annuity(swap, curve), p,
        ))
    return rows


def build(store, labels: Sequence[str], rebuild: bool = False, log=print) -> Dict[date, str]:
    """store(historical_store.HistoricalStore)のカーブのある全日付について swaption_forwards を作る。
    入力が変わった日だけ計算する。失敗した日 → 内容 を返す。"""
    curve_hashes = store.input_hashes()
    existing = {} if rebuild else store.swaption_forward_hashes()
    dates = sorted(curve_hashes)
    built = 0
    failed: Dict[date, str] = {}
    with store:
        for i, d in enumerate(dates):
            prev_date = dates[i - 1] if i > 0 else None
            h = input_hash(curve_hashes[d], curve_hashes.get(prev_date), labels)
            if existing.get(d) == h:
                continue
            try:
                rows = compute_day(store, d, prev_date, labels)
            except Exception as e:  # 1日の失敗で全体を止めない
                failed[d] = str(e)
                continue
            store.save_swaption_forwards(d, rows, h)
            built += 1
            if built % 100 == 0:
                log(f"  原資産のFwd 計算中... {built}日分")
        removed = store.delete_swaption_forwards_except(dates)
    log(f"原資産のFwd: 新規/更新 {built}日 / 変更なし {len(dates) - built - len(failed)}日 / "
        f"失敗 {len(failed)}日 / 古い行を削除 {removed}日")
    for d, msg in sorted(failed.items()):
        log(f"  {d}: 原資産のFwdの失敗 {msg}")
    return failed
