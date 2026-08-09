"""
BOJスワップ由来の金利 vs スポット直接クォートの歪み（distortion）を計算。

■ 歪みの定義
  distortion(T) = BOJ_implied_rate(T) - spot_ois_rate(T)    [bps単位]

  正値 → BOJスワップがスポットOISより高い水準を示す（BOJ利上げ期待が高め）
  負値 → BOJスワップがスポットOISより低い水準を示す

■ 現状
  スポットOISの直接クォート（1M-11M TONA OIS）はLSEGから取得予定。
  本モジュールはダミーデータで動作し、実データ接続後にそのまま使用可能。
"""

import numpy as np
import pandas as pd
from datetime import date


TENOR_LABELS = {
    1: "1M", 2: "2M", 3: "3M", 4: "4M", 5: "5M", 6: "6M",
    7: "7M", 8: "8M", 9: "9M", 10: "10M", 11: "11M", 12: "1Y",
}


def generate_dummy_spot_ois(
    boj_implied_df: pd.DataFrame,
    noise_bps: float = 3.0,
    seed: int = 42,
) -> pd.DataFrame:
    """
    スポットOIS直接クォートのダミーデータ生成。
    BOJ impliedレートに±noise_bpsのランダムノイズを加えたもの。
    実データ接続後はこの関数を差し替える。
    """
    rng = np.random.default_rng(seed)
    df = boj_implied_df[["tenor_months", "settle_date", "zero_rate_pct"]].copy()
    noise = rng.uniform(-noise_bps / 100, noise_bps / 100, size=len(df))
    df["spot_ois_rate_pct"] = df["zero_rate_pct"] + noise
    df["tenor_label"] = df["tenor_months"].map(TENOR_LABELS)
    return df


def compute_distortion(
    boj_implied_df: pd.DataFrame,
    spot_ois_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    BOJスワップ由来のゼロレートとスポットOIS直接クォートの差分（歪み）を計算。

    Parameters
    ----------
    boj_implied_df : extract_tenor_rates()の出力 (zero_rate_pct列含む)
    spot_ois_df    : スポットOIS金利 (tenor_months, spot_ois_rate_pct列含む)

    Returns
    -------
    DataFrame: tenor別の歪み分析結果
    """
    merged = boj_implied_df.merge(
        spot_ois_df[["tenor_months", "spot_ois_rate_pct"]],
        on="tenor_months",
        how="left",
    )
    merged["tenor_label"] = merged["tenor_months"].map(TENOR_LABELS)

    # 歪み = BOJ implied - Spot OIS [bps]
    merged["distortion_bps"] = (
        merged["zero_rate_pct"] - merged["spot_ois_rate_pct"]
    ) * 100.0  # %→bps変換

    return merged


def summarize_distortion(distortion_df: pd.DataFrame) -> pd.DataFrame:
    """
    歪みの統計サマリーを返す。
    """
    cols = [
        "tenor_label", "settle_date", "days",
        "zero_rate_pct", "spot_ois_rate_pct", "distortion_bps",
    ]
    summary = distortion_df[cols].copy()
    summary.columns = [
        "Tenor", "Settle Date", "Days",
        "BOJ Implied Rate (%)", "Spot OIS Rate (%)", "Distortion (bps)",
    ]
    return summary
