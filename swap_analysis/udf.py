"""
分析ブック(SwapAnalysis.xlsm)のUDF。Excelとswap_analysisの唯一の接点。

■ 設計方針(PricingSheet.xlsmと同じ)
  - VBAは使わない。Excelの関数で難しい計算(固有値分解・DBの読み出し)だけをUDFにし、
    得点・復元(行列の積)・差分・寄与率などはExcelの数式で計算する。
  - 計算方法は「手動」+F9。UDFは非volatile。TODAY()は引数に使わない(比較日の空欄=今日)。
  - 配列はそのまま返し、Excelの動的配列のスピルに任せる(@xw.ret(expand=...)は使わない)。
  - 主成分の推定は引数とDBの更新時刻ごとにプロセス内で覚えておき、
    PcaModel/PcaEigen/PcaWindowで1回の計算を共有する。

■ 引数(共通)
  labels: 使う銘柄の縦の並び(スワップはテナー、ボラは「満期x原資産」)。並び順が表の行順。
  pca_mode: 「レベル」/「日次変化」。standardize: 「あり」(z-score)/「なし」(分散共分散)。
  window: サンプル数(営業日)。base_date: 推定基準日(windowの最終日。空欄=比較日より前の直近)。
  target_date: 比較日(空欄=今日、実績はRealTimeのライブの値。日付=DBのその日の引け値)。
  dataset: "swap"(既定)/"vol"。
"""

from functools import lru_cache
from typing import List, Optional, Tuple

import xlwings as xw

from swap_analysis import pca
from swap_analysis.history import UNIT_BP, load_history, normalize_label, parse_dataset
from swap_pricing.curve_store import CurveStore
from swap_pricing.udf import _is_blank, _to_date, _today


def _labels(values) -> Tuple[str, ...]:
    return tuple(str(v).strip() for v in (values or []) if not _is_blank(v))


def _window(value) -> int:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"windowは営業日数(整数)です: {value!r}")
    if n != int(n):
        raise ValueError(f"windowは営業日数(整数)です: {value!r}")
    return int(n)


def _db_key() -> Optional[int]:
    store = CurveStore()
    return store.db_path.stat().st_mtime_ns if store.exists() else None


@lru_cache(maxsize=16)
def _estimate_cached(dataset, labels, mode, standardize, window, until, db_key) -> pca.PcaModel:
    return pca.estimate(
        load_history(dataset), labels, mode, standardize, window, until, UNIT_BP[dataset],
    )


def _model(labels, pca_mode, standardize, window, base_date, target_date, dataset):
    """→ (推定結果, 比較日, 推定基準日が空欄か)"""
    dataset = parse_dataset(dataset)
    target = _to_date(target_date) or _today()
    base = _to_date(base_date)
    model = _estimate_cached(
        dataset, _labels(labels), pca.parse_mode(pca_mode), pca.parse_standardize(standardize),
        _window(window), base or pca.default_until(target), _db_key(),
    )
    return model, target, base is None


@xw.func
@xw.arg("labels", ndim=1)
def PcaModel(labels, pca_mode, standardize, window, base_date=None, target_date=None,
             dataset="swap") -> List[list]:
    """
    平均・スケールと主成分ベクトル。見出し行 [Label, Mean, Scale, PC1, …, PCm] + 銘柄ごとの行。
    Mean: windowのサンプルの平均 μ(値の単位。日次変化モードは変化の平均)。
    Scale: 標準化ありは標準偏差 σ、なしは1。
    PCj: 第j主成分の単位ベクトル(全銘柄数m本。Excel側でTAKEしてk本使う)。
    """
    model, _, _ = _model(labels, pca_mode, standardize, window, base_date, target_date, dataset)
    m = len(model.labels)
    header = ["Label", "Mean", "Scale"] + [f"PC{j + 1}" for j in range(m)]
    return [header] + [
        [label, float(model.mean[i]), float(model.scale[i])] + [float(v) for v in model.vectors[i]]
        for i, label in enumerate(_labels(labels))
    ]


@xw.func
@xw.arg("labels", ndim=1)
def PcaEigen(labels, pca_mode, standardize, window, base_date=None, target_date=None,
             dataset="swap") -> List[list]:
    """固有値(降順。標準化なしはbp²、ありは単位なし)。見出し行 [PC, Eigenvalue] + 主成分ごとの行。
    寄与率・累積寄与率はExcelの数式で計算する。引数はPcaModelと同じ。"""
    model, _, _ = _model(labels, pca_mode, standardize, window, base_date, target_date, dataset)
    return [["PC", "Eigenvalue"]] + [
        [f"PC{j + 1}", float(v)] for j, v in enumerate(model.eigenvalues)
    ]


@xw.func
@xw.arg("labels", ndim=1)
def PcaWindow(labels, pca_mode, standardize, window, base_date=None, target_date=None,
              dataset="swap") -> List[list]:
    """推定に使ったwindowの情報(項目/値の2列)。引数はPcaModelと同じ。"""
    model, target, auto = _model(labels, pca_mode, standardize, window, base_date, target_date, dataset)
    warning = pca.gap_warning(model.end, target, "推定終了日") if auto else ""
    return [
        ["推定開始日", model.start],
        ["推定終了日", model.end],
        ["サンプル数", model.n_samples],
        ["除外した営業日数", model.excluded_days],
        ["警告", warning],
    ]


def _observe(labels, pca_mode, target_date, real_time_range, dataset):
    dataset = parse_dataset(dataset)
    history = load_history(dataset)
    as_of = _to_date(target_date)
    if as_of is None:
        as_of, source, level = _today(), "RealTime", {}
        for r in real_time_range or []:
            if r is None or len(r) < 2 or _is_blank(r[0]):
                continue
            try:
                level[normalize_label(r[0])] = float(r[-1])
            except (TypeError, ValueError):
                pass  # 空欄・エラー値は欠損
    else:
        source, level = f"DB({as_of})", history.get(as_of)
        if level is None:
            raise ValueError(f"{as_of}の値がDBにありません(休日か、取り込み期間外)")
    obs = pca.observe(history, _labels(labels), pca.parse_mode(pca_mode), as_of, level, source)
    return obs, as_of, source


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PcaActual(labels, pca_mode, target_date=None, real_time_range=None, dataset="swap") -> List[list]:
    """
    比較日の実績(銘柄の並びの縦1列、値の単位)。レベルは水準、日次変化は
    「比較日の水準 − DBの前営業日(なければ直近の日)の水準」。

    target_date が空欄ならRealTimeのライブの値(real_time_range: 1列目=銘柄、最後の列=値)、
    日付ならDBのその日の引け値(スワップのO/Nはその日のTONA実績)。選択銘柄が1本でも
    欠けていればエラーにする(欠けた銘柄を別の値で代用しない)。
    """
    obs, _, _ = _observe(labels, pca_mode, target_date, real_time_range, dataset)
    return [[float(v)] for v in obs.values]


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PcaActualInfo(labels, pca_mode, target_date=None, real_time_range=None,
                  dataset="swap") -> List[list]:
    """比較日の実績の情報(項目/値の2列)。引数はPcaActualと同じ。"""
    obs, as_of, source = _observe(labels, pca_mode, target_date, real_time_range, dataset)
    return [
        ["比較日", as_of],
        ["実績", source],
        ["変化の基準日", obs.base_date or ""],
        ["警告", obs.warning],
    ]
