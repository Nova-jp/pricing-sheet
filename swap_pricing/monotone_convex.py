"""
Hagan & West (2006) "Interpolation Methods for Curve Construction"
(Applied Mathematical Finance, 13(2)) に基づく Monotone Convex 補間。

連続複利のインスタンテニアス・フォワードレート空間で補間を行う:
  1. pillarのDFから連続複利ゼロレート r_i = -ln(DF(t_i)) / t_i を求める
  2. pillar区間ごとの離散(区間平均)フォワード f_i を求める
  3. 各pillarでのノード値(インスタンテニアス・フォワード)f-hat_i を、
     隣接区間の離散フォワードの加重平均で求める
  4. 各区間内は、両端のノード値と区間平均(離散フォワードf_i)を満たす
     2次関数 g(x) = A(1-4x+3x^2) + B(3x^2-2x) + F(6x-6x^2) で補間する
     (面積保存: 0->1の積分がFに一致するため、ブートストラップした
     pillarのレートを厳密に再現する)

■ 実装上の簡略化(重要・要検証)
  原論文には、フォワードカーブの単調性・正値性を保証するための詳細な
  場合分け(collar)ロジックがある。ここでは「0<=A,B<=3F (F>0の場合。
  F<0なら符号反転)」という正値性の十分条件でノード値をクランプする
  簡略版を実装している。原論文の完全な場合分けとは異なるため、
  精密な検証が必要な用途では別途レビューを推奨する。
"""

import math
from typing import Sequence


class MonotoneConvexCurve:
    def __init__(self, pillar_times: Sequence[float], pillar_dfs: Sequence[float]):
        """
        pillar_times[0] は 0(評価日)、pillar_dfs[0] は 1.0 である必要がある。
        それ以降は昇順の年数(act/365等で計算済みのyear fraction)。
        """
        if len(pillar_times) != len(pillar_dfs):
            raise ValueError("pillar_times と pillar_dfs の長さが一致しません")
        if pillar_times[0] != 0.0 or pillar_dfs[0] != 1.0:
            raise ValueError("pillar_times[0]=0.0, pillar_dfs[0]=1.0 が必要です")
        if len(pillar_times) < 2:
            raise ValueError("pillarが2点(評価日+1点)以上必要です")

        self._t = list(pillar_times)
        self._df = list(pillar_dfs)
        n = len(self._t) - 1
        self._n = n

        r = [0.0] * (n + 1)
        for i in range(1, n + 1):
            r[i] = -math.log(self._df[i]) / self._t[i]

        f = [0.0] * (n + 1)
        for i in range(1, n + 1):
            prev_rt = r[i - 1] * self._t[i - 1] if i > 1 else 0.0
            f[i] = (r[i] * self._t[i] - prev_rt) / (self._t[i] - self._t[i - 1])

        fhat = [0.0] * (n + 1)
        if n == 1:
            fhat[0] = f[1]
            fhat[1] = f[1]
        else:
            for i in range(1, n):
                w_left = self._t[i] - self._t[i - 1]
                w_right = self._t[i + 1] - self._t[i]
                fhat[i] = (w_left * f[i + 1] + w_right * f[i]) / (self._t[i + 1] - self._t[i - 1])
            fhat[0] = f[1] - 0.5 * (fhat[1] - f[1])
            fhat[n] = f[n] - 0.5 * (fhat[n - 1] - f[n])

            # 正値性の簡略クランプ(0<=A,B<=3F、F<0なら符号反転)
            for i in range(1, n + 1):
                F = f[i]
                lo, hi = (0.0, 3.0 * F) if F >= 0 else (3.0 * F, 0.0)
                fhat[i - 1] = min(max(fhat[i - 1], lo), hi)
                fhat[i] = min(max(fhat[i], lo), hi)

        self._f = f
        self._fhat = fhat

    @property
    def pillar_times(self) -> list:
        return list(self._t)

    @property
    def pillar_dfs(self) -> list:
        return list(self._df)

    def _segment_index(self, t: float) -> int:
        """t <= t_i となる最小の i (1..n) を返す。"""
        for i in range(1, self._n + 1):
            if t <= self._t[i] + 1e-12:
                return i
        return self._n

    def zero_rate(self, t: float) -> float:
        """連続複利ゼロレート R(t) を返す(t>0)。"""
        if t <= 0:
            return self._fhat[0]

        last_t = self._t[self._n]
        if t >= last_t:
            integral = sum(
                self._f[j] * (self._t[j] - self._t[j - 1]) for j in range(1, self._n + 1)
            )
            integral += self._f[self._n] * (t - last_t)  # 最終区間のフォワードでフラット外挿
            return integral / t

        i = self._segment_index(t)
        integral = sum(self._f[j] * (self._t[j] - self._t[j - 1]) for j in range(1, i))

        t0, t1 = self._t[i - 1], self._t[i]
        seg_len = t1 - t0
        x = (t - t0) / seg_len
        A, B, F = self._fhat[i - 1], self._fhat[i], self._f[i]
        seg_integral = A * (x - 2 * x**2 + x**3) + B * (x**3 - x**2) + F * (3 * x**2 - 2 * x**3)
        integral += seg_len * seg_integral

        return integral / t

    def discount_factor(self, t: float) -> float:
        if t <= 0:
            return 1.0
        return math.exp(-self.zero_rate(t) * t)
