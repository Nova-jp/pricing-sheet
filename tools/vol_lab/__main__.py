"""
    python -m tools.vol_lab            # DBのデータをVolLab.xlsxに書き込む(無ければtemplates/からコピー)
    python -m tools.vol_lab --build    # ブックの構成を作り直す(開発用。既存のブックは --force で上書き)
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

from engine.common.historical_store import HistoricalStore
from engine.common.paths import REPO_ROOT

BOOK = "VolLab.xlsx"
WORKBOOK_PATH = REPO_ROOT / BOOK
TEMPLATE_PATH = REPO_ROOT / "templates" / BOOK


def _check_closed(path: Path) -> None:
    if (path.parent / f"~${path.name}").exists():
        raise SystemExit(f"エラー: {path.name} がExcelで開かれています。閉じてから実行してください")


def build_workbook(path: Path, force: bool) -> None:
    from tools.vol_lab.build import build

    if path.exists() and not force:
        raise SystemExit(f"エラー: {path.name} は既にあります(作り直すと設定の値も消えます。上書きするなら --force)")
    _check_closed(path)
    build(path)
    print(f"作成: {path.name}(データは空。python -m tools.vol_lab でDBのデータを書き込む)")


def fill(path: Path) -> None:
    from tools.vol_lab.data import compute
    from tools.vol_lab.fill import as_rows, fill_workbook

    _check_closed(path)
    if not path.exists():
        if not TEMPLATE_PATH.exists():
            raise SystemExit(f"エラー: {path.name} も templates/{BOOK} もありません")
        shutil.copyfile(TEMPLATE_PATH, path)
        print(f"templates/{BOOK} をコピーしました")
    store = HistoricalStore()
    if not store.exists():
        raise SystemExit("エラー: ヒストリカルDBがありません(python -m engine.common.morning_batch を先に実行)")

    started = time.time()

    def progress(i: int, n: int) -> None:
        if i % 50 == 0 or i == n:
            print(f"  Fwdの計算 {i}/{n}日 ({time.time() - started:.0f}秒)", flush=True)

    data = compute(store, store.vol_quotes(), progress=progress)
    if not data.dates:
        raise SystemExit("エラー: DBにカーブがありません")
    from tools.vol_lab.build import MAX_ROWS
    keep = slice(-MAX_ROWS, None)  # 上限を超えたら新しい方を残す(累積は差だけを使うので途中から切ってよい)
    if len(data.dates) > MAX_ROWS:
        print(f"警告: データが{len(data.dates)}日あり、ブックの上限({MAX_ROWS}日)を超えるため新しい{MAX_ROWS}日だけ書き込みます")
    d = data.dates[keep]
    fill_workbook(path, {
        "IV": as_rows(d, data.iv[keep]),
        "dF": as_rows(d, data.dfwd[keep]),
        "dF2累積": as_rows(d, data.cum_sq[keep]),
        "Fwd": as_rows(d, data.fwd[keep]),
        "残存": as_rows(d, data.days_bd[keep], data.days_cal[keep]),
        "追跡": as_rows(d, data.track_sumsq[keep], data.track_n[keep]),
    })
    print(f"書き込み: {path.name}({d[0]}〜{d[-1]}、{len(d)}日)。開くと全体が再計算されます")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ボラ分析のたたき台ブック(VolLab.xlsx)")
    parser.add_argument("--build", action="store_true", help="ブックの構成を作り直す(開発用)")
    parser.add_argument("--force", action="store_true", help="--build で既存のブックを上書きする")
    parser.add_argument("--path", type=Path, default=WORKBOOK_PATH, help=f"対象のブック(既定: 直下の{BOOK})")
    args = parser.parse_args(argv)
    if args.build:
        build_workbook(args.path, args.force)
    else:
        fill(args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
