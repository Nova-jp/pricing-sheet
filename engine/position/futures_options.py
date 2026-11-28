"""
長期国債先物オプション(Raw_JGBの`債先`の行)のグリークス(PositionSheetのFutOptシート)。

会社のファイルのRaw_JGBに、現物と同じ表で先物オプションの残(枚数)と引けの評価値が入る。デルタ・ガンマは入って
いないので、基準日の引けの値で計算し、PositionシートのCloseのBondに足す(ユーザー決定、2026-11-23)。

■ 行の読み方
  Name `債先2612C127.50` = 2026年12月限の先物が原資産のコール、ストライク127.50(Pはプット)。Yesterday = 前日の残の
  枚数(符号つき、1枚 = 先物の額面1億円)。Maturity = 取引最終日。Closing Mark Pr = 引けの評価値(額面100円あたりの円)。

■ 計算(すべて基準日の引け)
  1. 先物の価格 F = DBの基準日の清算値(fut_settles、朝バッチが期近から3限月を取る)。
  2. 期間 T = 取引最終日までの営業日数 / 252(現物オプションと同じ term.option_term)。
  3. ボラ = 会社の評価値を再現するBlack式(割引なし)の価格ボラ(pricer.implied_price_vol。LiveScreenと共用)。
     大阪取引所の先物オプションはアメリカン型だが、ヨーロピアンで近似する(2026-11-23、ユーザー了承)。
  4. グリークス = 現物オプションと同じ pricer.black_greeks(先物の価格での デルタ・ガンマ・ベガ・セータ)。
  5. 金利のリスクへの換算: 先物 ≈ CTDの単価 ÷ 交換係数(CF)なので、CTDの複利1bpあたりの単価の変化 dP・2階 d2P
     (bonds.py と同じ修正デュレーション・コンベクシティ、DBの引け値)を使い、シートで
       E = 枚数 × 100 × デルタ / CF(CTD換算の額面、百万円)、G = 枚数 × 100 × ガンマ / CF²
       デルタ = E × dP / 100、ガンマ = (G × dP² + E × d2P) / 100
     とする。CTD = 基準日のネットベーシス最小の銘柄(jgb_futures.basis_table / cheapest、レポはSetupの1セル)。
     ロールの近くで2つの限月があっても、リスクはSetupで決めた1つのピラー(初期値7Y)にまとめて載せる
     (CTDの満期の区間ではなく固定のピラー。2026-11-23、ユーザー指示)。
"""

import re
from datetime import date
from typing import List, NamedTuple, Optional, Sequence

from engine.common import jgb_futures
from engine.common.option import term
from engine.common.option.bond import pricer
from engine.position.bonds import BondPosition, bond_risk

LOT_FACE = 100.0  # 1枚 = 先物の額面1億円 = 100百万円
_NAME = re.compile(r"^債先\s*(\d{2})(\d{2})\s*([CP])\s*(\d+(?:\.\d+)?)$")

HEADER = ["Contract", "Type", "Strike", "Futures", "T", "Vol (%)", "Premium", "Delta", "Gamma", "Vega", "Theta",
          "CTD", "CF", "dP", "d2P", "Message"]


class FutOption(NamedTuple):
    contract: jgb_futures.Contract
    option_type: str  # pricer.CALL / pricer.PUT
    strike: float


def is_futures_option(name) -> bool:
    return str(name or "").strip().startswith("債先")


def parse_name(name) -> FutOption:
    """`債先2612C127.50` → 2026年12月限・コール・127.50。"""
    m = _NAME.match(str(name or "").strip())
    if m is None:
        raise ValueError(f"先物オプションの銘柄名を読めません(例 債先2612C127.50): {name!r}")
    contract = jgb_futures.Contract(2000 + int(m.group(1)), int(m.group(2)))
    if contract.month not in jgb_futures.MONTH_CODES:
        raise ValueError(f"限月は3・6・9・12月です: {name!r}")
    return FutOption(contract, pricer.CALL if m.group(3) == "C" else pricer.PUT, float(m.group(4)))


class Ctd(NamedTuple):
    code: str
    conversion_factor: float
    dp: float  # 額面100円あたりの複利1bpの単価の変化(円/bp)
    d2p: float


def ctd_for(store, contract: jgb_futures.Contract, futures_price: float, base_date: date, repo: float) -> Ctd:
    """基準日の受渡適格銘柄(DBの引け値)からCTDを決め、その dP・d2P を返す。"""
    quotes = [
        jgb_futures.BondQuote(b.code, b.coupon, b.maturity, b.close_yield)
        for b in store.jgb_master(base_date)
        if b.code.upper().startswith(jgb_futures.DELIVERABLE_PREFIX) and b.close_date == base_date
    ]
    row = jgb_futures.cheapest(jgb_futures.basis_table(contract, futures_price, base_date, quotes, repo))
    if row is None:
        raise ValueError(f"{contract.label}限の受渡適格銘柄の{base_date}の引け値がDBにありません")
    r = bond_risk(store, BondPosition(row.code, 100.0), base_date)
    return Ctd(row.code, row.conversion_factor, r.dp, r.d2p)


def risk_rows(store, names: Sequence, expiries: Sequence, marks: Sequence, base_date: date,
              repo: Optional[float]) -> List[list]:
    """行ごとの HEADER の値(額面100円あたり。枚数・符号・百万円への換算はシート)。Nameが空欄の行は空欄、
    エラーの行(`債先`の形でないNameを含む)はMessageに理由。シートはRaw_JGBのHolding Typeが`OPT`の行だけを渡す。"""
    blank = [""] * len(HEADER)
    settles = None
    ctds = {}
    out: List[list] = []
    for name, expiry, mark in zip(names, expiries, marks):
        if name is None or str(name).strip() == "":
            out.append(blank)
            continue
        row = [""] * len(HEADER)
        try:
            opt = parse_name(name)
            row[0:3] = [opt.contract.label, opt.option_type, opt.strike]
            if not isinstance(expiry, date):
                raise ValueError(f"取引最終日(Maturity)が日付ではありません: {expiry!r}")
            if expiry <= base_date:
                raise ValueError(f"Expired ({expiry})")
            if settles is None:
                settles = store.fut_settles()
            price = settles.get(opt.contract.label, {}).get(base_date)
            if price is None:
                raise ValueError(f"{opt.contract.label}限の{base_date}の清算値がDBにありません(朝バッチ)")
            row[3] = price
            t = term.option_term(base_date, expiry)
            row[4] = t
            premium = float(mark)
            vol = pricer.implied_price_vol(opt.option_type, price, opt.strike, premium, t)
            g = pricer.black_greeks(opt.option_type, price, opt.strike, vol, t)
            row[5:11] = [vol, g.premium, g.delta, g.gamma, g.vega, g.theta]
            if repo is None:
                raise ValueError("レポが空です(SetupのRepo)")
            key = (opt.contract, price)
            if key not in ctds:
                ctds[key] = ctd_for(store, opt.contract, price, base_date, repo)
            ctd = ctds[key]
            row[11:15] = [ctd.code, ctd.conversion_factor, ctd.dp, ctd.d2p]
        except Exception as e:  # 行ごとにメッセージを出して他の行は続ける
            row[-1] = str(e)
        out.append(row)
    return out
