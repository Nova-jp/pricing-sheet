"""
現物オプション(国債店頭オプション)の約定のグリークス(PositionSheetのBondOptシート)。

会社のファイルには入っていないので、約定(Raw_BondOptionに貼る)から基準日の引けの値で計算し、Positionシートの
Bondのデルタ・ガンマとベガの格子に足す(ユーザー決定、2026-11-10)。リアルタイムでは計算しない。
計算はBondOption.xlsmと同じ部品(engine/common/jgb_option/、式は docs/bond_option.md)。

■ 分担(BondOptionと同じ。中身のある計算は表UDF、符号・額面・単位の換算と合計はシートの数式)
  market_rows : 行ごとの [利率, 償還日, 引けの単利, 引けの日付, スポット単価, 先渡clean, 先渡MD, 先渡DD,
                スワップションのボラ(補間), Vol ratio, T, message]
  greeks_rows : 行ごとの [プレミアム, デルタ, ガンマ, ベガ, セータ, message](額面100円あたり、Black式)
  pillar_risk : 銘柄ごとの「デルタ換算の額面」「価格のガンマ」→ ピラーごとの [デルタ, ガンマ]
  vega_grid   : 約定ごとのベガ → スワップションの格子(満期×原資産)に補間の重みで分けたもの

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

■ ベガの分け方
  スワップションの格子の点を1つずつ少し動かしたときの補間値の変化(重み)をベガに掛けて足す。格子にあって
  Positionのベガの格子に無い原資産(6Y・8Y・9Y)は、前後の原資産に年数で線形に分ける。
"""

from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from engine.common import bond, udf_common
from engine.common.jgb_option import hv, pricer, vol
from engine.common.market import CurveSet
from engine.position.bonds import BondPosition, bond_risk, bucket, pillar_dates

SPOT_LAG = 1
MARKET_COLUMNS = 12
GREEK_COLUMNS = 6
_VEGA_EPS = 1e-4


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

def pillar_risk(curves: CurveSet, store, codes: Sequence, faces: Sequence,
                price_gammas: Sequence) -> Tuple[Dict[str, float], Dict[str, float], List[str]]:
    """銘柄ごとの E(デルタ換算の額面、百万円)・G(価格のガンマ × 額面、百万円/円)を足し合わせ、
    ピラーのラベル → デルタ(百万円/bp)・ガンマ(百万円/bp²)を返す。G が空欄なら0(現物のポジション)。
    3つ目はエラーの銘柄のメッセージ(その銘柄は足さない)。"""
    totals: Dict[str, List[float]] = {}
    for code, face, gamma in _rows(codes, faces, price_gammas):
        if udf_common._is_blank(code) or udf_common._is_blank(face):
            continue
        try:
            e = float(face)
            g = 0.0 if udf_common._is_blank(gamma) else float(gamma)
        except (TypeError, ValueError):
            continue
        acc = totals.setdefault(_code(code), [0.0, 0.0])
        acc[0] += e
        acc[1] += g
    pillars = pillar_dates(curves.bump_labels, curves.spot_date)
    deltas: Dict[str, float] = {}
    gammas: Dict[str, float] = {}
    errors: List[str] = []
    for code, (e, g) in totals.items():
        try:
            r = bond_risk(store, BondPosition(code, 100.0), curves.valuation_date)  # 額面100円あたり
            label = bucket(r.maturity, pillars).lower()
        except Exception as ex:
            errors.append(f"{code}: {ex}")
            continue
        deltas[label] = deltas.get(label, 0.0) + e * r.dp / 100.0
        gammas[label] = gammas.get(label, 0.0) + (g * r.dp * r.dp + e * r.d2p) / 100.0
    return deltas, gammas, errors


# ---------------------------------------------------------------------------
# ベガの格子
# ---------------------------------------------------------------------------

def _years(label: str) -> float:
    p = str(label).strip().upper()
    n = float(p[:-1])
    return n / 12 if p.endswith("M") else n


def _split(x: float, axis: Sequence[float]) -> List[Tuple[int, float]]:
    """x を軸の点に線形に分ける(範囲外は端の点に全部)。"""
    if x <= axis[0]:
        return [(0, 1.0)]
    if x >= axis[-1]:
        return [(len(axis) - 1, 1.0)]
    for i in range(1, len(axis)):
        if x <= axis[i]:
            if x == axis[i]:
                return [(i, 1.0)]
            w = (x - axis[i - 1]) / (axis[i] - axis[i - 1])
            return [(i - 1, 1.0 - w), (i, w)]
    return [(len(axis) - 1, 1.0)]


def vega_grid(vol_rows: Sequence, base_date: date, vegas: Sequence, expiries: Sequence, maturities: Sequence,
              expiry_labels: Sequence[str], tail_labels: Sequence[str]) -> List[List[float]]:
    """約定ごとのベガ(百万円/スワップションのvol 1bp)を、補間に使ったスワップションの格子の点に重みで分け、
    行=expiry_labels × 列=tail_labels の格子で返す。ベガが数値でない行(空欄・満期済み)は足さない。"""
    trades = []
    for v, te, m in _rows(vegas, expiries, maturities):
        if udf_common._is_blank(v) or udf_common._is_blank(te) or udf_common._is_blank(m):
            continue
        try:
            trades.append((float(v), _date(te, "ExerciseDate"), _date(m, "償還日")))
        except (TypeError, ValueError):
            continue
    e_axis = [_years(x) for x in expiry_labels]
    t_axis = [_years(x) for x in tail_labels]
    out = [[0.0] * len(t_axis) for _ in e_axis]
    if not trades:
        return out
    grid = vol.build_grid(vol_rows, base_date)
    if not grid:
        raise ValueError(f"{base_date}のスワップションのvolがDBにありません")
    # 格子の点(満期の営業日数, 原資産の年数) → 満期・原資産の年数(ラベルから)
    point_years: Dict[Tuple[float, float], Tuple[float, float]] = {}
    for row in vol_rows:
        if not row or udf_common._is_blank(row[0]) or vol._as_vol(row[-1]) is None:
            continue
        e, t = vol.parse_label(row[0])
        e_label, t_label = str(row[0]).upper().split("X")
        point_years[(vol._expiry_days(base_date, e), vol._tenor_years(t))] = (_years(e_label), _years(t_label))
    coords = [(vol.business_days(base_date, te), vol._years(te, m)) for _, te, m in trades]  # grid_vol と同じ軸
    base = [vol.interpolate(grid, x, y) for x, y in coords]
    for e_days, points in grid.items():
        for j, (tenor, value) in enumerate(points):
            points[j] = (tenor, value + _VEGA_EPS)
            bumped = [vol.interpolate(grid, x, y) for x, y in coords]
            points[j] = (tenor, value)
            ey, ty = point_years[(e_days, tenor)]
            amount = sum(v * (b - a) / _VEGA_EPS for (v, _, _), a, b in zip(trades, base, bumped))
            if amount == 0.0:
                continue
            for i, wi in _split(ey, e_axis):
                for k, wk in _split(ty, t_axis):
                    out[i][k] += amount * wi * wk
    return out

