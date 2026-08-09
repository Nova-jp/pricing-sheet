"""
BOJスワップからOIS割引曲線をブートストラップし、
標準テナー（1M-11M, 1Y）のゼロレートを算出するモジュール。

■ JPBOJxONI の慣行
  JPBOJ1ONI : スポット日 → BOJ会合1の実効日 までの期間OIS金利（フォワード）
  JPBOJ2ONI : BOJ会合1実効日 → BOJ会合2実効日 までの期間OIS金利（フォワード）
  ...
  JPBOJnONI : BOJ会合n-1実効日 → BOJ会合n実効日 までの期間OIS金利（フォワード）

■ デイカウント : Act/365 Fixed
■ 割引係数の計算 (単利, 各ピリオド独立)
  DF(end) = DF(start) / (1 + forward_rate/100 * dcf)
  ※ ピリオドが短い(<1年)場合は単利が市場慣行

■ 補間 : ログ線形（フラットフォワードと等価）

■ ゼロレート変換
  zero_rate = (1/DF(T) - 1) / dcf  [小数表示 → %換算は呼び出し側]
"""

import numpy as np
import pandas as pd
from datetime import date
from typing import Optional

from .calendar_jpy import (
    spot_date, meeting_effective_date, tenor_date, act365
)


def load_meeting_schedule(schedule_path: str, valuation_date: date) -> list[date]:
    """
    BOJ会合スケジュールCSVを読み込み、バリュエーション日以降の
    会合日（決定日）をソートして返す。
    """
    df = pd.read_csv(schedule_path, parse_dates=["Date"])
    meetings = sorted(
        d.date() for d in df["Date"] if d.date() >= valuation_date
    )
    return meetings


def bootstrap_ois_curve(
    valuation_date: date,
    boj_rates: list[float],          # JPBOJ1-8 の金利（%単位）、フォワード金利
    tona_1y_rate: float,             # 1Y TONA OIS金利（%単位）
    meeting_dates: list,              # 今後8回のBOJ会合決定日（昇順）
) -> dict:
    """
    BOJスワップ（8本）と1Y TONAからOIS割引曲線をブートストラップ。

    Returns
    -------
    dict with keys:
      'pillars'  : list of (date, discount_factor) tuples (スポット日含む)
      'dates'    : list[date]
      'dfs'      : list[float]
    """
    spot = spot_date(valuation_date)
    effective_dates = [meeting_effective_date(m) for m in meeting_dates]

    # --- ピラー日付の構築 ---
    pillar_dates = [spot]
    pillar_dfs = [1.0]

    # BOJ1-8: 各ピリオドのフォワード金利から割引係数を累積
    prev_date = spot
    prev_df = 1.0

    for i, (eff_date, rate_pct) in enumerate(zip(effective_dates, boj_rates)):
        if eff_date <= prev_date:
            # 会合が既に過去 or スポット以前 → スキップ
            continue
        dcf = act365(prev_date, eff_date)
        # 単利: DF_end = DF_start / (1 + r * dcf)
        df_new = prev_df / (1.0 + rate_pct / 100.0 * dcf)
        pillar_dates.append(eff_date)
        pillar_dfs.append(df_new)
        prev_date = eff_date
        prev_df = df_new

    # 1Y TONAをアンカーとして追加
    tona_1y_end = tenor_date(spot, 12)
    dcf_1y = act365(spot, tona_1y_end)
    df_1y = 1.0 / (1.0 + tona_1y_rate / 100.0 * dcf_1y)

    # 既存ピラーと重複しない場合のみ追加
    if tona_1y_end not in pillar_dates:
        pillar_dates.append(tona_1y_end)
        pillar_dfs.append(df_1y)

    # 日付順にソート
    sorted_pairs = sorted(zip(pillar_dates, pillar_dfs))
    pillar_dates = [p[0] for p in sorted_pairs]
    pillar_dfs = [p[1] for p in sorted_pairs]

    return {
        "pillars": list(zip(pillar_dates, pillar_dfs)),
        "dates": pillar_dates,
        "dfs": pillar_dfs,
        "spot": spot,
    }


def interpolate_df(
    target_date: date,
    pillar_dates: list[date],
    pillar_dfs: list[float],
    ref_date: date,
) -> float:
    """
    ログ線形補間でtarget_dateの割引係数を求める。
    補間軸はref_date（=スポット日）からの経過日数。
    """
    t = (target_date - ref_date).days
    ts = [(d - ref_date).days for d in pillar_dates]
    log_dfs = [np.log(df) for df in pillar_dfs]

    # 外挿は端点フラット（フォワード一定）
    if t <= ts[0]:
        return pillar_dfs[0]
    if t >= ts[-1]:
        # フラットフォワード外挿
        log_df = np.interp(t, ts, log_dfs)
        return float(np.exp(log_df))

    return float(np.exp(np.interp(t, ts, log_dfs)))


def extract_tenor_rates(
    curve: dict,
    tenors_months: list[int],
) -> pd.DataFrame:
    """
    ブートストラップ曲線から各標準テナーのゼロレートを算出。

    Parameters
    ----------
    curve       : bootstrap_ois_curve()の戻り値
    tenors_months : 算出するテナー（月数）のリスト、例 [1,2,...,11]

    Returns
    -------
    DataFrame: columns = ['tenor_months', 'settle_date', 'days', 'discount_factor', 'zero_rate_pct']
    """
    spot = curve["spot"]
    rows = []
    for m in tenors_months:
        settle = tenor_date(spot, m)
        df_val = interpolate_df(settle, curve["dates"], curve["dfs"], spot)
        dcf = act365(spot, settle)
        if dcf > 0:
            zero_rate = (1.0 / df_val - 1.0) / dcf * 100.0  # %単位
        else:
            zero_rate = 0.0
        rows.append({
            "tenor_months": m,
            "settle_date": settle,
            "days": (settle - spot).days,
            "discount_factor": df_val,
            "zero_rate_pct": zero_rate,
        })
    return pd.DataFrame(rows)


def compute_implied_forward_rates(tenor_df: pd.DataFrame) -> pd.DataFrame:
    """
    標準テナーのゼロレートから隣接テナー間のインプライドフォワードを計算。
    """
    df = tenor_df.copy()
    fwd_rates = [None]
    for i in range(1, len(df)):
        df_prev = df.iloc[i - 1]["discount_factor"]
        df_curr = df.iloc[i]["discount_factor"]
        dcf = act365(df.iloc[i - 1]["settle_date"], df.iloc[i]["settle_date"])
        if dcf > 0:
            fwd = (df_prev / df_curr - 1.0) / dcf * 100.0
        else:
            fwd = None
        fwd_rates.append(fwd)
    df["implied_forward_pct"] = fwd_rates
    return df


def get_boj_implied_forward_rates(
    valuation_date: date,
    boj_rates: list[float],
    tona_1y_rate: float,
    meeting_dates: list[date],
) -> dict:
    """
    メインのエントリポイント。
    BOJスワップ + 1Y TONAから1M-11Mのゼロレートとフォワードを返す。

    Returns
    -------
    dict:
      'curve'     : bootstrap_ois_curve()の生データ
      'tenors'    : 1M-11M, 12MのテナーレートDataFrame
      'spot_date' : スポット日
      'pillar_summary': ピラー日付・DFのDataFrame
    """
    curve = bootstrap_ois_curve(valuation_date, boj_rates, tona_1y_rate, meeting_dates)
    tenors = extract_tenor_rates(curve, list(range(1, 13)))  # 1M-12M
    tenors = compute_implied_forward_rates(tenors)

    pillar_df = pd.DataFrame({
        "date": curve["dates"],
        "discount_factor": curve["dfs"],
    })
    pillar_df["zero_rate_pct"] = pillar_df.apply(
        lambda row: (
            (1.0 / row["discount_factor"] - 1.0) / act365(curve["spot"], row["date"]) * 100.0
            if act365(curve["spot"], row["date"]) > 0 else 0.0
        ),
        axis=1,
    )
    pillar_df["label"] = (
        ["Spot"] +
        [f"BOJ{i+1}" for i in range(len(meeting_dates))] +
        (["1Y TONA"] if len(curve["dates"]) > len(meeting_dates) + 1 else [])
    )[:len(curve["dates"])]

    return {
        "curve": curve,
        "tenors": tenors,
        "spot_date": curve["spot"],
        "pillar_summary": pillar_df,
    }
