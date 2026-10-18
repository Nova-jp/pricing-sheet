"""
リアルタイムのレートの受け渡し(engine/common/live_store.py)のUDF。

  - PublishLive : LiveFeed.xlsm(自動計算のExcelで開く)専用。RtGetの範囲を受け取り、data/live.db に書く。
                  自動計算なので、範囲のどこかが更新される(ティック)たびに呼ばれる
  - LiveQuotes  : PricingSheet.xlsm・SwapAnalysis.xlsm(手動計算)用。data/live.db の最新の行を返し、
                  呼び出したシートの固定の値として保存する(volatile)
  - LiveSnapshot: 同上。そのシートの固定の値(最後にLiveQuotesが読んだ行)を返す(非volatile)
  - LiveInfo    : 同上。そのシートが読んだ行の値がLiveFeedで最後に変わった時刻を返す

■ 取得モード(手動計算のブック)
  各フロントシートの1行目のB1(プルダウン Live/Fixed)で、シートごとに選ぶ。数式は
    =IF($B$1="Fixed", LiveSnapshot("swap"), LiveQuotes("swap"))
  - Live : LiveQuotesはvolatileなので、Shift+F9のたびに最新を読む(それに依存するセルも再計算される)
  - Fixed: IFでLiveQuotesが呼ばれないと、そのセルはvolatileでなくなる(Excelの実機で確認済み)。
           Shift+F9で再計算されるのは入力を変えたセル(例 追加したコンベンションの行)だけで、
           それらは直前にLiveで読んだのと同じレートで計算される。Fixedに切り替えた直後に1回だけ
           LiveSnapshotが呼ばれ、保存しておいた値(切り替える前に計算に使ったレート)を返す
  固定の値はブック・シート・データセットごとに data/live.db に保存するので、Ctrl+Alt+F9やUDFサーバの
  再起動でも変わらない(docs/decisions.md)。呼び出したシートはxlwingsの引数 caller(呼び出し元のセル)で知る。
  PublishLiveだけはデータを書き出すUDF(値を返すだけの他のUDFと違う)。VBAなしで自動計算の
  ブックからデータを渡すため、LiveFeedブックに限って置く(docs/decisions.md)。
"""

import datetime
from typing import List

import xlwings as xw

from engine.common.live_store import LiveStore


def _live_store() -> LiveStore:
    """受け渡しのDB(テストで差し替えられるように1か所にまとめる)。"""
    return LiveStore()


@xw.func
@xw.arg("values", ndim=2)
def PublishLive(dataset, values) -> str:
    """LiveFeed.xlsm専用。values(例 RealTime!C2:F45)をデータセット名 dataset で data/live.db に書く。
    値が前回と同じなら書かない。セルには最後に書いた時刻と行数を表示する。"""
    rows = [list(r) if r is not None else [] for r in (values or [])]
    store = _live_store()
    stamp = store.publish(dataset, rows)
    if stamp is None:
        stamp = store.read(dataset).updated_at
    return f"{str(dataset).strip().lower()}: {stamp:%H:%M:%S} 更新 / {len(rows)}行"


@xw.func(volatile=True)
def LiveQuotes(dataset, caller=None) -> List[list]:
    """data/live.db のデータセットの最新の行(PublishLiveに渡した範囲と同じ形)。空欄は""。
    読んだ行を呼び出したシートの固定の値として保存する(LiveSnapshotが返す)。
    volatile: 取得モードがLiveのとき(IFの分岐でこの関数が呼ばれるとき)だけ毎回再計算される。"""
    return _as_table(_live_store().read_live(dataset, *_where(caller)).rows)


@xw.func
def LiveSnapshot(dataset, caller=None) -> List[list]:
    """呼び出したシートの固定の値(そのシートで最後にLiveQuotesが読んだ行)。LiveQuotesと同じ形。"""
    return _as_table(_live_store().read_snapshot(dataset, *_where(caller)).rows)


@xw.func
@xw.arg("quotes", ndim=2)
def LiveInfo(dataset, quotes=None, caller=None) -> datetime.datetime:
    """呼び出したシートが読んだ行(LiveQuotes/LiveSnapshot)の値が、LiveFeedで最後に変わった時刻
    (セルの書式を日時にして表示する)。
    quotes: そのシートのLiveQuotes/LiveSnapshotのセル(例 $H$3#)。値は使わず、そのセルが
            再計算されたときだけこのセルも再計算されるようにするために渡す。"""
    if not quotes or len(quotes[0]) < 2:  # 読めていない(エラーの文字列が1つだけ来る)
        raise ValueError("ライブのレートを読めていません(LiveQuotesのセルを確認)")
    return _live_store().read_snapshot(dataset, *_where(caller)).updated_at


def _where(caller):
    """呼び出し元のセルの(ブック名, シート名)。固定の値を保存する場所のキー。"""
    if caller is None:
        raise ValueError("呼び出し元のセルが分かりません(Excelのセルから呼んでください)")
    sheet = caller.sheet
    return sheet.book.name, sheet.name


def _as_table(rows) -> List[list]:
    width = max((len(r) for r in rows), default=0)
    if width == 0:
        return [[""]]
    return [["" if v is None else v for v in row] + [""] * (width - len(row)) for row in rows]
