"""
分析結果を1枚のExcelシートに出力するモジュール。

■ レイアウト（1シート: "BOJ Swap Analysis"）
  A1   : タイトル・更新日時
  A4   : BOJスワップ入力テーブル（期間・会合日・実効日・フォワード金利）
  A15  : テナー分析テーブル（BOJ逆算・スポットOIS・乖離bps）
  H4   : グラフ1 – BOJスワップ階段状 + 逆算ゼロレート（ラインオーバーレイ）
  H24  : グラフ2 – 乖離(bps) バーチャート

■ チャートのステップ（階段状）実現方法
  openpyxlにネイティブのstep chartはないため、
  各区間を「開始日と終了日に同じレートを2点」記録したデータ系列を作成し
  ラインチャートで描画する。
"""

import pandas as pd
from datetime import date, datetime
from openpyxl import Workbook
from openpyxl.chart import LineChart, BarChart, Reference
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.styles.numbers import FORMAT_NUMBER
from openpyxl.utils import get_column_letter


# ---- カラー定義 ----
C_TITLE_BG   = "1F4E79"
C_TITLE_FG   = "FFFFFF"
C_HDR_BOJ_BG = "2E75B6"
C_HDR_SPT_BG = "70AD47"
C_HDR_DIST_BG = "ED7D31"
C_HDR_FG     = "FFFFFF"
C_POS_DIST   = "C6EFCE"  # 正の乖離
C_NEG_DIST   = "FFC7CE"  # 負の乖離
C_ZERO_DIST  = "FFEB9C"  # 中立


def _fill(hex_color: str) -> PatternFill:
    return PatternFill(fill_type="solid", fgColor=hex_color)


def _font(bold=False, color="000000", size=11) -> Font:
    return Font(bold=bold, color=color, size=size)


def _border_thin() -> Border:
    s = Side(style="thin")
    return Border(left=s, right=s, top=s, bottom=s)


def _write_header_row(ws, row: int, headers: list, bg_color: str, col_start=1):
    fill = _fill(bg_color)
    for i, h in enumerate(headers):
        c = ws.cell(row=row, column=col_start + i, value=h)
        c.fill = fill
        c.font = _font(bold=True, color="FFFFFF")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = _border_thin()


def _dist_fill(val: float) -> PatternFill:
    if val > 1.0:
        return _fill(C_POS_DIST)
    elif val < -1.0:
        return _fill(C_NEG_DIST)
    else:
        return _fill(C_ZERO_DIST)


def _set_col_widths(ws, widths: dict):
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def write_single_sheet(
    output_path: str,
    valuation_date: date,
    boj_rates: list,
    meeting_dates: list,
    effective_dates: list,
    pillar_summary: pd.DataFrame,
    tenor_table: pd.DataFrame,
    distortion_summary: pd.DataFrame,
    tona_1y_rate: float,
    is_dummy_spot: bool = True,
):
    """
    分析結果を1シートのExcelに書き出す。

    Parameters
    ----------
    output_path       : 出力パス
    valuation_date    : バリュエーション日
    boj_rates         : BOJ1-8フォワード金利リスト(%)
    meeting_dates     : 会合決定日リスト
    effective_dates   : 会合実効日リスト
    pillar_summary    : ブートストラップピラーのDF
    tenor_table       : テナーレートDF（zero_rate_pct, implied_forward_pct等）
    distortion_summary: 歪みサマリーDF
    tona_1y_rate      : 1Y TONA OIS(%)
    is_dummy_spot     : スポットOISがダミーデータかどうか（警告表示用）
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "BOJ Swap Analysis"

    # ================================================================
    # セクション1: タイトル・メタ情報 (Row 1-2)
    # ================================================================
    ws.merge_cells("A1:G1")
    t = ws["A1"]
    t.value = "BOJ Swap Distortion Analysis"
    t.font = _font(bold=True, size=14, color=C_TITLE_FG)
    t.fill = _fill(C_TITLE_BG)
    t.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    ws["A2"] = f"Valuation: {valuation_date}  |  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws["A2"].font = _font(color="595959", size=9)
    if is_dummy_spot:
        ws["E2"] = "⚠ Spot OIS: DUMMY DATA – replace with LSEG real-time feed"
        ws["E2"].font = _font(color="C00000", bold=True, size=9)

    # ================================================================
    # セクション2: BOJスワップ入力テーブル (Row 4-13)
    # ================================================================
    ws["A4"] = "■ BOJ Swap Inputs (Forward Rates)"
    ws["A4"].font = _font(bold=True, color=C_HDR_BOJ_BG)

    boj_headers = ["", "Meeting Date", "Effective Date", "Forward Rate (%)"]
    _write_header_row(ws, 5, boj_headers, C_HDR_BOJ_BG)

    for i, (rate, mdate, edate) in enumerate(zip(boj_rates, meeting_dates, effective_dates)):
        r = 6 + i
        ws.cell(r, 1, f"BOJ{i+1}").font = _font(bold=True)
        ws.cell(r, 2, str(mdate))
        ws.cell(r, 3, str(edate))
        c = ws.cell(r, 4, round(rate, 5))
        c.number_format = "0.00000"
        for col in range(1, 5):
            ws.cell(r, col).border = _border_thin()

    # 1Y TONA行
    r_1y = 6 + len(boj_rates)
    ws.cell(r_1y, 1, "1Y TONA").font = _font(bold=True, color=C_HDR_SPT_BG)
    ws.cell(r_1y, 4, round(tona_1y_rate, 5)).number_format = "0.00000"
    for col in range(1, 5):
        ws.cell(r_1y, col).border = _border_thin()

    # ================================================================
    # セクション3: テナー分析テーブル (Row 15-28)
    # ================================================================
    tenor_start_row = 15
    ws[f"A{tenor_start_row - 1}"] = "■ Tenor Analysis"
    ws[f"A{tenor_start_row - 1}"].font = _font(bold=True, color=C_HDR_BOJ_BG)

    tenor_headers = [
        "Tenor",
        "Settle Date",
        "Days",
        "BOJ Implied (%)",      # BOJスワップから逆算したゼロレート
        "Spot OIS (%)",         # LSEG直接クォート
        "Distortion (bps)",     # BOJ Implied - Spot OIS
        "Impl. Fwd (%)",        # テナー間インプライドフォワード
    ]
    _write_header_row(ws, tenor_start_row, tenor_headers, C_HDR_BOJ_BG)

    # ヘッダー色分け（列ごとに変える）
    ws.cell(tenor_start_row, 4).fill = _fill(C_HDR_BOJ_BG)
    ws.cell(tenor_start_row, 5).fill = _fill(C_HDR_SPT_BG)
    ws.cell(tenor_start_row, 6).fill = _fill(C_HDR_DIST_BG)

    for i, row in distortion_summary.iterrows():
        r = tenor_start_row + 1 + i
        tenor_label = row["Tenor"]
        ws.cell(r, 1, tenor_label).font = _font(bold=True)
        ws.cell(r, 2, str(row["Settle Date"]))
        ws.cell(r, 3, row["Days"])
        c_boj = ws.cell(r, 4, round(row["BOJ Implied Rate (%)"], 5))
        c_boj.number_format = "0.00000"
        c_spot = ws.cell(r, 5, round(row["Spot OIS Rate (%)"], 5))
        c_spot.number_format = "0.00000"
        dist_val = row["Distortion (bps)"]
        c_dist = ws.cell(r, 6, round(dist_val, 2))
        c_dist.number_format = "0.00"
        c_dist.fill = _dist_fill(dist_val)

        # インプライドフォワード
        tenor_row = tenor_table[tenor_table["tenor_months"] == i + 1]
        if not tenor_row.empty:
            fwd = tenor_row.iloc[0].get("implied_forward_pct")
            if fwd is not None and str(fwd) != "nan":
                ws.cell(r, 7, round(float(fwd), 5)).number_format = "0.00000"

        for col in range(1, 8):
            ws.cell(r, col).border = _border_thin()

    # ================================================================
    # セクション4: チャート用データ（非表示列 I-P に埋め込み）
    # ================================================================
    # 階段状グラフのための展開データ
    # 各BOJ期間について (開始日テキスト, 終了日テキスト, レート) の2点を記録

    step_data_col_start = 9   # 列I
    sdc = step_data_col_start

    # -- 4a. BOJ階段状データ --
    ws.cell(1, sdc, "ChartData_Step")
    ws.cell(2, sdc, "Label")
    ws.cell(2, sdc + 1, "BOJ Fwd (%)")
    ws.cell(2, sdc + 2, "BOJ Zero (%)")   # 逆算ゼロレート（スムーズ曲線）
    ws.cell(2, sdc + 3, "Spot OIS (%)")

    # 階段状データ: 各BOJ期間の開始・終了に同レートを2点置く
    step_rows = []
    prev_eff = effective_dates[0]  # BOJ1の前はスポット日（会合前期間の開始）

    # スポット日から会合1実効日までの期間（BOJ1）
    from src.calendar_jpy import spot_date
    spot = spot_date(valuation_date)

    period_starts = [spot] + list(effective_dates[:-1])
    period_ends = list(effective_dates)

    for i, (pstart, pend, rate) in enumerate(zip(period_starts, period_ends, boj_rates)):
        label_start = f"BOJ{i+1} start\n({pstart})"
        label_end   = f"BOJ{i+1} end\n({pend})"
        step_rows.append((label_start, rate))
        step_rows.append((label_end, rate))

    for idx, (label, rate) in enumerate(step_rows):
        r = 3 + idx
        ws.cell(r, sdc, label)
        ws.cell(r, sdc + 1, round(rate, 5))

    step_data_end_row = 3 + len(step_rows) - 1

    # -- 4b. ゼロレート・スポットOISデータ（標準テナー = 1M-12M）--
    zero_col_start = sdc + 4   # 列M
    zc = zero_col_start
    ws.cell(2, zc, "Tenor")
    ws.cell(2, zc + 1, "BOJ Implied (%)")
    ws.cell(2, zc + 2, "Spot OIS (%)")
    ws.cell(2, zc + 3, "Dist (bps)")

    for i, row in distortion_summary.iterrows():
        r = 3 + i
        ws.cell(r, zc, row["Tenor"])
        ws.cell(r, zc + 1, round(row["BOJ Implied Rate (%)"], 5))
        ws.cell(r, zc + 2, round(row["Spot OIS Rate (%)"], 5))
        ws.cell(r, zc + 3, round(row["Distortion (bps)"], 2))

    n_tenors = len(distortion_summary)
    zero_data_end_row = 3 + n_tenors - 1

    # ================================================================
    # セクション5: グラフ
    # ================================================================
    chart_col = "H"
    chart_col_num = 8

    # -- Chart 1: BOJスワップ階段状 + 逆算ゼロレート --
    lc = LineChart()
    lc.title = "BOJ Swap: Forward Rates (step) vs BOJ Implied Zero Rate"
    lc.style = 10
    lc.y_axis.title = "Rate (%)"
    lc.x_axis.title = ""
    lc.width = 24
    lc.height = 14

    # BOJフォワード（階段状: 列sdc+1）
    ref_step = Reference(ws, min_col=sdc + 1, min_row=2, max_row=step_data_end_row)
    lc.add_data(ref_step, titles_from_data=True)
    cats_step = Reference(ws, min_col=sdc, min_row=3, max_row=step_data_end_row)
    lc.set_categories(cats_step)

    # 階段状の系列をステップ風に（smoothを無効化）
    lc.series[0].smooth = False
    lc.series[0].graphicalProperties.line.width = 20000  # 2pt
    lc.series[0].graphicalProperties.line.solidFill = "2E75B6"

    ws.add_chart(lc, f"{chart_col}4")

    # -- Chart 2: BOJ逆算ゼロレート vs スポットOIS（ライン） --
    lc2 = LineChart()
    lc2.title = "OIS Curve: BOJ Implied Zero vs Spot OIS (Direct Quote)"
    lc2.style = 10
    lc2.y_axis.title = "Rate (%)"
    lc2.x_axis.title = "Tenor"
    lc2.width = 24
    lc2.height = 14

    ref_boj_zero = Reference(ws, min_col=zc + 1, min_row=2, max_row=zero_data_end_row)
    ref_spot_ois = Reference(ws, min_col=zc + 2, min_row=2, max_row=zero_data_end_row)
    cats_tenor = Reference(ws, min_col=zc, min_row=3, max_row=zero_data_end_row)

    lc2.add_data(ref_boj_zero, titles_from_data=True)
    lc2.add_data(ref_spot_ois, titles_from_data=True)
    lc2.set_categories(cats_tenor)

    lc2.series[0].smooth = False
    lc2.series[0].graphicalProperties.line.solidFill = "2E75B6"
    lc2.series[1].smooth = False
    lc2.series[1].graphicalProperties.line.solidFill = "70AD47"

    ws.add_chart(lc2, f"{chart_col}24")

    # -- Chart 3: 歪みバーチャート --
    bc = BarChart()
    bc.type = "col"
    bc.title = "Distortion: BOJ Implied - Spot OIS (bps)"
    bc.style = 10
    bc.y_axis.title = "bps"
    bc.x_axis.title = "Tenor"
    bc.width = 24
    bc.height = 11

    ref_dist = Reference(ws, min_col=zc + 3, min_row=2, max_row=zero_data_end_row)
    bc.add_data(ref_dist, titles_from_data=True)
    bc.set_categories(cats_tenor)
    bc.series[0].graphicalProperties.solidFill = "ED7D31"

    ws.add_chart(bc, f"{chart_col}44")

    # ================================================================
    # 列幅設定
    # ================================================================
    _set_col_widths(ws, {
        "A": 12, "B": 14, "C": 14, "D": 16,
        "E": 16, "F": 16, "G": 16, "H": 2,
    })
    # チャート用データ列は非表示
    for col_num in range(sdc, zc + 5):
        col_letter = get_column_letter(col_num)
        ws.column_dimensions[col_letter].hidden = True

    wb.save(output_path)
    print(f"Excel saved: {output_path}")
    return output_path
