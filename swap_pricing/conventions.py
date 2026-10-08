"""
業務上のコンベンション表記 → QuantLibの引数・オブジェクトへの変換を一か所にまとめたモジュール。

pricingシート・RealTimeシート等の入力(テナー表記、dcf、freq、roll conv、IMMコード)を
QuantLibが受け取れる形に変換する対応表と、日本の営業日カレンダーだけを持つ。
営業日判定・加算・スケジュール生成等の日付アルゴリズムは全てQuantLib純正のAPI
(CALENDAR.isBusinessDay/adjust/advance、ql.MakeOIS等)を呼び出し側で直接使い、
ここに自前の日付計算ロジックは置かない。Python date ⇄ ql.Date の変換も
ql.Date.from_date()/to_date() を呼び出し側で直接使う。

(Excelの日付シリアル値など、Excelとの境界の型変換は swap_pricing/udf.py に置く)

■ カレンダー
  ql.Japan()。jpholiday(祝日法ベース)と異なり、1/2・1/3等の金融機関特有の
  休業日も正しく非営業日として扱う(確認済み)。

■ dcf(日数計算方式)
  - "act/365fixed": Act/365 Fixed
  - "30/360": 30/360 US(Bond Basis, ISDA定義)。QuantLibの Thirty360.BondBasis が
    厳密に一致することを確認済み(Feb28,2007->Aug31,2007 = 183/360)。
    ※ Thirty360.USA は2月末特例が入るため別物(不一致)。BondBasisを使うこと。

■ roll convention
  - STD: ql.DateGeneration.Forward, endOfMonth=False
         (start起点で正規期間を並べ、端数はショート・バックスタブ)
  - EOM: 同じくForwardだが endOfMonth=True を渡す。
  - IMM: ql.DateGeneration.ThirdWednesday(各周期の月の第3水曜日にロール)
  休日調整は常にModified Following(swap_pricer側で指定)。

  EOMでもendOfMonth=Trueを渡すだけで、実際にEOMロールになるかどうかの判定
  (startが月末かどうか)は行わない。ql.Schedule/Calendar.advance 自体が
  「effective dateが月末かどうか」でこのフラグを内部的にゲートしているため
  (startが月末でなければTrueを渡してもFalseと全く同じ結果になる)、ここで
  CALENDAR.isEndOfMonth() を自前で判定するのは冗長かつ、QuantLib側の判定と
  ズレるリスクがあるだけ。

■ IMMコード
  月記号(H=3月, M=6月, U=9月, Z=12月)+2桁の年(例: M27 = 2027年6月の第3水曜)。
  ql.IMM.date() は年が1桁のコード(M7)しか扱えず、しかも「基準日以降で次に来る日付」
  として解釈するため、過去のIMM日(例: 今日から見たU26)を表せない。そのため
  コードの読み取り(月記号・年)だけをここで行い、日付自体は ql.IMM.nextDate
  (その月の1日の次のIMM日=その月の第3水曜)でQuantLibに求めさせる。
  年は2000年代の2桁のみ受け付ける(1桁は年代が曖昧になるため不可)。
  返すのは休日調整前のIMM日で、休日調整はスケジュール生成(Modified Following)に任せる。

■ start列のテナー入力(フォワードスタート)
  start列に '10y' 等のテナーを入れると、評価日(ヒストリカルでは基準日)のスポット日+tenorを
  起算日にする(例: start=10y, tenor=20y で 10y20y)。ql.MakeOISのフォワードスタートと同じく
  休日調整前の日付で、休日調整はスケジュール生成に任せる。

■ テナー表記の読み替え(TENOR_ALIASES)
  RealTimeシート(Excel)は Reuters フィードの都合で 1Y/2Y/3Y を 12M/24M/36M
  表記にしている(historical_data.xlsxのMidシートも同じ表記)。
  カーブ構築の入力境界(curve.bootstrap_curve)で正規化し、内部は常に 1Y/2Y/3Y 表記で扱う。
"""

import re
from datetime import date

import QuantLib as ql

# --- カレンダー ---
CALENDAR = ql.Japan()

# --- テナー表記 ---
TENOR_ALIASES = {"12m": "1y", "24m": "2y", "36m": "3y"}

# --- dcf ---
ACT_365_FIXED = "act/365fixed"
THIRTY_360_US = "30/360"
SUPPORTED_DCF = (ACT_365_FIXED, THIRTY_360_US)


def day_counter(convention: str) -> ql.DayCounter:
    if convention == ACT_365_FIXED:
        return ql.Actual365Fixed()
    if convention == THIRTY_360_US:
        return ql.Thirty360(ql.Thirty360.BondBasis)
    raise ValueError(
        f"未対応のdcfコンベンションです: {convention!r} (対応: {SUPPORTED_DCF})"
    )


# --- 利払い頻度 ---
FREQ_TO_QL_FREQUENCY = {
    "PA": ql.Annual,
    "SA": ql.Semiannual,
    "QA": ql.Quarterly,
    "1m": ql.Monthly,
}


# --- roll convention ---
ROLL_STD = "STD"
ROLL_EOM = "EOM"
ROLL_IMM = "IMM"
SUPPORTED_ROLL = (ROLL_STD, ROLL_EOM, ROLL_IMM)


def rule_and_eom(roll_conv: str) -> tuple:
    """roll convention → (ql.DateGeneration の生成ルール, endOfMonth)。"""
    if roll_conv not in SUPPORTED_ROLL:
        raise ValueError(f"未対応のroll conventionです: {roll_conv!r} (対応: {SUPPORTED_ROLL})")
    if roll_conv == ROLL_IMM:
        return ql.DateGeneration.ThirdWednesday, False
    if roll_conv == ROLL_EOM:
        return ql.DateGeneration.Forward, True
    return ql.DateGeneration.Forward, False


# --- IMMコード ---
_IMM_MONTHS = {"H": ql.March, "M": ql.June, "U": ql.September, "Z": ql.December}
_IMM_CODE = re.compile(r"^([HMUZ])(\d{2})$", re.IGNORECASE)


def is_imm_code(value) -> bool:
    return isinstance(value, str) and _IMM_CODE.match(value.strip()) is not None


def imm_code_to_date(code: str) -> date:
    """'M27' → 2027-06-16(2027年6月の第3水曜、休日調整前)。"""
    m = _IMM_CODE.match(code.strip())
    if m is None:
        raise ValueError(f"IMMコードは月記号(H/M/U/Z)+2桁の年で入力してください(例: M27): {code!r}")
    first_of_month = ql.Date(1, _IMM_MONTHS[m.group(1).upper()], 2000 + int(m.group(2)))
    return ql.IMM.nextDate(first_of_month).to_date()


# --- start列のテナー入力(フォワードスタート) ---
_TENOR = re.compile(r"^\d+[DWMY]$", re.IGNORECASE)


def is_tenor(value) -> bool:
    """'10y'・'18M'・'2w' のようなテナー表記か(start列にフォワードスタートとして入力できる)。"""
    return isinstance(value, str) and _TENOR.match(value.strip()) is not None


def forward_start_date(spot: date, tenor: str) -> date:
    """スポット日+tenor(休日調整前)。ql.MakeOISのフォワードスタート(spotDate + forwardStart)と
    同じ扱いで、休日調整はスケジュール生成(Modified Following)に任せる。"""
    return (ql.Date.from_date(spot) + ql.PeriodParser.parse(tenor.strip().upper())).to_date()
