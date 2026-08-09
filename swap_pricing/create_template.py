"""
PricingSheetのひな形（.xlsx）を生成するスクリプト。

openpyxlではマクロ付き(.xlsm)を新規生成できないため、まず素の.xlsxを作成する。
実際に使う際は、Excelでこのファイルを開き、Alt+F11でvba/PricingSheet.basを
インポートし、ボタンを配置した上で .xlsm として保存し直す。

使い方:
    python -m swap_pricing.create_template
"""

from openpyxl import Workbook

from swap_pricing.paths import REPO_ROOT

OUTPUT_PATH = REPO_ROOT / "PricingSheet_template.xlsx"


def create_pricing_sheet_template(output_path=OUTPUT_PATH) -> None:
    wb = Workbook()

    realtime_ws = wb.active
    realtime_ws.title = "RealTime"
    realtime_ws.append(["Ticker", "Value"])

    calc_ws = wb.create_sheet("Calc")
    calc_ws.append(["Ticker", "RealTimeValue"])

    wb.save(output_path)
    print(f"Template created: {output_path}")


if __name__ == "__main__":
    create_pricing_sheet_template()
