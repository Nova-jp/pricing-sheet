"""
データ読み込みモジュール。

■ データソース
  data/realtime_data.xlsx  ─── LSEGのExcel Add-in（RTD関数）が Value列を自動更新。
                                Ticker列はあらかじめ設定済み。
                                Valueが空（NaN）の場合は計算不可として扱う。

■ realtime_data.xlsxの列構成
  Ticker  | Value
  --------|--------
  JPBOJ1ONI=TRDT  | 0.75625   ← BOJ1 フォワード金利(%)
  ...
  JPBOJ8ONI=TRDT  | 1.31000
  JPY1DOIS=ICAP   | 0.72875   ← TONA 1日物(%)
  JPY1YOIS=ICAP   | 1.13500   ← TONA 1年物(%)
  JPYOIS1M=ICAP   | 0.77500   ← スポットOIS 1M(%)  ← 歪み計算に使用
  ...
  JPYOIS11M=ICAP  | 1.08500

  ※ 将来追加予定（ASW用）:
  JPYIRS1Y=ICAP  etc.  スポットスワップ全年限
"""

import math
import pandas as pd
from datetime import date
from pathlib import Path


# ---- ティッカー定義 ----
BOJ_TICKERS = [f"JPBOJ{i}ONI=TRDT" for i in range(1, 9)]
TONA_1D_TICKER = "JPY1DOIS=ICAP"
TONA_1Y_TICKER = "JPY1YOIS=ICAP"

# スポットOIS直接クォート（歪み計算用）
SPOT_OIS_TENORS = {
    1:  "JPYOIS1M=ICAP",
    2:  "JPYOIS2M=ICAP",
    3:  "JPYOIS3M=ICAP",
    4:  "JPYOIS4M=ICAP",
    5:  "JPYOIS5M=ICAP",
    6:  "JPYOIS6M=ICAP",
    7:  "JPYOIS7M=ICAP",
    8:  "JPYOIS8M=ICAP",
    9:  "JPYOIS9M=ICAP",
    10: "JPYOIS10M=ICAP",
    11: "JPYOIS11M=ICAP",
    12: "JPY1YOIS=ICAP",    # 1Y はTONA 1Yと同じ
}


def _is_valid(v) -> bool:
    """None・NaN・空文字でなく数値として有効かチェック。"""
    if v is None:
        return False
    try:
        return not math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def load_realtime_data(excel_path: str) -> dict:
    """
    realtime_data.xlsxを読み込み、全ティッカーの値を辞書で返す。

    Returns
    -------
    dict:
      'boj_rates'      : list[float|None]  BOJ1-8フォワード金利(%) (無効値はNone)
      'tona_1d'        : float|None        TONA 1日物(%)
      'tona_1y'        : float|None        TONA 1年物(%)
      'spot_ois_rates' : dict              {tenor_months: rate(%)} 有効値のみ
      'raw'            : dict              {ticker: value} 全データ（将来のASW等に使用）
      'is_valid'       : bool              BOJ1-8が全て有効か
    """
    path = Path(excel_path)
    if not path.exists():
        raise FileNotFoundError(f"realtime_data.xlsx が見つかりません: {excel_path}")

    df = pd.read_excel(excel_path, header=0)

    # Ticker / Value 列を特定（列名の揺れに対応）
    cols = [c.strip().lower() for c in df.columns]
    ticker_col_idx = next((i for i, c in enumerate(cols) if c in ("ticker", "name", "item", "symbol")), None)
    value_col_idx  = next((i for i, c in enumerate(cols) if c in ("value", "val", "mid", "price", "rate")), None)

    if ticker_col_idx is None or value_col_idx is None:
        raise ValueError(
            f"realtime_data.xlsx に 'Ticker' / 'Value' 列が見つかりません。"
            f"列名: {list(df.columns)}"
        )

    ticker_col = df.columns[ticker_col_idx]
    value_col  = df.columns[value_col_idx]

    # ティッカー→値の辞書
    raw = {}
    for _, row in df.iterrows():
        t = str(row[ticker_col]).strip() if row[ticker_col] is not None else ""
        if t and t != "nan":
            try:
                raw[t] = float(row[value_col])
            except (TypeError, ValueError):
                raw[t] = None

    # BOJ1-8
    boj_rates = [raw.get(t) for t in BOJ_TICKERS]
    boj_valid = all(_is_valid(r) for r in boj_rates)

    # TONA
    tona_1d = raw.get(TONA_1D_TICKER)
    tona_1y = raw.get(TONA_1Y_TICKER)

    # スポットOIS（有効値のみ）
    spot_ois = {
        months: float(raw[ticker])
        for months, ticker in SPOT_OIS_TENORS.items()
        if ticker in raw and _is_valid(raw.get(ticker))
    }

    return {
        "boj_rates":      boj_rates,
        "tona_1d":        tona_1d if _is_valid(tona_1d) else None,
        "tona_1y":        tona_1y if _is_valid(tona_1y) else None,
        "spot_ois_rates": spot_ois,
        "raw":            raw,
        "is_valid":       boj_valid,
    }


def load_meeting_schedule(schedule_path: str, from_date: date) -> list:
    """BOJ会合スケジュールCSVを読み込み、from_date以降の会合日を返す。"""
    df = pd.read_csv(schedule_path, parse_dates=["Date"])
    return sorted(d.date() for d in df["Date"] if d.date() >= from_date)


def create_realtime_template(output_path: str):
    """
    realtime_data.xlsxのテンプレートを生成。
    LSEGのRTD/Add-in関数でValueを埋める想定。
    """
    tickers = (
        BOJ_TICKERS
        + [TONA_1D_TICKER, TONA_1Y_TICKER]
        + list(SPOT_OIS_TENORS.values())
    )
    # 重複除去（JPY1YOIS=ICAPがSPOT_OIS_TENORSにも含まれるため）
    seen = set()
    unique_tickers = [t for t in tickers if not (t in seen or seen.add(t))]

    df = pd.DataFrame({"Ticker": unique_tickers, "Value": [None] * len(unique_tickers)})
    df.to_excel(output_path, index=False, sheet_name="RealTime")
    print(f"Template created: {output_path}")
