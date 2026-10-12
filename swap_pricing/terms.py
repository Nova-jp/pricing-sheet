"""
スワップの取引条件の型。性質の違う3段に分ける(内側から順に入れ子になる)。

    SwapConvention(規約)     … fix freq / fix dcf / float freq / roll conv
      ↑ 含む
    SwapTerms(取引条件)      … 規約 + start + tenor/end(=1本の具体的なスワップ。パーレートの計算単位)
      ↑ 含む
    SwapParams(ポジション)   … 取引条件 + 想定元本 + PAY/REC + 固定金利(PV・デルタの計算単位)

■ 分ける理由
  - 規約は多くのスワップで共有する(標準の STANDARD 等)。日付だけが違うスワップを、
    同じ規約から組める(ヒストリカルは規約を固定したまま日付ごとに SwapTerms を作る)。
  - 確認のタイミングが違う。規約の値は入力を受け取った時点で確かめられる(作るときに検査し、
    未対応の値はここでエラーにする)。満期済みかどうか等は評価日が決まってから
    (swap_pricer._prepare_swap)。
  - 関数に8個の引数を並べて渡すと取り違えが起きやすいため、1つの値にまとめて渡す。

■ float dcf を持たない理由
  TONAのOISでは、変動(overnight)脚の日数計算はQuantLibが指標(ql.Tonar)の日数計算に固定する
  仕様で、入力しても計算に使われない。使われない入力は持たない(2026-10-12 ユーザー判断で
  Excelの列からも削除)。

日付に決まる前の入力(start="10y"・end="M27"等)は、ヒストリカルの雛形
historical.pricer.SwapSpec が持ち、日付ごとに SwapTerms に直す。
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from swap_pricing.conventions import (
    ACT_365_FIXED, FREQ_TO_QL_FREQUENCY, ROLL_STD, SUPPORTED_DCF, SUPPORTED_ROLL,
)

PAY = "PAY"
REC = "REC"


@dataclass(frozen=True)
class SwapConvention:
    """スワップの規約(どういうルールで組むか)。作るときに値を確かめる。"""

    fix_freq: str
    fix_dcf: str
    float_freq: str
    roll_conv: str

    def __post_init__(self) -> None:
        for name in ("fix_freq", "float_freq"):
            if getattr(self, name) not in FREQ_TO_QL_FREQUENCY:
                raise ValueError(
                    f"未対応の{name}です: {getattr(self, name)!r} (対応: {tuple(FREQ_TO_QL_FREQUENCY)})"
                )
        if self.fix_dcf not in SUPPORTED_DCF:
            raise ValueError(f"未対応のdcfコンベンションです: {self.fix_dcf!r} (対応: {SUPPORTED_DCF})")
        if self.roll_conv not in SUPPORTED_ROLL:
            raise ValueError(f"未対応のroll conventionです: {self.roll_conv!r} (対応: {SUPPORTED_ROLL})")


# 標準の規約(RealTimeのスポットのOISと同じ。カーブの検証・BOJシート等で使う)
STANDARD = SwapConvention(fix_freq="PA", fix_dcf=ACT_365_FIXED, float_freq="PA", roll_conv=ROLL_STD)


@dataclass(frozen=True)
class SwapTerms:
    """1本の具体的なスワップ(起算日と期間が決まったもの)。tenor と end はどちらか一方だけ。

    start は休日調整前の起算日(QuantLibのスケジュール生成が調整する)。tenor は '5y' 等の表記。
    """

    start: date
    convention: SwapConvention
    tenor: Optional[str] = None
    end: Optional[date] = None

    def __post_init__(self) -> None:
        if (self.tenor is None) == (self.end is None):
            raise ValueError("tenor と end のどちらか一方だけを指定してください")


@dataclass(frozen=True)
class SwapParams:
    """ポジション(取引条件+想定元本・PAY/REC・固定金利)。PV・デルタの計算に使う。

    fix_rate: 固定金利(%)。None なら市場のパーレートで組む(risk.resolve_params で固定する)。
    TONA実績は市場データなのでここには持たない(market_data.Market.fixings)。
    """

    terms: SwapTerms
    notional: float
    pay_rec: str
    fix_rate: Optional[float] = None

    def __post_init__(self) -> None:
        if self.pay_rec not in (PAY, REC):
            raise ValueError(f"pay_recは'{PAY}'または'{REC}'である必要があります: {self.pay_rec!r}")
