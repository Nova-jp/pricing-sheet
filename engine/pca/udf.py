"""
分析ブック(SwapAnalysis.xlsm)のUDF。Excelとswap_analysisの唯一の接点。

■ 設計方針(PricingSheet.xlsmと同じ)
  - VBAは使わない。Excelの関数で難しい計算(固有値分解・DBの読み出し)だけをUDFにし、
    得点・復元(行列の積)・差分・寄与率などはExcelの数式で計算する。
  - 計算方法は「手動」+F9。UDFは非volatile。TODAY()は引数に使わない(推定の最終日・復元日の空欄=今日)。
  - 配列はそのまま返し、Excelの動的配列のスピルに任せる(@xw.ret(expand=...)は使わない)。
  - 主成分の推定は引数とDBの更新時刻ごとにプロセス内で覚えておき、
    PcaModel/PcaEigen/PcaWindow/PcaGridで1回の計算を共有する。
  - Excelとの往復はUDFのセル1つごとにかかるので、複数の条件をまとめて見るシート(PCAGrid)は
    ケースごとにUDFを呼ばず、PcaGridの1回で全ケースの推定結果を返す。

■ 引数(共通)
  labels: 使う銘柄の縦の並び(スワップはテナー、ボラは「満期x原資産」)。並び順が表の行順。
  pca_mode: 「Level」/「Change」(「レベル」/「日次変化」も可)。
  standardize: 「Yes」(z-score)/「No」(分散共分散)(「あり」/「なし」も可)。
  window: サンプル数(営業日)。window_end: 推定の最終日(Window end。空欄=今日。DBの引け値だけを使うので、
    DBに今日の値がなければ直近の営業日まで)。推定(PcaModel/PcaEigen/PcaWindow/PcaResidStd/PcaGrid)は復元日によらない。
  recon_date: 復元日(Recon date。PcaActual/PcaActualInfo。空欄=今日、実績はRealTimeのライブの値。
    日付=DBのその日の引け値)。
  dataset: "swap"(既定)/"vol"。
  sigma_days: 差分のσ(PcaResidStd・PcaGridResidStd)をとるサンプル数(推定終了日からさかのぼる営業日。
    空欄=推定のwindowと同じ。windowより大きくすれば推定より前の日も入る)。
"""

from functools import lru_cache
from typing import List, Optional, Tuple

import xlwings as xw

from engine.pca import pca
from engine.pca.history import UNIT_BP, load_history, normalize_label, parse_dataset
from engine.common.historical_store import HistoricalStore
from engine.common.udf_common import _is_blank, _to_date, _today


def _labels(values) -> Tuple[str, ...]:
    return tuple(str(v).strip() for v in (values or []) if not _is_blank(v))


def _count(value, what: str) -> int:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what}は整数です: {value!r}")
    if n != int(n):
        raise ValueError(f"{what}は整数です: {value!r}")
    return int(n)


def _window(value) -> int:
    return _count(value, "window(営業日数)")


def _sigma_days(value) -> Optional[int]:
    return None if _is_blank(value) else _count(value, "σの日数(営業日数)")


def _db_key() -> Optional[int]:
    store = HistoricalStore()
    return store.db_path.stat().st_mtime_ns if store.exists() else None


@lru_cache(maxsize=16)
def _estimate_cached(dataset, labels, mode, standardize, window, until, db_key) -> pca.PcaModel:
    return pca.estimate(
        load_history(dataset), labels, mode, standardize, window, until, UNIT_BP[dataset],
    )


@lru_cache(maxsize=128)
def _resid_std_cached(dataset, labels, mode, standardize, window, until, db_key, k, n_days):
    model = _estimate_cached(dataset, labels, mode, standardize, window, until, db_key)
    return tuple(pca.residual_std(load_history(dataset), model, k, n_days, UNIT_BP[dataset]))


def _window_end(value):
    """推定の最終日(空欄=今日)。"""
    return _to_date(value) or _today()


def _model_key(labels, pca_mode, standardize, window, window_end, dataset) -> tuple:
    """_estimate_cached の引数(推定の最終日の空欄は今日にする)。"""
    return (
        parse_dataset(dataset), _labels(labels), pca.parse_mode(pca_mode),
        pca.parse_standardize(standardize), _window(window), _window_end(window_end), _db_key(),
    )


def _model(labels, pca_mode, standardize, window, window_end, dataset) -> pca.PcaModel:
    return _estimate_cached(*_model_key(labels, pca_mode, standardize, window, window_end, dataset))


def _model_row(model: pca.PcaModel, i: int, label: str) -> list:
    """PcaModel・PcaGridの銘柄ごとの行 [銘柄, Mean, Scale, Std, PC1, …]。"""
    return [label, float(model.mean[i]), float(model.scale[i]), float(model.std[i])] + [
        float(v) for v in model.vectors[i]
    ]


@xw.func
@xw.arg("labels", ndim=1)
def PcaModel(labels, pca_mode, standardize, window, window_end=None, dataset="swap") -> List[list]:
    """
    平均・スケールと主成分ベクトル。見出し行 [Label, Mean, Scale, Std, PC1, …, PCm] + 銘柄ごとの行。
    Mean: windowのサンプルの平均 μ(値の単位。日次変化モードは変化の平均)。
    Scale: 標準化ありは標準偏差 σ、なしは1。
    Std: windowのサンプルの標準偏差(値の単位。標準化の有無によらない。グラフの平均±1σ用)。
    PCj: 第j主成分の単位ベクトル(全銘柄数m本。Excel側でTAKEしてk本使う)。
    """
    model = _model(labels, pca_mode, standardize, window, window_end, dataset)
    m = len(model.labels)
    header = ["Label", "Mean", "Scale", "Std"] + [f"PC{j + 1}" for j in range(m)]
    return [header] + [
        _model_row(model, i, label) for i, label in enumerate(_labels(labels))
    ]


@xw.func
@xw.arg("labels", ndim=1)
def PcaResidStd(labels, pca_mode, standardize, window, k, window_end=None, sigma_days=None,
                dataset="swap") -> List[list]:
    """
    差分(実績 − k本での復元)の標準偏差(bp、銘柄の並びの縦1列。差分のグラフの±1σ)。
    主成分ベクトル・平均・スケールは推定のまま固定し、推定終了日からさかのぼった sigma_days 個の
    サンプル(空欄=windowと同じ数)の各日を k 本で復元した差分から計算する。引数はPcaModel+k・sigma_days。
    """
    values = _resid_std_cached(
        *_model_key(labels, pca_mode, standardize, window, window_end, dataset),
        _count(k, "主成分数k"), _sigma_days(sigma_days),
    )
    return [[float(v)] for v in values]


@xw.func
@xw.arg("labels", ndim=1)
def PcaEigen(labels, pca_mode, standardize, window, window_end=None, dataset="swap") -> List[list]:
    """固有値(降順。標準化なしはbp²、ありは単位なし)。見出し行 [PC, Eigenvalue] + 主成分ごとの行。
    寄与率・累積寄与率はExcelの数式で計算する。引数はPcaModelと同じ。"""
    model = _model(labels, pca_mode, standardize, window, window_end, dataset)
    return [["PC", "Eigenvalue"]] + [
        [f"PC{j + 1}", float(v)] for j, v in enumerate(model.eigenvalues)
    ]


@xw.func
@xw.arg("labels", ndim=1)
def PcaWindow(labels, pca_mode, standardize, window, window_end=None, dataset="swap") -> List[list]:
    """推定に使ったwindowの情報(項目/値の2列。項目名は英語: 開始日・終了日・サンプル数・除外した営業日数・警告)。
    End は推定に使った最後の日(推定の最終日以前で銘柄がそろった直近の日)。引数はPcaModelと同じ。"""
    model = _model(labels, pca_mode, standardize, window, window_end, dataset)
    warning = pca.gap_warning(model.end, _window_end(window_end))
    return [
        ["Start", model.start],
        ["End", model.end],
        ["Samples", model.n_samples],
        ["Excluded days", model.excluded_days],
        ["Warning", warning],
    ]


GRID_HEADER = [
    "Case", "Start", "End", "Samples", "Excluded", "Warning", "Label", "Mean", "Scale", "Std",
]


def _grid_cases(names, settings, dataset, today):
    """PcaGrid/PcaGridResidStdのケース: windowが空欄でない列ごとに (ケース番号, 推定のキー or エラー, 推定の最終日)。"""
    for case, (window_end, window, pca_mode, standardize) in enumerate(zip(*settings), start=1):
        if _is_blank(window):
            continue
        end = None
        try:
            end = _to_date(window_end) or today
            key = (dataset, names, pca.parse_mode(pca_mode), pca.parse_standardize(standardize),
                   _window(window), end, _db_key())
        except ValueError as exc:
            key = exc
        yield case, key, end


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("settings", ndim=2)
def PcaGrid(labels, settings, dataset="swap") -> List[list]:
    """
    複数の推定条件(ケース)の主成分ベクトルを1回の呼び出しでまとめて返す(PCAGridシート用)。

    settings: 4行×ケース数の列。行は上から 推定の最終日(空欄=今日)・window・モード・標準化。
      windowが空欄の列は使わない(行を出さない)。推定は復元日によらない(復元日はシート共通のRecon dateで
      PcaActualに渡す)。
    戻り値: 見出し行 GRID_HEADER + [PC1, …, PCm] と、ケースごと・銘柄ごとの行
      [ケース番号(列の順、1始まり), 推定開始日, 推定終了日, サンプル数, 除外した営業日数, 警告,
       銘柄, 平均, スケール, 標準偏差, PC1, …](銘柄以降はPcaModelと同じ列)。windowの情報はそのケースの全行に同じ値を入れる
      (Excel側はケース番号でFILTERして使う)。推定できないケースは1行だけで、警告にエラーの理由を入れ、
      他は空欄にする(1ケースのエラーで全ケースを止めない)。
    """
    dataset = parse_dataset(dataset)
    names = _labels(labels)
    m = len(names)
    today = _today()
    rows = [GRID_HEADER + [f"PC{j + 1}" for j in range(m)]]
    for case, key, end in _grid_cases(names, settings, dataset, today):
        try:
            if isinstance(key, ValueError):
                raise key
            model = _estimate_cached(*key)
        except ValueError as exc:
            rows.append([case, "", "", "", "", str(exc)] + [""] * (m + 4))
            continue
        warning = pca.gap_warning(model.end, end)
        info = [case, model.start, model.end, model.n_samples, model.excluded_days, warning]
        rows += [
            info + _model_row(model, i, label) for i, label in enumerate(names)
        ]
    return rows


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("settings", ndim=2)
@xw.arg("ks", ndim=1)
def PcaGridResidStd(labels, settings, ks, sigma_days=None, dataset="swap") -> List[list]:
    """
    PCAGridの差分のグラフの±1σ: ケースごと・主成分数kごとの差分の標準偏差(bp。PcaResidStdと同じ値)。

    settings: PcaGridと同じ。ks: 段ごとのk(空欄の段は空欄)。sigma_days: 全ケース共通(空欄=各ケースのwindow)。
    戻り値: 見出し行 [Case, Warning, ResidStd1, …](ksの順)と、ケースごと・銘柄ごとの行(PcaGridと同じ並び)。
      推定できないケースは1行だけで警告に理由。σが計算できない段(日数不足等)は空欄で、警告に理由を足す。
    推定はPcaGridとキャッシュを共有する(PcaGridの出力に列を足さないのは、テナーを多く選ぶと出力が
    右隣の計算領域にぶつかるため)。
    """
    dataset = parse_dataset(dataset)
    names = _labels(labels)
    ks = list(ks or [])
    n_days = _sigma_days(sigma_days)
    rows = [["Case", "Warning"] + [f"ResidStd{n + 1}" for n in range(len(ks))]]
    for case, key, _ in _grid_cases(names, settings, dataset, _today()):
        try:
            if isinstance(key, ValueError):
                raise key
            _estimate_cached(*key)
        except ValueError as exc:
            rows.append([case, str(exc)] + [""] * len(ks))
            continue
        sigmas, warnings = [], []
        for k in ks:
            try:
                sigmas.append(None if _is_blank(k) else _resid_std_cached(*key, _count(k, "主成分数k"), n_days))
            except ValueError as exc:
                sigmas.append(None)
                warnings.append(f"σ(k={k}): {exc}")
        warning = " / ".join(warnings)
        rows += [
            [case, warning] + ["" if sg is None else float(sg[i]) for sg in sigmas]
            for i in range(len(names))
        ]
    return rows


def _observe(labels, pca_mode, recon_date, real_time_range, dataset):
    dataset = parse_dataset(dataset)
    history = load_history(dataset)
    as_of = _to_date(recon_date)
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
def PcaActual(labels, pca_mode, recon_date=None, real_time_range=None, dataset="swap") -> List[list]:
    """
    復元日の実績(銘柄の並びの縦1列、値の単位)。レベルは水準、日次変化は
    「復元日の水準 − DBの前営業日(なければ直近の日)の水準」。

    recon_date が空欄ならRealTimeのライブの値(real_time_range: 1列目=銘柄、最後の列=値)、
    日付ならDBのその日の引け値(スワップのO/Nはその日のTONA実績)。選択銘柄が1本でも
    欠けていればエラーにする(欠けた銘柄を別の値で代用しない)。
    """
    obs, _, _ = _observe(labels, pca_mode, recon_date, real_time_range, dataset)
    return [[float(v)] for v in obs.values]


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PcaActualInfo(labels, pca_mode, recon_date=None, real_time_range=None,
                  dataset="swap") -> List[list]:
    """復元日の実績の情報(項目/値の2列。項目名は英語: 復元日・実績の元・変化の基準日・警告)。引数はPcaActualと同じ。"""
    obs, as_of, source = _observe(labels, pca_mode, recon_date, real_time_range, dataset)
    return [
        ["Recon date", as_of],
        ["Source", source],
        ["Change base", obs.base_date or ""],
        ["Warning", obs.warning],
    ]
