#!/usr/bin/env python3
"""
BOJ Swap Distortion - スタンドアロン Excel シート生成スクリプト

■ 計算定義（厳密）
  BOJスワップ M_i (JPBOJ1-8ONI): 各MPM間フォワードOIS金利（単利, ACT/365）
  FV_i = FV_{i-1} × (1 + M_i × ΔD_i / 365)  ← 各期間の割引係数の逆数を累積
  テナーT が期間k内(D_{k-1} < D_T ≤ D_k)の場合:
    FV_T = FV_{k-1} × (1 + M_k × (D_T - D_{k-1}) / 365)
  インプライドスポット金利 = (FV_T - 1) × 365 / D_T
  歪み (bps) = (実スポット金利 - インプライドスポット金利) × 10000

実行:
  python3 create_excel_sheet.py
"""

from datetime import date, timedelta
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import BarChart, LineChart, Reference

# ── カラー定数 ──────────────────────────────────────
C_TITLE_BG = "1F4E79"
C_TITLE_FG = "FFFFFF"
C_HDR_BG   = "2E75B6"
C_HDR_FG   = "FFFFFF"
C_INPUT_BG = "FFF2CC"   # 黄 = LSEG入力
C_CALC_BG  = "EBF3FB"   # 薄青 = 計算値
C_WARN_BG  = "FFE699"   # 警告
C_SEC_BG   = "D6E4F0"   # セクション見出し

def _fill(c): return PatternFill(fill_type="solid", fgColor=c)
def _font(bold=False, color="000000", size=11, italic=False):
    return Font(bold=bold, color=color, size=size, italic=italic)
def _border():
    s = Side(style="thin")
    return Border(left=s, right=s, top=s, bottom=s)

# ── ダミーデータ ──────────────────────────────────────
TRADE_DATE = date(2026, 3, 22)
SPOT_DATE  = date(2026, 3, 26)   # T+2 BD

# MPM会合日 → 実効日（翌営業日、ダミー: +1日で近似）
MPM_MEETING = [
    date(2026, 4, 28), date(2026, 6, 16), date(2026, 7, 31), date(2026, 9, 18),
    date(2026, 10, 30), date(2026, 12, 18), date(2027, 1, 28), date(2027, 3, 18),
]
MPM_EFF = [d + timedelta(days=1) for d in MPM_MEETING]  # 実効日（+1BD 近似）
MPM_DAYS = [(e - SPOT_DATE).days for e in MPM_EFF]        # 累積日数 (Spot→MPM_i_eff)

# BOJスワップ フォワード金利（小数 e.g. 0.005 = 0.5%）
M_RATES = [0.00495, 0.00500, 0.00510, 0.00530, 0.00535, 0.00570, 0.00590, 0.00620]

# LSEGティッカー
BOJ_TICKERS  = [f"JPBOJ{i}ONI=TRDT" for i in range(1, 9)]
SPOT_TENORS  = ["1M","2M","3M","4M","5M","6M","7M","8M","9M","10M","11M","12M","15M"]
SPOT_TICKERS = [
    "JPYOIS1M=ICAP","JPYOIS2M=ICAP","JPYOIS3M=ICAP","JPYOIS4M=ICAP",
    "JPYOIS5M=ICAP","JPYOIS6M=ICAP","JPYOIS7M=ICAP","JPYOIS8M=ICAP",
    "JPYOIS9M=ICAP","JPYOIS10M=ICAP","JPYOIS11M=ICAP","JPY1YOIS=ICAP",
    "JPYOIS15M=ICAP",
]
# ダミー スポット金利（小数）
SPOT_RATES = [0.00485,0.00490,0.00498,0.00510,0.00520,0.00535,
              0.00548,0.00558,0.00565,0.00570,0.00575,0.00580,0.00595]
# ダミー 実日数（ACT, MF適用後）
SPOT_DAYS  = [31,62,91,122,153,184,214,245,275,306,337,368,459]

# ── シートレイアウト定数 ──────────────────────────────
# data シート
DATA_BOJ_HDR  = 8   # BOJスワップ ヘッダー行
DATA_BOJ_START = 9  # M1 データ行（〜16）
DATA_SPT_SEC  = 18  # スポット金利 セクション行
DATA_SPT_HDR  = 19  # スポット金利 ヘッダー行
DATA_SPT_START = 20 # 1M データ行（〜32）

# analysis シート
BOOT_START = 8      # M1 ブートストラップ行（〜15）
DIST_START = 19     # 1M 歪み計算行（〜31）
NOTE_ROW   = 34     # 方法論ノート開始行


# ══════════════════════════════════════════════════════
#  data シート
# ══════════════════════════════════════════════════════
def _build_data_sheet(ws):
    ws.title = "data"

    # 列幅
    for col, w in zip("ABCDE", [22, 20, 16, 18, 28]):
        ws.column_dimensions[col].width = w

    # タイトル
    ws.merge_cells("A1:E1")
    c = ws["A1"]
    c.value = "BOJ Swap Distortion – データ入力シート（黄色セル = LSEG RTDフィード）"
    c.font = _font(bold=True, color=C_TITLE_FG, size=12)
    c.fill = _fill(C_TITLE_BG)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    # ダミー警告
    ws.merge_cells("A2:E2")
    c = ws["A2"]
    c.value = "⚠ 現在はダミーデータです。LSEGのRTD / Add-in でValue列を自動更新してください。"
    c.font = _font(bold=True, color="C00000", size=10)
    c.fill = _fill(C_WARN_BG)
    c.alignment = Alignment(horizontal="center")

    # 日付
    ws["A4"] = "トレード日"
    ws["B4"] = TRADE_DATE;  ws["B4"].number_format = "YYYY/MM/DD"
    ws["B4"].fill = _fill(C_INPUT_BG)
    ws["A5"] = "スポット日（T+2BD）"
    ws["B5"] = SPOT_DATE;   ws["B5"].number_format = "YYYY/MM/DD"
    ws["B5"].fill = _fill(C_INPUT_BG)
    ws["C5"] = "※ 実際はT+2BD、土日祝考慮。手動更新"
    ws["C5"].font = _font(italic=True, color="595959", size=9)

    # ── BOJスワップ セクション ──
    ws.merge_cells("A7:E7")
    c = ws["A7"]
    c.value = "■ BOJスワップ フォワード金利（JPBOJ1-8ONI=TRDT）"
    c.font = _font(bold=True, color=C_TITLE_FG)
    c.fill = _fill(C_HDR_BG)
    c.alignment = Alignment(horizontal="center")

    boj_hdrs = ["LSEGティッカー", "フォワード金利（%）", "MPM実効日（ダミー）",
                "累積日数\nSpot→MPM_i", "備考（MPM会合日）"]
    for j, h in enumerate(boj_hdrs):
        c = ws.cell(DATA_BOJ_HDR, j+1, h)
        c.font = _font(bold=True, color=C_HDR_FG, size=10)
        c.fill = _fill(C_HDR_BG)
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        c.border = _border()
    ws.row_dimensions[DATA_BOJ_HDR].height = 28

    for i in range(8):
        r = DATA_BOJ_START + i
        ws.cell(r, 1, BOJ_TICKERS[i]).font = _font(size=10)
        c = ws.cell(r, 2, M_RATES[i])
        c.number_format = "0.000%";  c.fill = _fill(C_INPUT_BG)
        c = ws.cell(r, 3, MPM_EFF[i])
        c.number_format = "YYYY/MM/DD";  c.fill = _fill(C_INPUT_BG)
        # 累積日数 = MPM実効日 - スポット日（自動計算）
        c = ws.cell(r, 4, f"=C{r}-$B$5")
        c.fill = _fill(C_CALC_BG)
        ws.cell(r, 5, f"会合{i+1}: {MPM_MEETING[i].strftime('%Y/%m/%d')} → 実効日:{MPM_EFF[i].strftime('%Y/%m/%d')}")
        ws.cell(r, 5).font = _font(size=9, color="595959")
        for j in range(1, 6):
            ws.cell(r, j).border = _border()

    # ── スポット金利 セクション ──
    ws.merge_cells(f"A{DATA_SPT_SEC}:E{DATA_SPT_SEC}")
    c = ws[f"A{DATA_SPT_SEC}"]
    c.value = "■ スポット金利 直接クォート（JPYOIS1-12M, 15M = ICAP）"
    c.font = _font(bold=True, color=C_TITLE_FG)
    c.fill = _fill(C_HDR_BG)
    c.alignment = Alignment(horizontal="center")

    spot_hdrs = ["LSEGティッカー", "スポット金利（%）", "実日数（ACT/MF）",
                 "テナー", "備考"]
    for j, h in enumerate(spot_hdrs):
        c = ws.cell(DATA_SPT_HDR, j+1, h)
        c.font = _font(bold=True, color=C_HDR_FG, size=10)
        c.fill = _fill(C_HDR_BG)
        c.alignment = Alignment(horizontal="center")
        c.border = _border()

    for i in range(13):
        r = DATA_SPT_START + i
        ws.cell(r, 1, SPOT_TICKERS[i]).font = _font(size=10)
        c = ws.cell(r, 2, SPOT_RATES[i])
        c.number_format = "0.000%";  c.fill = _fill(C_INPUT_BG)
        c = ws.cell(r, 3, SPOT_DAYS[i])
        c.fill = _fill(C_INPUT_BG)
        ws.cell(r, 4, SPOT_TENORS[i])
        ws.cell(r, 5, "← LSEGより（MF適用後の実日数）").font = _font(size=9, italic=True, color="595959")
        for j in range(1, 6):
            ws.cell(r, j).border = _border()


# ══════════════════════════════════════════════════════
#  analysis シート
# ══════════════════════════════════════════════════════
def _build_fv_formula(dt_ref: str) -> str:
    """
    FV(D_T) の分岐計算式（Excel 数式文字列）を返す。

    期間k内 (D_{k-1} < D_T <= D_k):
      FV_T = FV_{k-1} × (1 + M_k × (D_T - D_{k-1}) / 365)
      ※ k=1 は D_0=0, FV_0=1

    D_T > D_8（MPM8超）: M8レートで外挿（一定ON金利継続）
      FV_T = FV_8 × (1 + M_8 × (D_T - D_8) / 365)

    セル参照（analysis シート内の絶対参照）:
      C8-C15: 累積日数 D_i
      E8-E15: フォワード金利 M_i
      F8-F15: 累積FV FV_i
    """
    C = [f"$C${BOOT_START+i}" for i in range(8)]   # 累積日数 D_i
    E = [f"$E${BOOT_START+i}" for i in range(8)]   # フォワード金利 M_i
    F = [f"$F${BOOT_START+i}" for i in range(8)]   # 累積FV FV_i

    # MPM8超の外挿（最内側のデフォルト）
    formula = f"{F[7]}*(1+{E[7]}*({dt_ref}-{C[7]})/365)"

    # 内側（i=7, M8期間）から外側（i=0, M1期間）へネスト
    for i in range(7, -1, -1):
        if i == 0:
            # 期間1: D_0=0 なので D_T - D_0 = D_T
            inner = f"(1+{E[0]}*{dt_ref}/365)"
        else:
            # 期間i+1: FV_{i-1} × (1 + M_{i+1} × (D_T - D_i)/365)
            inner = f"{F[i-1]}*(1+{E[i]}*({dt_ref}-{C[i-1]})/365)"
        formula = f"IF({dt_ref}<={C[i]},{inner},{formula})"

    return "=" + formula


def _build_analysis_sheet(ws):
    ws.title = "analysis"

    # 列幅
    for col, w in zip("ABCDEFGH", [10,14,14,12,16,20,18,20]):
        ws.column_dimensions[col].width = w

    # ── タイトル ──
    ws.merge_cells("A1:H1")
    c = ws["A1"]
    c.value = "BOJスワップ → インプライドスポット金利 逆算・歪み分析"
    c.font = _font(bold=True, color=C_TITLE_FG, size=13)
    c.fill = _fill(C_TITLE_BG)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    # 日付参照
    ws["A3"] = "トレード日";  ws["B3"] = "=data!B4";  ws["B3"].number_format = "YYYY/MM/DD"
    ws["A4"] = "スポット日";  ws["B4"] = "=data!B5";  ws["B4"].number_format = "YYYY/MM/DD"

    # ── Step1: ブートストラップ ──
    ws.merge_cells("A6:H6")
    c = ws["A6"]
    c.value = "【Step 1】 ブートストラップ: BOJフォワード金利 → 累積FV（割引係数の逆数）"
    c.font = _font(bold=True);  c.fill = _fill(C_SEC_BG)

    boot_hdrs = ["テナー", "MPM実効日", "累積日数\nD_i", "期間日数\nΔD_i",
                 "フォワードM_i\n(小数)", "累積FV_i\n=1/DF_i", "割引係数\nDF_i",
                 "ゼロレート検証\n(FV_i-1)×365/D_i"]
    for j, h in enumerate(boot_hdrs):
        c = ws.cell(7, j+1, h)
        c.font = _font(bold=True, color=C_HDR_FG, size=10)
        c.fill = _fill(C_HDR_BG)
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        c.border = _border()
    ws.row_dimensions[7].height = 32

    for i in range(8):
        r = BOOT_START + i
        dr = DATA_BOJ_START + i    # data シートの対応行

        ws.cell(r, 1, f"M{i+1}").font = _font(bold=True)

        # MPM実効日
        c = ws.cell(r, 2, f"=data!C{dr}")
        c.number_format = "YYYY/MM/DD";  c.fill = _fill(C_CALC_BG)

        # 累積日数 D_i (data シートで計算済み)
        c = ws.cell(r, 3, f"=data!D{dr}")
        c.fill = _fill(C_CALC_BG)

        # 期間日数 ΔD_i
        fml_d = f"=C{r}" if i == 0 else f"=C{r}-C{r-1}"
        c = ws.cell(r, 4, fml_d);  c.fill = _fill(C_CALC_BG)

        # フォワード金利 M_i
        c = ws.cell(r, 5, f"=data!B{dr}")
        c.number_format = "0.0000%";  c.fill = _fill(C_CALC_BG)

        # 累積FV_i: FV_0=1, FV_i = FV_{i-1} × (1 + M_i × ΔD_i/365)
        fml_fv = f"=1+E{r}*D{r}/365" if i == 0 else f"=F{r-1}*(1+E{r}*D{r}/365)"
        c = ws.cell(r, 6, fml_fv)
        c.number_format = "0.0000000";  c.fill = _fill(C_CALC_BG)

        # DF_i = 1/FV_i
        c = ws.cell(r, 7, f"=1/F{r}")
        c.number_format = "0.0000000";  c.fill = _fill(C_CALC_BG)

        # ゼロレート検証 = (FV_i - 1) × 365/D_i
        c = ws.cell(r, 8, f"=(F{r}-1)*365/C{r}")
        c.number_format = "0.0000%";  c.fill = _fill(C_CALC_BG)

        for j in range(1, 9):
            ws.cell(r, j).border = _border()

    # ── Step2: 歪み計算 ──
    ws.merge_cells("A17:H17")
    c = ws["A17"]
    c.value = "【Step 2】 インプライドスポット金利の逆算 & 歪み計算"
    c.font = _font(bold=True);  c.fill = _fill(C_SEC_BG)

    dist_hdrs = ["テナー", "実日数\nD_T", "実スポット\n金利",
                 "FV(D_T)\n逆算値", "インプライド\nスポット金利",
                 "歪み\n実際－インプライド", "歪み\n(bps)", "備考"]
    for j, h in enumerate(dist_hdrs):
        c = ws.cell(18, j+1, h)
        c.font = _font(bold=True, color=C_HDR_FG, size=10)
        c.fill = _fill(C_HDR_BG)
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        c.border = _border()
    ws.row_dimensions[18].height = 32

    for i in range(13):
        r = DIST_START + i
        dr = DATA_SPT_START + i    # data シートの対応行

        ws.cell(r, 1, SPOT_TENORS[i]).font = _font(bold=True)

        # 実日数 D_T
        c = ws.cell(r, 2, f"=data!C{dr}")
        c.fill = _fill(C_CALC_BG)

        # 実スポット金利
        c = ws.cell(r, 3, f"=data!B{dr}")
        c.number_format = "0.0000%";  c.fill = _fill(C_INPUT_BG)

        # FV(D_T): 分岐計算式
        c = ws.cell(r, 4, _build_fv_formula(f"B{r}"))
        c.number_format = "0.0000000";  c.fill = _fill(C_CALC_BG)

        # インプライドスポット金利 = (FV_T - 1) × 365/D_T
        c = ws.cell(r, 5, f"=(D{r}-1)*365/B{r}")
        c.number_format = "0.0000%";  c.fill = _fill(C_CALC_BG)

        # 歪み (小数)
        c = ws.cell(r, 6, f"=C{r}-E{r}")
        c.number_format = "+0.0000%;-0.0000%;0.0000%"
        c.fill = _fill(C_CALC_BG)

        # 歪み (bps)
        c = ws.cell(r, 7, f"=(C{r}-E{r})*10000")
        c.number_format = '+0.00;-0.00;0.00'
        c.fill = _fill(C_CALC_BG)

        # 備考
        note = "※MPM8超→外挿" if i >= 11 else ""
        ws.cell(r, 8, note).font = _font(italic=True, color="595959", size=9)

        for j in range(1, 9):
            ws.cell(r, j).border = _border()

    # ── 方法論ノート ──
    ws.merge_cells(f"A{NOTE_ROW}:H{NOTE_ROW}")
    c = ws[f"A{NOTE_ROW}"]
    c.value = "【計算定義】 Act/365 Fixed, 単利建て（日本円OIS市場標準）"
    c.font = _font(bold=True);  c.fill = _fill(C_SEC_BG)

    notes = [
        "① デイカウント: ACT/365 Fixed（全期間共通）",
        "② BOJスワップ M_i (JPBOJ1-8ONI): フォワード期間OIS金利（単利建て）",
        "   JPBOJ1ONI: スポット日 → MPM1実効日  の期間フォワード金利",
        "   JPBOJ2ONI: MPM1実効日 → MPM2実効日 の期間フォワード金利（以降同様）",
        "③ 累積FV: FV_0=1,  FV_i = FV_{i-1} × (1 + M_i × ΔD_i/365)",
        "   ΔD_i = D(MPM_i実効日) - D(MPM_{i-1}実効日)  [i=1のみ D(MPM1) - D(Spot)]",
        "④ テナーT(実日数D_T)のFV逆算: D_{k-1} < D_T ≤ D_k の場合",
        "   FV_T = FV_{k-1} × (1 + M_k × (D_T - D_{k-1})/365)  ← 期間内一定ON金利を仮定",
        "⑤ インプライドスポット金利: r_implied = (FV_T - 1) × 365/D_T  [単利, ACT/365]",
        "⑥ 歪み = 実スポット金利 − インプライドスポット金利  [bps換算: ×10000]",
        "⑦ MPM8超テナー(15M等): M8フォワード金利で外挿（ON金利一定継続の仮定）",
        "※ スポット金利: JPYOIS1-12M=ICAP, 15M=ICAP（TONA OIS直接クォート）",
    ]
    for j, note in enumerate(notes):
        c = ws[f"A{NOTE_ROW+1+j}"]
        c.value = note
        indent = note.startswith("   ") or note.startswith("※")
        c.font = _font(size=10, color="404040" if indent else "000000")

    # ── チャート用データ（非表示列 J-M） ──
    CC = 10  # J列
    ws.cell(18, CC, "テナー")
    ws.cell(18, CC+1, "インプライドスポット(%)")
    ws.cell(18, CC+2, "実スポット(%)")
    ws.cell(18, CC+3, "歪み(bps)")
    for i in range(13):
        r = DIST_START + i
        ws.cell(r, CC,   SPOT_TENORS[i])
        ws.cell(r, CC+1, f"=E{r}")
        ws.cell(r, CC+2, f"=C{r}")
        ws.cell(r, CC+3, f"=G{r}")
    for col in range(CC, CC+4):
        ws.column_dimensions[get_column_letter(col)].hidden = True

    # ── Chart 1: ライン（インプライド vs 実スポット）──
    lc = LineChart()
    lc.title = "OIS曲線: インプライドスポット vs 実スポット金利"
    lc.style = 10
    lc.y_axis.title = "金利 (小数表示)";  lc.x_axis.title = "テナー"
    lc.width = 22;  lc.height = 12
    end_r = DIST_START + 12
    lc.add_data(Reference(ws, min_col=CC+1, min_row=18, max_row=end_r), titles_from_data=True)
    lc.add_data(Reference(ws, min_col=CC+2, min_row=18, max_row=end_r), titles_from_data=True)
    lc.set_categories(Reference(ws, min_col=CC, min_row=DIST_START, max_row=end_r))
    lc.series[0].graphicalProperties.line.solidFill = "2E75B6"
    lc.series[1].graphicalProperties.line.solidFill = "70AD47"
    ws.add_chart(lc, "I2")

    # ── Chart 2: バー（歪み bps）──
    bc = BarChart()
    bc.type = "col"
    bc.title = "歪み: 実スポット − インプライドスポット (bps)"
    bc.style = 10
    bc.y_axis.title = "bps";  bc.x_axis.title = "テナー"
    bc.width = 22;  bc.height = 11
    bc.add_data(Reference(ws, min_col=CC+3, min_row=18, max_row=end_r), titles_from_data=True)
    bc.set_categories(Reference(ws, min_col=CC, min_row=DIST_START, max_row=end_r))
    bc.series[0].graphicalProperties.solidFill = "ED7D31"
    ws.add_chart(bc, "I19")


# ══════════════════════════════════════════════════════
#  メイン
# ══════════════════════════════════════════════════════
def create_excel(output_path: str):
    wb = Workbook()
    _build_data_sheet(wb.active)
    _build_analysis_sheet(wb.create_sheet("analysis"))
    wb.save(output_path)
    print(f"✓ Saved: {output_path}")


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "output/boj_distortion_standalone.xlsx"
    create_excel(out)
