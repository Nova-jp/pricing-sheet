"""
pricingシートを実際に計算するパイプライン。xlwingsのRunPythonから呼ばれる想定。

処理の流れ:
  1. RealTimeシートのTicker/Value(O/N〜40Y、M1〜M8含む)を読む
  2. 評価日(today)でDFカーブをブートストラップ(M1〜M8は自動的に除外される)
  3. pricingシートの各行(flag==1)についてコンベンションをパースし、
     price_swap() でtarget fixrate/PVを、risk.pyでdelta類を計算する
  4. adj_notional / target fixrate / PV / delta(bump) / delta(annuity) を書き戻す

■ fix rateが空欄の行の扱い(重要)
  空欄の場合、その行の基準パーレートを一度だけ計算し、その数値で
  fix_rateを固定してからPV・delta計算に使う(risk.pyのdocstring参照)。
  これにより「今のマーケットレートで組んだ場合のリスク量」が
  意味のある値として得られる。

■ start/tenor/endの扱い
  start・endは常に具体的な日付。tenorは 'nm'/'ny'(nは数値)のみ。
  endが空欄ならtenorからstart起点で満期を計算し、その計算結果を
  end列に書き戻す(ユーザーが満期日を確認できるようにするため)。
  endに具体的な日付が入っている場合は、tenorを無視してその日付を
  満期として使う。

■ delta(D列・T列・U列)の単位
  百万円単位。D列(目標delta)に値がある行のみadj_notionalを計算し、
  ない場合はE列に一切書き込まない(既存の値をそのまま残す)。

■ risk_flag
  risk_flag=1の行は、RealTimeシートにレートがある各テナー(グリッド)を
  個別に+1bpバンプしたバケットデルタを計算し、risk_flag=1の全行合計を
  pricingシートの右側にグリッド(テナー別)として表示する(グラフ描画はなし)。

■ Historicalシート(アウトライト・カーブ・フライ)
  Historicalシート上部(A1:F11)に10枠の管理表を置く:
    slot / type(outright,curve,fly) / row1 / row2 / row3 / label
  row1〜row3はpricingシートの行番号を指す。
    - outright: row1のコンベンションのパーレート推移
    - curve:    row2のパーレート - row1のパーレート
    - fly:      2*row2のパーレート - row1のパーレート - row3のパーレート
  (row2は「真ん中」、row1/row3が両翼)
  hist_flag列は現在この管理表に置き換えられ未使用(将来削除候補)。
"""

import datetime
from datetime import date, timedelta
from typing import Dict, Optional

import xlwings as xw

from swap_pricing.curve import bootstrap_curve
from swap_pricing.feed_reader import read_realtime_values
from swap_pricing.historical_pricer import (
    Convention,
    historical_curve_series,
    historical_fly_series,
    historical_par_rate_series,
)
from swap_pricing.risk import (
    SwapParams,
    annuity_delta,
    bucketed_delta,
    parallel_bump_delta,
    solve_notional_for_target_delta,
)
from swap_pricing.swap_pricer import price_swap

PRICING_SHEET_NAME = "pricing"
HISTORICAL_SHEET_NAME = "Historical"
HIST_BLOCK_WIDTH = 3  # Date列, Value列, 空白列
HIST_CONTROL_ROWS = 11  # ヘッダー1行 + 10枠
HIST_DATA_START_ROW = 14  # 管理表の下に余白を空けてデータ/チャートを配置
RISK_GRID_GAP_COLS = 3  # 既存の最終列からグリッドまでの空白列数

EXCEL_EPOCH = date(1899, 12, 30)


def _to_date(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, (int, float)):
        return EXCEL_EPOCH + timedelta(days=int(value))
    raise ValueError(f"日付として解釈できません: {value!r}")


def _header_index(header_row) -> dict:
    return {str(h).strip(): i for i, h in enumerate(header_row) if h is not None}


def _read_include_odd_tenors(book: xw.Book) -> bool:
    """RealTimeシートの 'include_15_18_21m' ラベル横のセルを読む(未設定ならFalse)。"""
    sheet = book.sheets["RealTime"]
    rows = sheet.used_range.value
    if rows is None:
        return False
    if not isinstance(rows[0], (list, tuple)):
        rows = [rows]
    for row in rows:
        if row and str(row[0]).strip() == "include_15_18_21m":
            return bool(row[1])
    return False


def refresh_pricing_sheet(book: xw.Book, valuation_date: Optional[date] = None) -> None:
    valuation_date = valuation_date or date.today()

    spot_rates = read_realtime_values(book)
    include_odd_tenors = _read_include_odd_tenors(book)

    boot = bootstrap_curve(valuation_date, spot_rates, include_odd_tenors=include_odd_tenors)

    sheet = book.sheets[PRICING_SHEET_NAME]
    used = sheet.used_range
    rows = used.value
    if rows is None or not isinstance(rows[0], (list, tuple)):
        return

    header = rows[0]
    col = _header_index(header)

    required = [
        "flag", "risk_flag", "remarks", "notional", "delta", "adj_notional",
        "start", "tenor", "end", "fix rate", "fix freq", "fix dcf", "index", "float freq", "float dcf",
        "roll conv", "target fixrate", "pay/rec", "PV", "delta(annuity)",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        raise ValueError(f"pricingシートに必要な列がありません: {missing}")

    # 'delta' はD列(目標)とT列(バンプ結果)で同名 -> ヘッダー出現順で解決
    delta_cols = [i for i, h in enumerate(header) if str(h).strip() == "delta"]
    target_delta_col, bump_delta_col = delta_cols[0], delta_cols[1]

    tenor_grid_order = [k for k in spot_rates if spot_rates[k] is not None]  # RealTime記載順
    risk_grid_total = {k: 0.0 for k in tenor_grid_order}

    # Historicalシートの管理表(row1〜row3参照)から任意の行のコンベンションを
    # 引けるよう、flagに関わらず全行を軽くパースしておく(妥当な行のみ格納)
    row_conventions: Dict[int, Convention] = {}
    for row_idx in range(1, len(rows)):
        row = rows[row_idx]
        fields = (
            row[col["fix freq"]], row[col["fix dcf"]], row[col["float freq"]],
            row[col["float dcf"]], row[col["roll conv"]], row[col["tenor"]],
        )
        if all(f not in (None, "") for f in fields):
            row_conventions[row_idx + 1] = Convention(*fields)

    for row_idx in range(1, len(rows)):
        row = rows[row_idx]
        excel_row = row_idx + 1  # 1-indexed, ヘッダーがrow1

        flag = row[col["flag"]]
        if flag != 1:
            continue

        try:
            notional = float(row[col["notional"]])
            target_delta = row[target_delta_col]
            start = _to_date(row[col["start"]])
            tenor = row[col["tenor"]]
            end = _to_date(row[col["end"]])
            fix_rate_input = row[col["fix rate"]]
            fix_freq = row[col["fix freq"]]
            fix_dcf = row[col["fix dcf"]]
            index_name = str(row[col["index"]]).strip().upper() if row[col["index"]] else None
            float_freq = row[col["float freq"]]
            float_dcf = row[col["float dcf"]]
            roll_conv = row[col["roll conv"]]
            pay_rec = str(row[col["pay/rec"]]).strip().upper() if row[col["pay/rec"]] else None

            if index_name != "JSCC":
                raise ValueError(f"index={index_name!r} は未対応です(現在はJSCCのみ)")
            if pay_rec not in ("PAY", "REC"):
                raise ValueError(f"pay/rec列に PAY か REC を入力してください: {pay_rec!r}")

            tenor_arg = None if end else tenor
            end_arg = end

            r_base = price_swap(
                boot.curve, valuation_date, start, fix_freq, fix_dcf, float_freq, float_dcf,
                roll_conv, notional, pay_rec, tenor=tenor_arg, end=end_arg, fix_rate=fix_rate_input,
            )

            params_frozen = SwapParams(
                start=start, fix_freq=fix_freq, fix_dcf=fix_dcf, float_freq=float_freq,
                float_dcf=float_dcf, roll_conv=roll_conv, notional=notional, pay_rec=pay_rec,
                tenor=tenor_arg, end=end_arg, fix_rate=r_base.fix_rate_used,
            )
            bump_delta_yen = parallel_bump_delta(spot_rates, valuation_date, params_frozen)
            ann_delta_yen = annuity_delta(r_base.annuity, notional, pay_rec)

            # delta列は百万円単位で表示する
            bump_delta_mm = bump_delta_yen / 1_000_000.0
            ann_delta_mm = ann_delta_yen / 1_000_000.0

            risk_flag = row[col["risk_flag"]]
            if risk_flag == 1:
                row_bucketed = bucketed_delta(spot_rates, valuation_date, params_frozen)
                for k, v in row_bucketed.items():
                    risk_grid_total.setdefault(k, 0.0)
                    risk_grid_total[k] += v / 1_000_000.0

            if target_delta not in (None, ""):
                # D列も百万円単位の入力として扱う -> 逆算時は円に変換
                target_delta_yen = float(target_delta) * 1_000_000.0
                adj_notional = solve_notional_for_target_delta(target_delta_yen, r_base.annuity, pay_rec)
                sheet.range((excel_row, col["adj_notional"] + 1)).value = adj_notional
            # target_deltaが空欄の場合はadj_notional列に一切書き込まない(既存値を保持)

            if end is None:
                sheet.range((excel_row, col["end"] + 1)).value = r_base.maturity_date

            sheet.range((excel_row, col["target fixrate"] + 1)).value = r_base.target_fixrate
            sheet.range((excel_row, col["PV"] + 1)).value = r_base.pv
            sheet.range((excel_row, bump_delta_col + 1)).value = bump_delta_mm
            sheet.range((excel_row, col["delta(annuity)"] + 1)).value = ann_delta_mm

        except Exception as exc:  # 1行のエラーで全体を止めない
            sheet.range((excel_row, col["target fixrate"] + 1)).value = f"#ERROR: {exc}"

    _write_risk_grid(sheet, len(header), tenor_grid_order, risk_grid_total)
    _write_historical_sheet(book, row_conventions)


def _write_risk_grid(sheet, n_header_cols: int, tenor_order: list, risk_grid_total: dict) -> None:
    """risk_flag=1の全行のバケットデルタ合計を、既存表の右側にテナー別グリッドで表示する(単位: 百万円)。"""
    base_col = n_header_cols + RISK_GRID_GAP_COLS + 1  # 1-indexed

    sheet.range((1, base_col), (3, base_col + 100)).clear_contents()

    if not tenor_order:
        return

    sheet.range((1, base_col)).value = "risk_grid_total(mm, bump, risk_flag=1行の合計)"
    sheet.range((2, base_col)).value = [tenor_order]
    sheet.range((3, base_col)).value = [[risk_grid_total.get(t, 0.0) for t in tenor_order]]


def _read_historical_control_table(hist_sheet: xw.Sheet) -> list:
    """Historicalシート上部(A1:F11)の10枠管理表を読む。[(slot, type, row1, row2, row3, label), ...]"""
    block = hist_sheet.range((1, 1), (HIST_CONTROL_ROWS, 6)).value
    if block is None:
        return []
    header = [str(h).strip() if h else "" for h in block[0]]
    idx = {h: i for i, h in enumerate(header)}
    slots = []
    for row in block[1:]:
        if row[idx["type"]] in (None, ""):
            continue
        slots.append(
            (
                row[idx["slot"]],
                str(row[idx["type"]]).strip().lower(),
                row[idx["row1"]],
                row[idx["row2"]],
                row[idx["row3"]],
                row[idx["label"]],
            )
        )
    return slots


def _write_historical_sheet(book: xw.Book, row_conventions: Dict[int, Convention]) -> None:
    """
    Historicalシート上部の管理表(A1:F11、slot/type/row1/row2/row3/label)を読み、
    各枠についてoutright/curve/flyのヒストリカル系列をローカルキャッシュから計算し、
    データブロック(Date/Value)とチャートを配置する。Neonには接続しない。
    """
    if HISTORICAL_SHEET_NAME not in [s.name for s in book.sheets]:
        return
    hist_sheet = book.sheets[HISTORICAL_SHEET_NAME]
    slots = _read_historical_control_table(hist_sheet)

    # 管理表(1〜11行目)は保持し、データ/チャート領域だけをクリアする
    last_col = hist_sheet.range((1, 1)).current_region.last_cell.column
    hist_sheet.range(
        (HIST_DATA_START_ROW, 1), (HIST_DATA_START_ROW + 400, max(last_col, 10 * HIST_BLOCK_WIDTH))
    ).clear_contents()
    for chart in list(hist_sheet.charts):
        chart.delete()

    for slot_no, kind, row1, row2, row3, label in slots:
        try:
            if kind == "outright":
                if row1 not in row_conventions:
                    raise ValueError(f"row1={row1!r} が有効なコンベンションの行ではありません")
                series = historical_par_rate_series(
                    row_conventions[int(row1)].fix_freq, row_conventions[int(row1)].fix_dcf,
                    row_conventions[int(row1)].float_freq, row_conventions[int(row1)].float_dcf,
                    row_conventions[int(row1)].roll_conv, tenor=row_conventions[int(row1)].tenor,
                )
            elif kind == "curve":
                if row1 not in row_conventions or row2 not in row_conventions:
                    raise ValueError("row1/row2が有効なコンベンションの行ではありません")
                series = historical_curve_series(
                    row_conventions[int(row1)], row_conventions[int(row2)]
                )
            elif kind == "fly":
                if row1 not in row_conventions or row2 not in row_conventions or row3 not in row_conventions:
                    raise ValueError("row1/row2/row3が有効なコンベンションの行ではありません")
                series = historical_fly_series(
                    row_conventions[int(row1)], row_conventions[int(row2)], row_conventions[int(row3)]
                )
            else:
                raise ValueError(f"typeは outright/curve/fly のいずれかです: {kind!r}")
        except Exception as exc:
            hist_sheet.range((1, 8)).value = f"#ERROR slot{slot_no}: {exc}"
            continue

        row_label = str(label) if label else f"slot{slot_no}_{kind}"
        i = int(slot_no) - 1
        base_col = 1 + i * HIST_BLOCK_WIDTH
        rate_col_letter = hist_sheet.range((HIST_DATA_START_ROW, base_col + 1)).address.split("$")[1]

        hist_sheet.range((HIST_DATA_START_ROW, base_col)).value = [[row_label, ""]]
        hist_sheet.range((HIST_DATA_START_ROW + 1, base_col)).value = [
            [p.as_of_date, p.par_rate] for p in series
        ]

        if series:
            # チャートはデータブロックの右隣ではなく、重なりを避けるため
            # 固定の1列(全ブロックの右側)に縦一列で並べる
            charts_col = 1 + 10 * HIST_BLOCK_WIDTH + 2
            chart = hist_sheet.charts.add()
            chart.chart_type = "line"
            chart.set_source_data(
                hist_sheet.range(
                    f"{rate_col_letter}{HIST_DATA_START_ROW + 1}:{rate_col_letter}{HIST_DATA_START_ROW + len(series)}"
                )
            )
            chart.name = f"hist_chart_slot{slot_no}"
            chart.top = hist_sheet.range((HIST_DATA_START_ROW, charts_col)).top + i * 240
            chart.left = hist_sheet.range((HIST_DATA_START_ROW, charts_col)).left
            chart.width = 400
            chart.height = 220


def refresh() -> None:
    book = xw.Book.caller()
    refresh_pricing_sheet(book)


if __name__ == "__main__":
    xw.Book("PricingSheet_template.xlsx").set_mock_caller()
    refresh()
