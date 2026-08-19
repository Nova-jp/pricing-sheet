# 全体構成

## 目的

任意のコンベンションのJPY OIS(TONA)スワップについて、
(a) リアルタイムのパーレート/PV/デルタ と (b) ヒストリカルなアウトライト推移を
1つのExcelブック(`PricingSheet.xlsx`)上で計算・表示する。

## データフロー

```
[Neon(自前Postgres, irs_data)]
        │  1日1回 07:00 JST (Cloud Scheduler)
        ▼
[Cloud Run Job: pricing-sheet-morning-batch]
  swap_pricing/morning_batch.py
    - get_ois_rates_for_range() でNeonから生レートを一括取得
    - 各日について bootstrap_curve() で検証(異常データの早期検知)
    - local_cache.save_rates() で生レートをSQLiteに保存(カーブは保存しない)
        │
        ▼  curve_cache.db を生成
[GCS: 公開オブジェクト(バケット自体は非公開、対象オブジェクトのみ公開読み取り)]
  パスにUUIDを含めることで「公開だが推測困難」を実現
        │  会社PCから認証情報なしでダウンロード(curl/PowerShell)
        ▼
[会社PC: data/curve_cache.db]
        │  日中はここだけを参照。Neonには一切直接アクセスしない
        ▼
[Excel: PricingSheet.xlsx]
  RealTimeシート(LSEGアドインが手動更新するTicker/Value) ─┐
                                                            ├─▶ UDF(swap_pricing/udf.py)
  pricing/Historicalシートの各セルの数式(F9で再計算)      ─┘      → QuantLibでカーブ構築・評価
```

## なぜこの構成か(要点だけ、詳細は [decisions.md](decisions.md))

- Neonの接続情報を会社PCに一切持たせたくない → GCPが仲介し、会社PCは
  「公開だが推測困難なURLからファイルを1つ落とすだけ」にする。
- 日中の計算でNeonのコールドスタート遅延を踏みたくない → 朝バッチで
  ローカルSQLiteに退避し、日中はローカルのみ参照。
- ローカルキャッシュには**生レート**(スポットカーブの各テナー)だけを保存し、
  ブートストラップ済みのカーブそのものは保存しない。ヒストリカルの
  アウトライト計算も日中のリアルタイム計算も**同じ`bootstrap_curve()`**を
  通すことで、計算経路の食い違いを構造的に排除している。

## 計算エンジン(`swap_pricing/`)

| ファイル | 役割 |
|---|---|
| `calendars.py` | `ql.Japan()`カレンダーの公開、営業日判定・加算 |
| `daycount.py` | 日数計算方式(Act/365F, 30/360等)の文字列→QuantLibオブジェクト変換 |
| `schedule.py` | スケジュール生成(`ql.Schedule`ラッパー) |
| `curve.py` | スポットレート辞書→`ql.PiecewiseConvexMonotoneForward`によるカーブ構築。O/Nは`DepositRateHelper`、それ以外は`OISRateHelper` |
| `swap_pricer.py` | `ql.OvernightIndexedSwap`によるPV・パーレート計算。過去起算のスワップは`local_cache`のO/Nレートを実績フィキシングの代用として登録 |
| `risk.py` | 並行/バケットデルタ(全テナー再ブートストラップ方式)、notional逆算 |
| `historical_pricer.py` | キャッシュ済み日次カーブから、任意コンベンションのヒストリカル・パーレート系列を計算。標準コンベンションは`ql.MakeOIS`で厳密一致させる |
| `local_cache.py` | 生レートのSQLiteキャッシュ(`data/curve_cache.db`)。読み書きのみ、計算ロジックは持たない |
| `neon_client.py` | Neon接続・生レート取得(**Cloud Run上でのみ使用**。会社PCの日中実行では一切呼ばれない) |
| `morning_batch.py` | 朝バッチ本体(Neon→検証→ローカルキャッシュ保存) |
| `paths.py` | リポジトリ内パス解決(クローン先の絶対パスに依存しない) |
| `udf.py` | **Excelから直接呼べるUDF定義。ここが計算エンジンとExcelの唯一の接点** |

## UDF専用設計(VBAなし)

- `PricingSheet.py`(ワークブックと同名)は`from swap_pricing.udf import *`のみ。
  xlwingsのExcelアドイン(Windows)は「Import Functions」実行時に
  **ワークブックと同名の.pyファイル**からUDFを探すため、実体を
  `swap_pricing/udf.py`に置いたうえで、ここに再エクスポートしている。
- ブックの計算方法は「手動」。UDFは非volatileのままでよく、F9(再計算)を
  押すたびに最新のセル値を使って再評価される。VBAマクロ・ボタンは一切不要。
- UDFにするのは**QuantLibが必要な計算だけ**(パーレート、PV、デルタ、
  notional逆算、ヒストリカル、バケットデルタ)。カーブ(スプレッド)や
  フライ、合計といった四則演算はExcel自身の数式(`=J2#-C2#`等、スピル参照)
  に任せる。「エクセル上は平易な数式、高度な計算だけUDF化」という方針。

## Excelブック構成(`PricingSheet.xlsx`)

シート: `RealTime` / `pricing` / `Historical` / `Calc` / `_xlwings.conf`

### RealTimeシート

Ticker(A列) / Value(B列)。LSEGアドインが手動更新。テナー表記は全て大文字、
2Y/3Yのみ`24M`/`36M`表記(ヒストリカル側=Neonの`2Y`/`3Y`表記とは
`curve.py`の`TENOR_ALIASES`で吸収)。行36-43はM1-M8(BOJ会合デート物、
現状ブートストラップ未使用)。行45に`include_15_18_21m`トグルあり。

### pricingシート

列A-W: `flag / risk_flag / remarks / notional / delta(target) / adj_notional /
start / tenor / end / fix rate / fix freq / fix dcf / index / float freq /
float dcf / roll conv / target fixrate / spred / fly / pay/rec / PV /
delta(bump) / delta(annuity)`。target fixrate・PV・delta・adj_notionalは
UDF、spred・flyはExcel数式(1行上/下のtarget fixrateの単純な四則演算)。

列Z以降: リスクグリッド。`risk_flag=1`の行だけ`BucketedDelta(...)`
(`include_labels=FALSE`で値行のみ)を評価し、Z201行で列ごとに`SUM`。

### Historicalシート

2Y/5Y/10Yの`HistoricalOutright`UDF(Date/Rateの2列スピル配列)+
2s10sカーブ・2s5s10sフライをExcel数式(スピル参照`#`)で計算。

## クラウド側(`cloud/`)

Cloud Run Job(`entrypoint.py`)がコンテナ内で`morning_batch.run()`を実行し、
生成された`curve_cache.db`をGCSの公開オブジェクトにアップロードする
(バケット自体はfine-grained ACLで非公開、対象オブジェクトのみ
`blob.make_public()`で読み取り専用公開)。Cloud SchedulerがHTTPトリガーで
毎朝07:00 JSTに起動。実際のプロジェクトID・バケット名・URLは
`OPERATIONS.md`(gitignore対象)を参照。
