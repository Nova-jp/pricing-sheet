"""
現物オプション(国債店頭オプション)の約定のグリークス(PositionSheetのBondOptシート)。

会社のファイルには入っていないので、約定(Raw_BondOptionに貼る)から基準日の引けの値で計算し、Positionシートの
Bondのデルタ・ガンマとセータに足す(ユーザー決定、2026-11-10)。ベガの格子はスワップション(会社の値)だけで、
現物オプションのベガは約定ごとの表示だけ(2026-11-10、ユーザー指示)。リアルタイムでは計算しない。
計算はBondOption.xlsmと同じ部品(engine/common/jgb_option/、式は docs/bond_option.md)。

■ 分担(BondOptionと同じ。中身のある計算は表UDF、符号・額面・単位の換算と合計はシートの数式)
  market_rows : 行ごとの [利率, 償還日, 引けの単利, 引けの日付, スポット単価, 先渡clean, 先渡MD, 先渡DD,
                スワップションのボラ(補間), Vol ratio, T, message]
  greeks_rows : 行ごとの [プレミアム, デルタ, ガンマ, ベガ, セータ, message](額面100円あたり、Black式)
  pillar_risk : 銘柄ごとの「デルタ換算の額面」「価格のガンマ」「セータ」→ ピラーごとの [デルタ, ガンマ, セータ]

■ 日付・市場データ(すべて基準日 = 会社のリスクの基準日の引け)
  取引日 t0 = 基準日、スポット受渡日 = t0のT+1、行使後の受渡日 se = 約定のEffectiveDate。
  スポット単価 = 基準日の引けの単利(DBのJCL)からF1(起点 = スポット受渡日)。先渡はレポ(Setupの1セル)でF6。
  ボラ = 基準日のDBのスワップションのvol(vol_quotes)から補間 × Vol ratio(窓はSetup、BondOptionと同じ計算)。

■ ピラーへの分け方(チェーンルール)
  オプションの価値 V は原資産の先渡価格 F の関数で、F は現物の単価 P とほぼ1対1(dF/dP = 1 + レポ×期間、
  1か月で1.0003程度なので1とみなす)。P の複利1bpあたりの変化 dP と2階の d2P(bonds.py と同じ修正
  デュレーション・コンベクシティ、基準日の引けの単利から)から
    デルタ = E × dP / 100、ガンマ = (G × dP² + E × d2P) / 100
  E = 符号 × 額面 × Blackのデルタ(デルタ換算の額面、百万円)、G = 符号 × 額面 × Blackのガンマ。
  どちらも現物と同じく、銘柄の満期が入る区間の長いほうのピラーに全部載せる。
"""

from datetime import date
from typing import Dict, List, NamedTuple, Optional, Sequence

from engine.common import bond, udf_common
from engine.common.jgb_option import hv, pricer, vol
from engine.common.market import CurveSet
from engine.position.bonds import BondPosition, bond_risk, bucket, pillar_dates

SPOT_LAG = 1
MARKET_COLUMNS = 12
GREEK_COLUMNS = 6


def _date(value, name: str) -> date:
    d = udf_common._to_date(value) if not udf_common._is_blank(value) else None
    if d is None:
        raise ValueError(f"{name}が空です")
    return d


def _number(value, name: str) -> float:
    if udf_common._is_blank(value):
        raise ValueError(f"{name}が空です")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}は数値です: {value!r}") from None


def _column(values) -> list:
    return list(values) if isinstance(values, (list, tuple)) else [values]


def _rows(*columns) -> List[tuple]:
    cols = [_column(c) for c in columns]
    n = max(len(c) for c in cols)
    return list(zip(*(c + [None] * (n - len(c)) for c in cols)))


def _code(value) -> str:
    return str(value).strip().upper()


# ---------------------------------------------------------------------------
# 市場(先渡・ボラ)
# ---------------------------------------------------------------------------

def market_rows(
    codes: Sequence, expiries: Sequence, settles: Sequence, base_date: date, repo: Optional[float], window,
    store, rates: hv.CurveRates, vol_rows: Sequence,
) -> List[list]:
    """行ごとの [利率, 償還日, 引けの単利, 引けの日付, スポット単価, 先渡clean, 先渡MD, 先渡DD, スワップションのボラ
    (bp/年), Vol ratio, T, message]。銘柄が空欄の行は空欄、行使日が基準日以前の行はmessageだけ。
    repo: レポ(%、Noneなら先渡の列はmessageだけ)。store: 開いたHistoricalStore。rates: Vol ratio用のスワップレート(fill はここで行う)。
    vol_rows: 基準日のスワップションのvolの [銘柄(1Mx10Y), vol] の行。"""
    blank = [""] * MARKET_COLUMNS
    grid_error = None
    try:
        grid = vol.build_grid(vol_rows, base_date)
        if not grid:
            raise ValueError(f"{base_date}のスワップションのvolがDBにありません")
    except ValueError as e:
        grid, grid_error = {}, str(e)
    n_window = None
    try:
        n_window = hv.parse_window(window)
    except ValueError as e:
        window_error = str(e)
    spot_settle = bond.settlement_date(base_date, SPOT_LAG)

    parsed: List[Optional[tuple]] = []
    for code, expiry, settle in _rows(codes, expiries, settles):
        if udf_common._is_blank(code):
            parsed.append(None)
            continue
        code = _code(code)
        try:
            te = _date(expiry, "ExerciseDate")
            if te <= base_date:
                parsed.append((code, None, f"Expired ({te})"))
                continue
            se = _date(settle, "EffectiveDate")
            info = store.jgb_bond(code)
            if info is None:
                raise ValueError(f"銘柄 {code} がDBにありません")
            close = store.jgb_close(code, base_date)
            if close is None:
                raise ValueError(f"{code}の{base_date}以前の引け値がDBにありません")
            parsed.append((code, (bond.Jgb(info.coupon, info.maturity), close, te, se), ""))
        except Exception as e:  # 行ごとにメッセージを出して他の行は続ける
            parsed.append((code, None, str(e)))

    valid = [(c, p[0]) for c, p, _ in filter(None, parsed) if p is not None]
    closes_by_code: Dict[str, Dict[date, float]] = {}
    if n_window is not None and valid:
        span_start = date.fromordinal(base_date.toordinal() - n_window * 2 - 20)
        for d, by_code in store.jgb_closes_between(span_start, base_date).items():
            for c, y in by_code.items():
                closes_by_code.setdefault(_code(c), {})[d] = y
        rates.fill(hv.needed(rates.curve_dates, closes_by_code, valid, base_date, n_window))

    out = []
    for item in parsed:
        if item is None:
            out.append(blank)
            continue
        code, p, message = item
        if p is None:
            out.append([""] * (MARKET_COLUMNS - 1) + [message])
            continue
        jgb, close, te, se = p
        errors: List[str] = []
        row = [jgb.coupon, jgb.maturity, close.simple_yield, close.as_of_date] + [""] * 7
        try:
            if repo is None:
                raise ValueError("レポが空です(SetupのRepo)")
            spot = bond.price_from_simple_yield(jgb, close.simple_yield, spot_settle)
            fwd = bond.forward_price(jgb, spot, spot_settle, se, repo)
            risk = pricer.forward_duration(jgb, fwd.dirty, se)
            row[4:8] = [spot, fwd.clean, risk.modified_duration, risk.dollar_duration]
        except Exception as e:
            errors.append(f"Fwd: {e}")
        if grid_error:
            errors.append(f"Vol: {grid_error}")
        else:
            try:
                row[8] = vol.grid_vol(grid, base_date, te, jgb.maturity)
            except Exception as e:
                errors.append(f"Vol: {e}")
        if n_window is None:
            errors.append(f"HV: {window_error}")
        else:
            try:
                row[9] = hv.vol_ratio(jgb, closes_by_code.get(code, {}), rates, base_date, n_window).ratio
            except Exception as e:
                errors.append(f"HV: {e}")
        try:
            row[10] = pricer.option_term(base_date, te)
        except Exception as e:
            errors.append(f"T: {e}")
        out.append(row + [" / ".join(errors)])
    return out


# ---------------------------------------------------------------------------
# Black式のグリークス
# ---------------------------------------------------------------------------

def greeks_rows(types: Sequence, forwards: Sequence, strikes: Sequence, price_vols: Sequence,
                terms: Sequence) -> List[list]:
    """行ごとの [プレミアム, デルタ, ガンマ, ベガ(価格ボラ1%あたり), セータ(1営業日), message](額面100円あたり)。
    種類(Call/Put)か先渡が空欄の行は空欄。それ以外の入力が空欄・エラーの行はmessageだけ。"""
    out = []
    for kind, forward, strike, price_vol, term in _rows(types, forwards, strikes, price_vols, terms):
        if udf_common._is_blank(kind) or udf_common._is_blank(forward):
            out.append([""] * GREEK_COLUMNS)  # 先渡が空欄 = 市場の表で計算しなかった行(理由はそちらのmessage)
            continue
        try:
            g = pricer.black_greeks(
                kind, _number(forward, "先渡価格"), _number(strike, "ストライク"),
                _number(price_vol, "価格ボラ"), _number(term, "期間"),
            )
            out.append(list(g) + [""])
        except Exception as e:
            out.append([""] * (GREEK_COLUMNS - 1) + [str(e)])
    return out


# ---------------------------------------------------------------------------
# ピラーへの分解(現物オプション・現物の両方に使う)
# ---------------------------------------------------------------------------

class PillarRisk(NamedTuple):
    deltas: Dict[str, float]  # ピラーのラベル(小文字) → 百万円/bp
    gammas: Dict[str, float]  # → 百万円/bp²
    thetas: Dict[str, float]  # → 百万円/営業日(原資産の銘柄のピラーに載せる)
    errors: List[str]  # エラーの銘柄のメッセージ(その銘柄は足さない)


def _float_or_zero(value) -> float:
    return 0.0 if udf_common._is_blank(value) else float(value)


def pillar_risk(curves: CurveSet, store, codes: Sequence, faces: Sequence, price_gammas: Sequence,
                thetas: Sequence = ()) -> PillarRisk:
    """銘柄ごとの E(デルタ換算の額面、百万円)・G(価格のガンマ × 額面、百万円/円)・セータ(百万円/日)を
    足し合わせ、ピラーのラベル → デルタ(百万円/bp)・ガンマ(百万円/bp²)・セータを返す。
    G・セータが空欄なら0(現物のポジション)。"""
    totals: Dict[str, List[float]] = {}
    for code, face, gamma, theta in _rows(codes, faces, price_gammas, thetas):
        if udf_common._is_blank(code) or udf_common._is_blank(face):
            continue
        try:
            e, g, t = float(face), _float_or_zero(gamma), _float_or_zero(theta)
        except (TypeError, ValueError):
            continue
        acc = totals.setdefault(_code(code), [0.0, 0.0, 0.0])
        acc[0] += e
        acc[1] += g
        acc[2] += t
    pillars = pillar_dates(curves.bump_labels, curves.spot_date)
    out = PillarRisk({}, {}, {}, [])
    for code, (e, g, t) in totals.items():
        try:
            r = bond_risk(store, BondPosition(code, 100.0), curves.valuation_date)  # 額面100円あたり
            label = bucket(r.maturity, pillars).lower()
        except Exception as ex:
            out.errors.append(f"{code}: {ex}")
            continue
        out.deltas[label] = out.deltas.get(label, 0.0) + e * r.dp / 100.0
        out.gammas[label] = out.gammas.get(label, 0.0) + (g * r.dp * r.dp + e * r.d2p) / 100.0
        out.thetas[label] = out.thetas.get(label, 0.0) + t
    return out
