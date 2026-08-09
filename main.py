"""
BOJ Swap Distortion Analysis - メインエントリポイント

使い方:
  python3 main.py                           # 今日の日付で実行
  python3 main.py --date 2026-02-24         # 指定日付（会合スケジュールに使用）
  python3 main.py --tona1y 1.05             # 1Y TONAレートを手動上書き
  python3 main.py --output path/to/out.xlsx # 出力先指定
  python3 main.py --template               # realtime_data.xlsxテンプレートを再生成

データソース:
  data/realtime_data.xlsx  ← LSEGが Value列を自動更新
  data/BOJ_meeting_schedule.csv ← BOJ会合日程（手動管理）
"""
import argparse
from datetime import date, datetime
from pathlib import Path

from src.data_loader import load_realtime_data, load_meeting_schedule, create_realtime_template
from src.boj_curve import get_boj_implied_forward_rates
from src.calendar_jpy import meeting_effective_date
from src.distortion import generate_dummy_spot_ois, compute_distortion, summarize_distortion
from src.output_excel import write_single_sheet

DATA_DIR      = Path("data")
OUTPUT_DIR    = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

REALTIME_PATH = DATA_DIR / "realtime_data.xlsx"
SCHEDULE_PATH = DATA_DIR / "BOJ_meeting_schedule.csv"


def run(valuation_date=None, tona_1y_override=None, output_path=None):
    if valuation_date is None:
        valuation_date = date.today()

    print(f"\n{'='*55}")
    print(f"  BOJ Swap Distortion Analysis")
    print(f"  Valuation Date : {valuation_date}")
    print(f"{'='*55}")

    # ----------------------------------------------------------------
    # データ読み込み
    # ----------------------------------------------------------------
    rt = load_realtime_data(str(REALTIME_PATH))

    if not rt["is_valid"]:
        raise ValueError(
            "realtime_data.xlsx の BOJ1-8 金利に無効値（空・NaN）があります。\n"
            "LSEGのRTDデータが更新されているか確認してください。"
        )

    boj_rates = [float(r) for r in rt["boj_rates"]]
    tona_1d   = rt["tona_1d"] or 0.0
    tona_1y   = rt["tona_1y"]

    # スポットOIS（歪み計算用）: 有効なテナーのみ
    spot_ois_from_rt = rt["spot_ois_rates"]
    is_dummy_spot    = len(spot_ois_from_rt) < 6   # 有効テナーが少ない場合はダミー

    print(f"  Source         : realtime_data.xlsx")
    print(f"  BOJ Rates      : {[round(r, 4) for r in boj_rates]}")
    print(f"  TONA 1D        : {tona_1d:.5f}%")

    # 1Y TONA の決定
    if tona_1y_override is not None:
        tona_1y = tona_1y_override
        print(f"  1Y TONA        : {tona_1y:.5f}% (manual override)")
    elif tona_1y is not None:
        print(f"  1Y TONA        : {tona_1y:.5f}% (from LSEG)")
    else:
        # フォールバック: BOJ5-6の平均（実データ取得後は不要）
        tona_1y = (boj_rates[4] + boj_rates[5]) / 2
        print(f"  1Y TONA        : {tona_1y:.5f}% [ESTIMATED – replace with JPY1YOIS=ICAP]")

    if is_dummy_spot:
        print(f"  Spot OIS       : *** DUMMY DATA *** (有効テナー数: {len(spot_ois_from_rt)})")
    else:
        print(f"  Spot OIS       : {len(spot_ois_from_rt)} tenors from LSEG")

    # ----------------------------------------------------------------
    # BOJ会合スケジュール
    # ----------------------------------------------------------------
    meetings  = load_meeting_schedule(str(SCHEDULE_PATH), valuation_date)[:8]
    effdates  = [meeting_effective_date(m) for m in meetings]

    print(f"\n  Meetings (next 8):")
    for i, (m, e) in enumerate(zip(meetings, effdates)):
        print(f"    BOJ{i+1}: {m} → effective {e}")

    # ----------------------------------------------------------------
    # ブートストラップ
    # ----------------------------------------------------------------
    result = get_boj_implied_forward_rates(
        valuation_date=valuation_date,
        boj_rates=boj_rates,
        tona_1y_rate=tona_1y,
        meeting_dates=meetings,
    )
    tenor_table    = result["tenors"]
    pillar_summary = result["pillar_summary"]
    spot           = result["spot_date"]

    # ----------------------------------------------------------------
    # スポットOISの確定（実データ or ダミー）
    # ----------------------------------------------------------------
    if not is_dummy_spot:
        spot_ois_df = tenor_table[["tenor_months", "settle_date"]].copy()
        spot_ois_df["spot_ois_rate_pct"] = spot_ois_df["tenor_months"].map(spot_ois_from_rt)
        spot_ois_df["tenor_label"] = spot_ois_df["tenor_months"].map(
            {m: f"{m}M" if m < 12 else "1Y" for m in range(1, 13)}
        )
    else:
        spot_ois_df = generate_dummy_spot_ois(tenor_table, noise_bps=3.0)

    distortion_df      = compute_distortion(tenor_table, spot_ois_df)
    distortion_summary = summarize_distortion(distortion_df)

    # ----------------------------------------------------------------
    # 結果表示
    # ----------------------------------------------------------------
    print(f"\n  Spot Date: {spot}")
    print(f"\n  {'Tenor':<5}  {'Settle':>12}  {'Days':>4}  "
          f"{'BOJ Implied':>12}  {'Spot OIS':>10}  {'Dist(bps)':>10}")
    print(f"  {'-'*62}")
    for _, row in distortion_summary.iterrows():
        dist = row["Distortion (bps)"]
        sign = "+" if dist >= 0 else ""
        print(
            f"  {row['Tenor']:<5}  {str(row['Settle Date']):>12}  "
            f"{row['Days']:>4}  {row['BOJ Implied Rate (%)']:>12.5f}  "
            f"{row['Spot OIS Rate (%)']:>10.5f}  {sign}{dist:>8.2f}"
        )

    # ----------------------------------------------------------------
    # Excel出力（固定名 + 日付別）
    # ----------------------------------------------------------------
    kwargs = dict(
        valuation_date=valuation_date,
        boj_rates=boj_rates,
        meeting_dates=meetings,
        effective_dates=effdates,
        pillar_summary=pillar_summary,
        tenor_table=tenor_table,
        distortion_summary=distortion_summary,
        tona_1y_rate=tona_1y,
        is_dummy_spot=is_dummy_spot,
    )
    # VBAから常に同じパスで参照できる固定ファイル
    write_single_sheet(output_path=str(OUTPUT_DIR / "boj_latest.xlsx"), **kwargs)

    # 指定があれば追加出力、なければ日付別アーカイブ
    extra = output_path or str(OUTPUT_DIR / f"boj_distortion_{valuation_date}.xlsx")
    if extra != str(OUTPUT_DIR / "boj_latest.xlsx"):
        write_single_sheet(output_path=extra, **kwargs)

    print(f"\n{'='*55}")


def main():
    parser = argparse.ArgumentParser(description="BOJ Swap Distortion Analysis")
    parser.add_argument("--date",     type=str,   default=None,
                        help="Valuation date YYYY-MM-DD (default: today)")
    parser.add_argument("--tona1y",   type=float, default=None,
                        help="1Y TONA OIS rate in %% (overrides realtime_data)")
    parser.add_argument("--output",   type=str,   default=None,
                        help="Output Excel file path")
    parser.add_argument("--template", action="store_true",
                        help="Regenerate realtime_data.xlsx template and exit")
    args = parser.parse_args()

    if args.template:
        create_realtime_template(str(REALTIME_PATH))
        return

    val_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date else date.today()
    )
    run(valuation_date=val_date, tona_1y_override=args.tona1y, output_path=args.output)


if __name__ == "__main__":
    main()
