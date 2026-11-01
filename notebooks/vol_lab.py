"""ボラサーフェスの戦略ノートブック(notebooks/vol_strategies.ipynb)の部品。

2026-10-28〜29の探索(PCA×ガンマ・ボラの傾向)で使った処理を1か所にまとめたもの。ブックのUDFとは無関係で、
DB(data/historical.db)を読むだけ。価格の計算はノーマル(Bachelier)のATMストラドル。

データ:
- swaption_premiums(Traditionの引け、2022-01〜): ストラドルのプレミアム(bp)と atm_strike(=CMのFwd、%)。
- swaption_forwards(自前のカーブ、2024-10〜): 正確な年金。重なる期間で「正確な年金/フラットのFwdで近似した年金」の
  比を満期x原資産ごとに平均し、全期間のフラット近似の年金を補正する。
- σ√T(bp)= プレミアム / (2 × 年金 × φ(0))。営業日ベースのIV = σ√T / √N × √252。

時価評価(Surface.run):
- 建て日のATMストラドル(ロング、年金1単位)を、同じ契約(満期日が固定)のまま日々評価する。
- 契約のFwd: その日のCMのFwd(満期のグリッド)を満期までの営業日数で線形補間(1Mより短い所は1M・3Mの傾きで外挿)。
- 契約のIV: 総分散を「市場の時間」m = 営業日数 + θ×(暦日数−営業日数) で線形補間(1Mより短い所は1日あたり一定)。
- デルタは k 営業日ごとに取り直す。1日の増分 = ガンマ・セータ(IVを据え置いた分)+ ベガ(IVの変化の分)。
- P&Lは建てた時のベガ(営業日ベースのボラ1bpあたり)で割る → 単位は「ボラのbp」(≒ 実現ボラ − IV)。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import QuantLib as ql
from scipy.optimize import nnls
from scipy.special import ndtr

from engine.common.swaption import business_days, expiry_date

GRID = ["1M", "3M", "6M", "9M", "1Y", "2Y"]           # 満期(この中で補間する)
TENORS = ["1Y", "2Y", "3Y", "4Y", "5Y", "7Y", "10Y", "15Y", "20Y"]   # 原資産(6Y・8Y・9YはFwdが無い日が多い)
PHI0 = 1 / np.sqrt(2 * np.pi)

# 日銀の金融政策決定会合(結果の公表日)。2022-01〜2026-09
BOJ_MEETINGS = pd.to_datetime([
    "2022-01-18", "2022-03-18", "2022-04-28", "2022-06-17", "2022-07-21", "2022-09-22", "2022-10-28", "2022-12-20",
    "2023-01-18", "2023-03-10", "2023-04-28", "2023-06-16", "2023-07-28", "2023-09-22", "2023-10-31", "2023-12-19",
    "2024-01-23", "2024-03-19", "2024-04-26", "2024-06-14", "2024-07-31", "2024-09-20", "2024-10-31", "2024-12-19",
    "2025-01-24", "2025-03-19", "2025-05-01", "2025-06-17", "2025-07-31", "2025-09-19", "2025-10-30", "2025-12-19",
    "2026-01-23", "2026-03-19", "2026-04-28", "2026-06-16", "2026-07-31", "2026-09-18",
])


# ---------------------------------------------------------------- ストラドル

def straddle_value(f, K, s):
    """ノーマルモデルのストラドルの価値(年金1単位、bp)。f=Fwd(bp)、K=ストライク(bp)、s=σ√T(bp)。"""
    s = np.maximum(s, 1e-12)
    d = (f - K) / s
    return (f - K) * (2 * ndtr(d) - 1) + 2 * s * np.exp(-d * d / 2) * PHI0


def straddle_delta(f, K, s):
    return 2 * ndtr((f - K) / np.maximum(s, 1e-12)) - 1


# ---------------------------------------------------------------- データ

def default_db() -> Path:
    """worktreeでも直下でも、リポジトリの data/historical.db。"""
    return Path(__file__).resolve().parent.parent / "data" / "historical.db"


def _tenor_years(ten: str) -> int:
    return int(ten[:-1])


def _flat_annuity(F_pct: np.ndarray, ten: str) -> np.ndarray:
    """フラットのFwd(年1回払い)で近似した年金。"""
    i = np.arange(1, _tenor_years(ten) + 1)
    return (1 / (1 + F_pct[:, None] / 100) ** i[None, :]).sum(1)


def load_quotes(db_path=None) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame]]:
    """プレミアム → 日付×満期x原資産の行(F=CMのFwd(%)、stdev=σ√T(bp)、vol=営業日ベースのIV、N=営業日数、
    Ncal=暦日数、expd=満期日)。2つ目の返り値は確認用(年金の補正比、自前のIVとの差)。"""
    con = sqlite3.connect(str(db_path or default_db()))
    try:
        p = pd.read_sql("select as_of_date d, label, premium, atm_strike F from swaption_premiums", con)
        fw = pd.read_sql("select as_of_date d, label, forward, annuity, days from swaption_forwards", con)
        iv = pd.read_sql("select as_of_date d, label, vol v_own from vol_quotes", con)
    finally:
        con.close()
    p[["exp", "ten"]] = p.label.str.split("x", expand=True)
    p = p[p.exp.isin(GRID) & p.ten.isin(TENORS) & p.F.notna()].copy()
    p["Aflat"] = 0.0
    for t, g in p.groupby("ten"):
        p.loc[g.index, "Aflat"] = _flat_annuity(g.F.values, t)
    m = p.merge(fw, on=["d", "label"])
    ratio = (m.annuity / m.Aflat).groupby(m.label).agg(["mean", "std"])
    p["A"] = p.Aflat * p.label.map(ratio["mean"])
    p["stdev"] = p.premium / (2 * p.A * PHI0)
    days = {}
    for d in p.d.unique():
        td = pd.Timestamp(d).date()
        for e in GRID:
            ex = expiry_date(td, e)
            days[(d, e)] = (business_days(td, ex), (ex - td).days, ex)
    p["N"] = [days[(d, e)][0] for d, e in zip(p.d, p.exp)]
    p["Ncal"] = [days[(d, e)][1] for d, e in zip(p.d, p.exp)]
    p["expd"] = pd.to_datetime([days[(d, e)][2] for d, e in zip(p.d, p.exp)])
    p["vol"] = p.stdev / np.sqrt(p.N) * np.sqrt(252)
    p["d"] = pd.to_datetime(p.d)
    p = p.dropna(subset=["vol"])
    iv["d"] = pd.to_datetime(iv.d)
    chk = p.merge(iv, on=["d", "label"])
    return p.reset_index(drop=True), dict(annuity_ratio=ratio, vs_own=chk[["d", "label", "vol", "v_own"]])


# ---------------------------------------------------------------- 時価評価

def _interp_rows(X, Y, q, through0=False):
    """行ごとに X→Y を q で線形補間(範囲外は端の2点で外挿)。through0=True なら X[0] より手前は原点と結ぶ。"""
    i = np.clip((X < q[:, None]).sum(1), 1, X.shape[1] - 1)
    r = np.arange(len(q))
    x0, x1, y0, y1 = X[r, i - 1], X[r, i], Y[r, i - 1], Y[r, i]
    out = y0 + (y1 - y0) * (q - x0) / (x1 - x0)
    if through0:
        out = np.where(q <= X[:, 0], Y[:, 0] * q / X[:, 0], out)
    return out


class Surface:
    """日付×満期×原資産のFwd・IVと、ストラドルの時価評価。"""

    def __init__(self, quotes: pd.DataFrame):
        self.quotes = quotes
        self.D = pd.DatetimeIndex(sorted(quotes.d.unique()))
        self.nD = len(self.D)
        cal = ql.Japan()
        allb, d = [], ql.Date(1, 1, self.D[0].year - 1)
        while d <= ql.Date(31, 12, self.D[-1].year + 3):
            if cal.isBusinessDay(d):
                allb.append(pd.Timestamp(d.to_date()))
            d += 1
        self._allb = pd.DatetimeIndex(allb)
        self._DO = self._bord(self.D)                  # 営業日の通し番号
        self._DC = (self.D - self.D[0]).days.values    # 暦日の通し番号
        self.tenors = [t for t in TENORS if (quotes.ten == t).any()]
        self.A: Dict[str, dict] = {}
        for t in self.tenors:
            g = quotes[quotes.ten == t]
            pv = lambda c: g.pivot(index="d", columns="exp", values=c).reindex(self.D).reindex(columns=GRID)
            ex = pd.DatetimeIndex(pv("expd").values.ravel())
            shape = (self.nD, len(GRID))
            self.A[t] = dict(
                F=pv("F").values * 100, W=pv("stdev").values ** 2, N=pv("N").values.astype(float),
                NC=pv("Ncal").values.astype(float), vol=pv("vol").values,
                XO=np.where(ex.isna(), 0, self._bord(ex.fillna(self.D[0]))).reshape(shape),
                XC=np.where(ex.isna(), 0, (ex.fillna(self.D[0]) - self.D[0]).days.values).reshape(shape))
        self._cache: Dict[tuple, tuple] = {}

    def _bord(self, x) -> np.ndarray:
        """x以上の最初の営業日の通し番号。"""
        return self._allb.searchsorted(pd.DatetimeIndex(x))

    # 表示用の表
    def iv(self, exp: str) -> pd.DataFrame:
        """日付×原資産の営業日ベースIV(bp)。"""
        ie = GRID.index(exp)
        return pd.DataFrame({t: self.A[t]["vol"][:, ie] for t in self.tenors}, self.D)

    def fwd(self, exp: str) -> pd.DataFrame:
        """日付×原資産のCMのFwd(bp)。"""
        ie = GRID.index(exp)
        return pd.DataFrame({t: self.A[t]["F"][:, ie] for t in self.tenors}, self.D)

    def surface_on(self, date) -> pd.DataFrame:
        """その日のIVサーフェス(満期×原資産)。"""
        q = self.quotes[self.quotes.d == pd.Timestamp(date)]
        return q.pivot(index="exp", columns="ten", values="vol").reindex(index=GRID, columns=self.tenors)

    def realized_vol(self, ten: str, window: int = 20) -> pd.Series:
        """1MのCMのFwdの日次変化の過去 window 日の実現ボラ(bp/年)。"""
        return self.fwd("1M")[ten].diff().rolling(window).std() * np.sqrt(252)

    def run(self, e: str, t: str, h: int = 999, k: int = 1, theta: float = 0.0):
        """全建て日のロング・ATMストラドル(満期 e、原資産 t)を h 営業日保有(満期が先ならそこまで)、デルタは k 日ごと。

        返り値 (R, daily):
        - R: 建て日ごとの表。x=P&L/ベガ(ボラbp)、gth=うちガンマ・セータ、vg=うちIVの変化、iv=建てた時のIV、n=保有日数
        - daily: 建て日×経過日の行列(inc=増分、gt=ガンマ・セータ、vg=ベガ、F=契約のFwd、S=σ√T)。いずれもベガ0で割った値。
          daily["inc"][i0, st] は日付 D[i0+st+1] の引けまでの1日の増分。
        """
        key = (e, t, h, k, theta)
        if key in self._cache:
            return self._cache[key]
        a, nD, D = self.A[t], self.nD, self.D
        ie = GRID.index(e)
        i0 = np.arange(nD)
        ok0 = ~np.isnan(a["F"][:, ie])
        XO, XC = a["XO"][:, ie], a["XC"][:, ie]
        N0 = (XO - self._DO).astype(float)
        K = a["F"][:, ie].copy()
        s = np.sqrt(a["W"][:, ie])
        f = K.copy()
        mgrid = a["N"] + theta * (a["NC"] - a["N"])
        steps = int(min(h, np.nanmax(np.where(ok0, N0, 0))))
        mats = {c: np.full((nD, steps), np.nan) for c in ("inc", "gt", "vg", "F", "S")}
        gt, vg, n = np.zeros(nD), np.zeros(nD), np.zeros(nD, int)
        alive, dl = ok0.copy(), np.zeros(nD)
        for st in range(steps):
            j, jn = i0 + st, i0 + st + 1
            alive &= (jn < nD) & (st < h)
            jj, jnn = np.minimum(j, nD - 1), np.minimum(jn, nD - 1)
            Nj = XO - self._DO[jj]
            alive &= Nj > 0
            if not alive.any():
                break
            Nn = (XO - self._DO[jnn]).astype(float)
            Cn = (XC - self._DC[jnn]).astype(float)
            Cj = (XC - self._DC[jj]).astype(float)
            mj, mn = Nj + theta * (Cj - Nj), Nn + theta * (Cn - Nn)
            if st % k == 0:
                dl = np.where(alive, straddle_delta(f, K, s), dl)
            fn = _interp_rows(a["N"][jnn], a["F"][jnn], np.maximum(Nn, 0.0))
            expired = Nn <= 0
            s_old = np.where(expired, 0.0, s * np.sqrt(np.maximum(mn, 0) / np.maximum(mj, 1e-9)))
            s_new = np.where(expired, 0.0, np.sqrt(np.maximum(
                _interp_rows(mgrid[jnn], a["W"][jnn], np.maximum(mn, 1e-9), True), 0)))
            g1 = straddle_value(fn, K, s_old) - straddle_value(f, K, s) - dl * (fn - f)
            v1 = straddle_value(fn, K, s_new) - straddle_value(fn, K, s_old)
            g1, v1 = np.where(alive, g1, 0.0), np.where(alive, v1, 0.0)
            for c, val in (("inc", g1 + v1), ("gt", g1), ("vg", v1), ("F", fn), ("S", s_new)):
                mats[c][alive, st] = val[alive]
            gt += g1
            vg += v1
            n += alive
            f, s = np.where(alive, fn, f), np.where(alive, s_new, s)
            alive &= ~expired
        full = ok0 & (n >= np.minimum(h, N0))
        v0 = np.sqrt(np.maximum(N0, 1) / 252) * 2 * PHI0
        R = pd.DataFrame(dict(
            t0=D, exp=e, ten=t, h=h, k=k, iv=a["vol"][:, ie], N0=N0, K=K,
            x=np.nansum(mats["inc"], 1) / v0, gth=gt / v0, vg=vg / v0, n=n))[full].set_index("t0")
        for c in ("inc", "gt", "vg"):
            mats[c] = mats[c] / v0[:, None]
        mats["full"] = full
        self._cache[key] = (R, mats)
        return R, mats

    def trade_path(self, e: str, t: str, t0, h: int = 999, k: int = 1, theta: float = 0.0, sign: float = 1.0) -> pd.DataFrame:
        """1本の建て(t0)の日々の動き。sign=-1 でショート。"""
        R, M = self.run(e, t, h, k, theta)
        i0 = self.D.get_loc(pd.Timestamp(t0))
        ok = ~np.isnan(M["inc"][i0])
        idx = self.D[i0 + 1 + np.nonzero(ok)[0]]
        out = pd.DataFrame({
            "Fwd(bp)": M["F"][i0][ok], "σ√T(bp)": M["S"][i0][ok],
            "ガンマ・セータ": sign * M["gt"][i0][ok], "IVの変化": sign * M["vg"][i0][ok]}, idx)
        out["1日のP&L"] = out["ガンマ・セータ"] + out["IVの変化"]
        out["累積P&L"] = out["1日のP&L"].cumsum()
        return out

    def ladder(self, weights: Dict[tuple, pd.Series]) -> pd.Series:
        """毎日1/21ずつ建てる運用の日次P&L。weights = {(e, t, h, k, theta): 建て日ごとの量(ロング=+、ベガ1単位)}。"""
        out = np.zeros(self.nD)
        for key, w in weights.items():
            _, M = self.run(*key)
            inc = M["inc"]
            wv = w.reindex(self.D).fillna(0.0).values
            for st in range(inc.shape[1]):
                col = inc[:, st]
                ok = ~np.isnan(col) & (wv != 0)
                j = np.nonzero(ok)[0] + st + 1
                np.add.at(out, j[j < self.nD], (wv[ok] * col[ok])[j < self.nD])
        return pd.Series(out / 21, self.D)


# ---------------------------------------------------------------- 統計

def nw_se(x: np.ndarray, lag: int) -> float:
    """Newey-Westの平均の標準誤差(重なる保有期間の自己相関を補正)。"""
    x = np.asarray(x, float) - np.mean(x)
    n = len(x)
    v = x @ x / n
    for k in range(1, lag + 1):
        v += 2 * (1 - k / (lag + 1)) * (x[k:] @ x[:-k]) / n
    return np.sqrt(v / n)


def stat(s: pd.Series, lag: int = 21) -> pd.Series:
    """平均・NWのt値・勝率。"""
    s = pd.Series(s).dropna()
    se = nw_se(s.values, lag) if len(s) > lag else np.nan
    return pd.Series(dict(n=len(s), mean=s.mean(), t=s.mean() / se if se and se > 0 else np.nan,
                          hit=(s > 0).mean(), sd=s.std()))


def ols_nw(X: np.ndarray, y: np.ndarray, lag: int = 21) -> Tuple[np.ndarray, np.ndarray]:
    """定数項付きOLSの係数とNWのt値。"""
    X = np.c_[np.ones(len(y)), X]
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    e = y - X @ b
    n = len(y)
    XtXi = np.linalg.inv(X.T @ X)
    S0 = (X * e[:, None]).T @ (X * e[:, None]) / n
    for k in range(1, lag + 1):
        G = (X[k:] * e[k:, None]).T @ (X[:-k] * e[:-k, None]) / n
        S0 += (1 - k / (lag + 1)) * (G + G.T)
    se = np.sqrt(np.diag(n * XtXi @ S0 @ XtXi))
    return b, b / se


def by_period(s: pd.Series, periods: Sequence[Tuple[str, str, str]], lag: int = 21) -> pd.DataFrame:
    """期間ごとの平均・t値・勝率。periods = [(名前, 開始, 終了), ...]。"""
    rows = {}
    for lab, a, b in periods:
        g = s[(s.index >= a) & (s.index <= b)]
        if len(g) > 5:
            rows[lab] = stat(g, lag)
    return pd.DataFrame(rows).T


def ladder_summary(s: pd.Series, unit: float = 1e6) -> pd.Series:
    """日次P&L(ベガ1単位=unit 円/ボラbp で換算)の要約。単位は百万円。"""
    s = s * unit / 1e6
    cum = s.cumsum()
    return pd.Series({
        "年率P&L": s.mean() * 252, "年率の標準偏差": s.std() * np.sqrt(252),
        "シャープ": s.mean() / s.std() * np.sqrt(252) if s.std() > 0 else np.nan,
        "最大DD": (cum - cum.cummax()).min(), "最悪の日": s.min(), "最悪の21日": s.rolling(21).sum().min(),
        "負けた月の割合": (s.groupby(s.index.to_period("M")).sum() < 0).mean()})


# ---------------------------------------------------------------- PCA(③ PC2)

def rolling_pca(S: Surface, window: int = 120, tenors: Sequence[str] = TENORS) -> Dict[pd.Timestamp, dict]:
    """1MのCMのFwdの日次変化で過去 window 日のPCA(年率bp)。インプライドの因子分散は、各原資産の1MのIV²を
    Σ_k imp_k × u_k² に非負最小二乗で当てはめたもの。
    返り値 {日付: dict(u=(u1,u2,u3), lam=実現の固有値, imp=インプライドの因子分散, L1=u1√λ1)}"""
    tenors = [t for t in tenors if t in S.tenors]
    dF = S.fwd("1M")[tenors].diff()
    IV1 = S.iv("1M")[tenors]
    out = {}
    for i in range(window, S.nD):
        X = dF.iloc[i - window + 1:i + 1].values
        if np.isnan(X).any() or np.isnan(IV1.iloc[i].values).any():
            continue
        w, v = np.linalg.eigh(np.cov(X.T) * 252)
        o = w.argsort()[::-1]
        w, v = w[o], v[:, o]
        v = v * np.sign(v[:, 0].sum())     # PC1は全年限が同じ向き(+)
        if v[-1, 1] > 0:                   # PC2は長い方がマイナス
            v[:, 1] *= -1
        u = tuple(pd.Series(v[:, j], tenors) for j in range(3))
        imp, _ = nnls(np.c_[v[:, 0] ** 2, v[:, 1] ** 2, v[:, 2] ** 2], IV1.iloc[i].values ** 2)
        out[S.D[i]] = dict(u=u, lam=w[:3], imp=imp, L1=u[0] * np.sqrt(w[0]))
    return out


def pca_richness(pc: Dict[pd.Timestamp, dict]) -> pd.DataFrame:
    """因子ごとの割高さ = √(インプライドの分散 / 過去の実現の分散)。ratio = PC2の割高さ / PC1の割高さ。"""
    df = pd.DataFrame({d: dict(PC1=np.sqrt(v["imp"][0] / v["lam"][0]), PC2=np.sqrt(v["imp"][1] / v["lam"][1]))
                       for d, v in pc.items()}).T
    df["ratio"] = df.PC2 / df.PC1
    return df


def pc2_legs(pc_day: dict, iv1: pd.Series, belly: str = "7Y", wings: Sequence[str] = ("2Y", "20Y"),
             hedge: Optional[Tuple[str, int]] = ("1Y", 21), long_belly: bool = True, mode: str = "pc1g"):
    """PC2の取引の脚(真ん中の1Mのベガ=1)。[((満期, 保有, 原資産), ベガの量), ...]。
    mode="pc1g": 両端の量をPC1のガンマ(∝ ベガ × 負荷² / IV)が0になるように決める。mode="vega": 両端をベガで等量。
    hedge: ネットのベガを (満期, 保有) の真ん中の原資産で0にする(IVの水準の変化を消す)。"""
    L1 = pc_day["L1"]
    g = {t: L1[t] ** 2 / iv1[t] for t in (belly, *wings)}
    s = 1.0 if long_belly else -1.0
    q = [(("1M", 999, belly), s)]
    for w in wings:
        q.append((("1M", 999, w), -s * (g[belly] / g[w] if mode == "pc1g" else 1.0) / len(wings)))
    if hedge:
        q.append(((hedge[0], hedge[1], belly), -sum(v for _, v in q)))
    return q


def pc2_book(S: Surface, pc: Dict[pd.Timestamp, dict], entry_ok=lambda d: True, **legs_kw):
    """毎日建てるPC2の取引。返り値 (建て日ごとの表 x/gth/vg, ladderの日次P&L, 建て日ごとの脚の量)。"""
    IV1 = S.iv("1M")
    W: Dict[tuple, Dict] = {}
    for d, v in pc.items():
        if not entry_ok(d):
            continue
        for (e, h, t), q in pc2_legs(v, IV1.loc[d], **legs_kw):
            W.setdefault((e, t, h, 1, 0.0), {})[d] = q
    W = {k: pd.Series(v) for k, v in W.items()}
    idx = sorted(set().union(*[w.index for w in W.values()]))
    tot = pd.DataFrame(0.0, index=pd.DatetimeIndex(idx), columns=["x", "gth", "vg"])
    valid = pd.Series(True, tot.index)
    for key, w in W.items():
        R, _ = S.run(*key)
        part = R[["x", "gth", "vg"]].reindex(tot.index)
        valid &= part.x.notna() | (w.reindex(tot.index).fillna(0) == 0)
        tot += part.fillna(0).mul(w.reindex(tot.index).fillna(0), axis=0)
    tot = tot[valid]
    W = {k: w.reindex(tot.index).dropna() for k, w in W.items()}
    return tot, S.ladder(W), W


# ---------------------------------------------------------------- 夜の種類(④・⑤)

def night_types(S: Surface, meetings: Iterable = BOJ_MEETINGS, around: int = 1) -> pd.Series:
    """各日付(前の営業日の引け→その日の引け)の夜の種類。会合日の前後 around 日は「会合の前後」。"""
    D = S.D
    wd = np.array([len(pd.bdate_range(D[i - 1], D[i] - pd.Timedelta(days=1))) if i else 1 for i in range(S.nD)])
    typ = np.where(wd >= 2, "祝日明け", np.where(D.dayofweek == 0, "週末明け(金→月)", "平日の夜"))
    near = np.zeros(S.nD, bool)
    for m in pd.DatetimeIndex(meetings):
        if m in D:
            i = D.get_loc(m)
            near[max(0, i - around):i + around + 1] = True
    typ = np.where(near, "会合の前後", typ)
    return pd.Series(typ, D)


def meeting_trades(S: Surface, ten: str, before: int, after: int, meetings: Iterable = BOJ_MEETINGS):
    """会合の before 日前の引けに1Mを売り、after 日後の引けに買い戻す(毎日デルタヘッジ)。
    返り値 (会合ごとのショートのP&L表, 会合を含まない同じ長さの窓のショートのP&L)。"""
    D = S.D
    h = before + after
    R, _ = S.run("1M", ten, h)
    ms = [m for m in pd.DatetimeIndex(meetings) if m in D and D.get_loc(m) - before >= 0]
    rows = {}
    for m in ms:
        t0 = D[D.get_loc(m) - before]
        if t0 in R.index:
            r = R.loc[t0]
            rows[m] = dict(建て日=t0, P_L=-r.x, ガンマ_セータ=-r.gth, IVの変化=-r.vg, IV=r.iv)
    X = pd.DataFrame(rows).T
    near = np.zeros(S.nD, bool)
    for m in ms:
        i = D.get_loc(m)
        near[max(0, i - 1):i + 2] = True
    ii = np.array([D.get_loc(d) for d in R.index])
    clean = np.array([not near[i:i + h + 2].any() for i in ii])
    return X.astype({c: float for c in ["P_L", "ガンマ_セータ", "IVの変化", "IV"]}), -R.x[clean]
