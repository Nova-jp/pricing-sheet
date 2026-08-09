"""
JPY OIS(TONA)ディスカウントカーブのブートストラップモジュール。

■ 使用データ
  RealTimeシートのスポットテナー(O/N〜40Y)のみ。M1〜M8(BOJ会合デート物)
  は現時点では使用しない(ユーザー指示により将来フェーズで対応)。

■ 短期ゾーンの扱い
  - O/N: 評価日(T)からスポット日(T+2営業日)までを、O/Nレートを単純に
    延長して適用する近似でDF(spot)を算出する(T/Nレートがないための簡略化。
    要検証の前提)。
  - 1W: スポット日起点の単一期間(ブレット)として直接DFを算出(週単位)。
  - 1M〜11M: スポット日起点の単一期間(ブレット)として扱うが、内部的には
    1Y以上と同じPAスケジュール生成(端株1本のみ)に統一している。

■ 1M以上の扱い
  年1回払い(PA)のOISスワップとして、短い順にブートストラップする
  (1M〜11Mは実質1期間のみのブレット、schedule.pyのショート・バック
  スタブ機能によりPAのまま単一期間として扱われる)。
  市場クォートが飛んでいるテナー(例: 12Yの次が15Y)や、端株が生じる
  テナー(15M/18M/21M)では、中間・端数期間の支払日DFが未知のまま
  新pillarを解く必要があるため、その都度Monotone Convexカーブを
  仮組みしながらroot-finding(二分法)で解く。

■ 15M/18M/21Mの扱い(include_odd_tenors引数で選択可能)
  デフォルトは不使用(skipped_tenorsに計上)。含める場合、PA頻度での
  端株(例: 15M = 12M正規期間 + 3Mの端株)としてブートストラップする。
  端株の位置は「ショート・バックスタブ」(前方の正規期間を並べ、
  最後に端数を1本足す)を採用している。

■ 未対応(スコープ外)
  - M1〜M8: BOJ会合デート物、今回は使用しない
"""

from datetime import date, timedelta
from typing import Dict, List, NamedTuple, Tuple

from swap_pricing.calendars import add_business_days, modified_following
from swap_pricing.daycount import year_fraction
from swap_pricing.monotone_convex import MonotoneConvexCurve
from swap_pricing.schedule import generate_schedule, parse_tenor_months

CURVE_DCF = "act/365fixed"  # カーブ構築対象のOIS自体の市場コンベンション
CURVE_FREQ = "PA"
CURVE_ROLL = "STD"

BULLET_SUB_1Y = [f"{i}m" for i in range(1, 12)]  # 1m〜11m
ODD_TENORS = ["15m", "18m", "21m"]  # include_odd_tenors=Trueの場合のみ使用
ANNUAL_TENORS = [f"{i}y" for i in range(1, 13)] + ["15y", "20y", "25y", "30y", "35y", "40y"]
EXCLUDED_MEETING_TENORS = {f"m{i}" for i in range(1, 9)}


class BootstrapResult(NamedTuple):
    curve: MonotoneConvexCurve
    valuation_date: date
    spot_date: date
    used_tenors: List[str]
    skipped_tenors: List[str]


def _bisect(func, lo: float, hi: float, tol: float = 1e-13, max_iter: int = 100) -> float:
    f_lo = func(lo)
    f_hi = func(hi)
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if f_lo * f_hi > 0:
        raise ValueError("root-findingのブラケットが不正です(両端で符号が同じ)")
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = func(mid)
        if abs(f_mid) < tol or (hi - lo) / 2 < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return (lo + hi) / 2


def bootstrap_curve(
    valuation_date: date, spot_rates: Dict[str, float], include_odd_tenors: bool = False
) -> BootstrapResult:
    """
    spot_rates: {tenor_label: rate(パーセント表記)} 例 {'1Y': 1.135, '5Y': 1.62, ...}
    キーの大小文字・空白は問わない。

    include_odd_tenors: Trueの場合、15M/18M/21Mを端株ありでブートストラップに含める。
    """
    rates = {k.strip().lower(): v for k, v in spot_rates.items() if v is not None}

    spot_date = add_business_days(valuation_date, 2)

    pillar_dates: List[date] = [valuation_date]
    pillar_times: List[float] = [0.0]
    pillar_dfs: List[float] = [1.0]
    used_tenors: List[str] = []
    skipped_tenors: List[str] = [t for t in rates if t in EXCLUDED_MEETING_TENORS]
    if not include_odd_tenors:
        skipped_tenors += [t for t in rates if t in ODD_TENORS]

    def _add_pillar(d: date, df: float, tenor_label: str) -> None:
        t = year_fraction(valuation_date, d, "act/365fixed")
        pillar_dates.append(d)
        pillar_times.append(t)
        pillar_dfs.append(df)
        used_tenors.append(tenor_label)

    # --- O/N: 評価日→スポット日のDF ---
    # ('1d' はNeon等の一部データソースでのO/N相当ラベル)
    df_spot = 1.0
    on_key = next((k for k in ("o/n", "on", "1d") if k in rates), None)
    if on_key is not None:
        r_on = rates[on_key] / 100.0
        tau = year_fraction(valuation_date, spot_date, "act/365fixed")
        df_spot = 1.0 / (1.0 + r_on * tau)
        used_tenors.append(on_key)
    _add_pillar(spot_date, df_spot, "spot")

    # --- 1W/2W/3W: スポット起点の単一期間(ブレット、週単位のためgenerate_scheduleは使わない) ---
    for weeks, label in ((1, "1w"), (2, "2w"), (3, "3w")):
        if label not in rates:
            continue
        unadjusted = spot_date + timedelta(weeks=weeks)
        maturity = modified_following(unadjusted)
        dcf = year_fraction(spot_date, maturity, CURVE_DCF)
        df = df_spot / (1.0 + rates[label] / 100.0 * dcf)
        _add_pillar(maturity, df, label)

    # --- 1M以上: 年1回払いのOISスワップとして、月数の昇順に逐次ブートストラップ ---
    # (1M〜11Mは自動的に単一期間のブレットになる。15M/18M/21Mは端株ありで扱われる)
    candidate_tenors = list(BULLET_SUB_1Y) + list(ANNUAL_TENORS)
    if include_odd_tenors:
        candidate_tenors += ODD_TENORS
    ordered_tenors = sorted(
        (t for t in candidate_tenors if t in rates), key=parse_tenor_months
    )

    known_by_date: Dict[date, float] = dict(zip(pillar_dates, pillar_dfs))

    for label in ordered_tenors:
        rate = rates[label] / 100.0
        periods = generate_schedule(spot_date, CURVE_FREQ, CURVE_ROLL, CURVE_DCF, tenor=label)
        maturity = periods[-1].payment_date

        missing_dates = [
            p.payment_date
            for p in periods
            if p.payment_date != maturity and p.payment_date not in known_by_date
        ]

        def annuity_and_float_pv(df_new: float, periods=periods, maturity=maturity, missing_dates=missing_dates):
            if missing_dates:
                trial_times = list(pillar_times) + [
                    year_fraction(valuation_date, maturity, "act/365fixed")
                ]
                trial_dfs = list(pillar_dfs) + [df_new]
                order = sorted(range(len(trial_times)), key=lambda i: trial_times[i])
                trial_curve = MonotoneConvexCurve(
                    [trial_times[i] for i in order], [trial_dfs[i] for i in order]
                )
                interp = {
                    d: trial_curve.discount_factor(year_fraction(valuation_date, d, "act/365fixed"))
                    for d in missing_dates
                }
            else:
                interp = {}

            annuity = 0.0
            for p in periods:
                if p.payment_date == maturity:
                    df_p = df_new
                elif p.payment_date in known_by_date:
                    df_p = known_by_date[p.payment_date]
                else:
                    df_p = interp[p.payment_date]
                annuity += df_p * p.dcf
            float_pv = df_spot - df_new
            return annuity, float_pv

        def objective(df_new: float, _f=annuity_and_float_pv, _rate=rate):
            annuity, float_pv = _f(df_new)
            return float_pv - _rate * annuity

        df_solved = _bisect(objective, lo=1e-8, hi=2.0)

        known_by_date[maturity] = df_solved
        _add_pillar(maturity, df_solved, label)

    if len(pillar_times) < 2:
        raise ValueError("ブートストラップに使えるレートがありません")

    order = sorted(range(len(pillar_times)), key=lambda i: pillar_times[i])
    sorted_times = [pillar_times[i] for i in order]
    sorted_dfs = [pillar_dfs[i] for i in order]
    curve = MonotoneConvexCurve(sorted_times, sorted_dfs)

    return BootstrapResult(
        curve=curve,
        valuation_date=valuation_date,
        spot_date=spot_date,
        used_tenors=used_tenors,
        skipped_tenors=skipped_tenors,
    )
