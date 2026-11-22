"""
分析ブック(SwapAnalysis.xlsm)のUDF。Excelとswap_analysisの唯一の接点。

■ 設計方針(PricingSheet.xlsmと同じ)
  - VBAは使わない。Excelの関数で難しい計算(固有値分解・DBの読み出し)だけをUDFにし、
    得点・復元(行列の積)・差分・寄与率などはExcelの数式で計算する。
  - 計算方法は「手動」+F9。UDFは非volatile。日付は引数で受け取り、空欄はエラー(UDFの中で今日を決めない。
    シートは空欄の入力セルを式で TradeDate に寄せて渡す。docs/decisions.md の「評価日」)。
  - 配列はそのまま返し、Excelの動的配列のスピルに任せる(@xw.ret(expand=...)は使わない)。
  - 主成分の推定は引数とDBの版ごとにプロセス内で覚えておき、
    PcaModel/PcaEigen/PcaWindow/PcaGridで1回の計算を共有する。
  - Excelとの往復はUDFのセル1つごとにかかるので、複数の条件をまとめて見るシート(PCAGrid)は
    ケースごとにUDFを呼ばず、PcaGridの1回で全ケースの推定結果を返す。
  - 計算できないときはエラーにする(エラーを文字列や空欄に置き換えて返さない)。

■ 引数(共通)
  labels: 使う銘柄の縦の並び(スワップはテナー、ボラは「満期x原資産」)。並び順が表の行順。
  pca_mode: 「Level」/「Change」(「レベル」/「日次変化」も可)。
  standardize: 「Yes」(z-score)/「No」(分散共分散)(「あり」/「なし」も可)。
  window: サンプル数(営業日)。window_end: 推定の最終日(Window end。DBの引け値だけを使うので、
    DBにその日の値がなければ直近の営業日まで)。推定(PcaModel/PcaEigen/PcaWindow/PcaResidStd/PcaGrid)は復元日によらない。
  recon_date: 復元日(Recon date。PcaActual/PcaActualInfo)。今日ならRealTimeのライブの値、過去の日付ならDBのその日の引け値。
  dataset: "swap"(既定)/"vol"。
  sigma_days: 差分のσ(PcaResidStd・PcaGridResidStd)をとるサンプル数(推定終了日からさかのぼる営業日。
    空欄=推定のwindowと同じ。windowより大きくすれば推定より前の日も入る)。
"""

from functools import lru_cache
from typing import List, Optional

import xlwings as xw

from engine.common import pca, udf_common
from engine.common.history import UNIT_BP, load_history, normalize_label, parse_dataset


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
    return None if udf_common._is_blank(value) else _count(value, "σの日数(営業日数)")


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
    return udf_common._required_date(value, "推定の最終日(Window end)")


def _model_key(labels, pca_mode, standardize, window, window_end, dataset) -> tuple:
    """_estimate_cached の引数。"""
    return (
        parse_dataset(dataset), udf_common._labels(labels), pca.parse_mode(pca_mode),
        pca.parse_standardize(standardize), _window(window), _window_end(window_end), udf_common._db_key(),
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
def PcaModel(labels, pca_mode, standardize, window, window_end, dataset="swap") -> List[list]:
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
        _model_row(model, i, label) for i, label in enumerate(udf_common._labels(labels))
    ]


@xw.func
@xw.arg("labels", ndim=1)
def PcaResidStd(labels, pca_mode, standardize, window, k, window_end, sigma_days=None,
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
def PcaEigen(labels, pca_mode, standardize, window, window_end, dataset="swap") -> List[list]:
    """固有値(降順。標準化なしはbp²、ありは単位なし)。見出し行 [PC, Eigenvalue] + 主成分ごとの行。
    寄与率・累積寄与率はExcelの数式で計算する。引数はPcaModelと同じ。"""
    model = _model(labels, pca_mode, standardize, window, window_end, dataset)
    return [["PC", "Eigenvalue"]] + [
        [f"PC{j + 1}", float(v)] for j, v in enumerate(model.eigenvalues)
    ]


@xw.func
@xw.arg("labels", ndim=1)
def PcaWindow(labels, pca_mode, standardize, window, window_end, dataset="swap") -> List[list]:
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


def _grid_cases(names, settings, dataset):
    """PcaGrid/PcaGridResidStdのケース: windowが空欄でない列ごとに (ケース番号, 推定のキー, 推定の最終日)。
    設定の誤り(推定の最終日の空欄等)はそのケースの番号を付けたエラーにする。"""
    for case, (window_end, window, pca_mode, standardize) in enumerate(zip(*settings), start=1):
        if udf_common._is_blank(window):
            continue
        try:
            end = _window_end(window_end)
            key = (dataset, names, pca.parse_mode(pca_mode), pca.parse_standardize(standardize),
                   _window(window), end, udf_common._db_key())
        except ValueError as exc:
            raise ValueError(f"Case {case}: {exc}") from exc
        yield case, key, end


def _grid_model(case, key) -> pca.PcaModel:
    try:
        return _estimate_cached(*key)
    except ValueError as exc:
        raise ValueError(f"Case {case}: {exc}") from exc


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("settings", ndim=2)
def PcaGrid(labels, settings, dataset="swap") -> List[list]:
    """
    複数の推定条件(ケース)の主成分ベクトルを1回の呼び出しでまとめて返す(PCAGridシート用)。

    settings: 4行×ケース数の列。行は上から 推定の最終日・window・モード・標準化。
      windowが空欄の列は使わない(行を出さない)。推定は復元日によらない(復元日はシート共通のRecon dateで
      PcaActualに渡す)。
    戻り値: 見出し行 GRID_HEADER + [PC1, …, PCm] と、ケースごと・銘柄ごとの行
      [ケース番号(列の順、1始まり), 推定開始日, 推定終了日, サンプル数, 除外した営業日数, 警告,
       銘柄, 平均, スケール, 標準偏差, PC1, …](銘柄以降はPcaModelと同じ列)。windowの情報はそのケースの全行に同じ値を入れる
      (Excel側はケース番号でFILTERして使う)。推定できないケースが1つでもあれば、ケース番号を付けたエラーにする。
    """
    dataset = parse_dataset(dataset)
    names = udf_common._labels(labels)
    m = len(names)
    rows = [GRID_HEADER + [f"PC{j + 1}" for j in range(m)]]
    for case, key, end in _grid_cases(names, settings, dataset):
        model = _grid_model(case, key)
        info = [case, model.start, model.end, model.n_samples, model.excluded_days, pca.gap_warning(model.end, end)]
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
    戻り値: 見出し行 [Case, ResidStd1, …](ksの順)と、ケースごと・銘柄ごとの行(PcaGridと同じ並び)。
      推定できないケース・σが計算できない段(日数不足等)があれば、ケース番号とkを付けたエラーにする。
    推定はPcaGridとキャッシュを共有する(PcaGridの出力に列を足さないのは、テナーを多く選ぶと出力が
    右隣の計算領域にぶつかるため)。
    """
    dataset = parse_dataset(dataset)
    names = udf_common._labels(labels)
    ks = list(ks or [])
    n_days = _sigma_days(sigma_days)
    rows = [["Case"] + [f"ResidStd{n + 1}" for n in range(len(ks))]]
    for case, key, _ in _grid_cases(names, settings, dataset):
        _grid_model(case, key)
        sigmas = []
        for k in ks:
            if udf_common._is_blank(k):
                sigmas.append(None)
                continue
            try:
                sigmas.append(_resid_std_cached(*key, _count(k, "主成分数k"), n_days))
            except ValueError as exc:
                raise ValueError(f"Case {case}, k={k}: {exc}") from exc
        rows += [
            [case] + ["" if sg is None else float(sg[i]) for sg in sigmas]
            for i in range(len(names))
        ]
    return rows


def _observe(labels, pca_mode, recon_date, real_time_range, dataset):
    """復元日の実績。復元日が今日ならRealTimeのライブの値、過去の日付ならDBのその日の引け値。"""
    dataset = parse_dataset(dataset)
    history = load_history(dataset)
    as_of = udf_common._required_date(recon_date, "復元日(Recon date)")
    if as_of == udf_common._today():
        source, level = "RealTime", {}
        for r in real_time_range or []:
            if r is None or len(r) < 2 or udf_common._is_blank(r[0]):
                continue
            try:
                level[normalize_label(r[0])] = float(r[-1])
            except (TypeError, ValueError):
                pass  # 空欄・エラー値は欠損(選択銘柄が欠けていれば pca.observe がエラーにする)
    else:
        source, level = f"DB({as_of})", history.get(as_of)
        if level is None:
            raise ValueError(f"{as_of}の値がDBにありません(休日か、取り込み期間外)")
    obs = pca.observe(history, udf_common._labels(labels), pca.parse_mode(pca_mode), as_of, level, source)
    return obs, as_of, source


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PcaActual(labels, pca_mode, recon_date, real_time_range=None, dataset="swap") -> List[list]:
    """
    復元日の実績(銘柄の並びの縦1列、値の単位)。レベルは水準、日次変化は
    「復元日の水準 − DBの前営業日(なければ直近の日)の水準」。

    recon_date が今日ならRealTimeのライブの値(real_time_range: 1列目=銘柄、最後の列=値)、
    過去の日付ならDBのその日の引け値(スワップのO/Nはその日のTONA実績)。選択銘柄が1本でも
    欠けていればエラーにする(欠けた銘柄を別の値で代用しない)。
    """
    obs, _, _ = _observe(labels, pca_mode, recon_date, real_time_range, dataset)
    return [[float(v)] for v in obs.values]


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PcaActualInfo(labels, pca_mode, recon_date, real_time_range=None,
                  dataset="swap") -> List[list]:
    """復元日の実績の情報(項目/値の2列。項目名は英語: 復元日・実績の元・変化の基準日・警告)。引数はPcaActualと同じ。"""
    obs, as_of, source = _observe(labels, pca_mode, recon_date, real_time_range, dataset)
    return [
        ["Recon date", as_of],
        ["Source", source],
        ["Change base", obs.base_date or ""],
        ["Warning", obs.warning],
    ]
