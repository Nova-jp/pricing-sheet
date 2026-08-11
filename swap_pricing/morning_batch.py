"""
朝バッチ: Neonから過去N日分のOISレートを取得し、生レートをローカル
SQLiteキャッシュ(local_cache.py)に保存する。各日について念のため
bootstrap_curve()が正常に通ることも検証する(データ不備の早期検知)。

日中はこのキャッシュだけを参照し、Neonには一切アクセスしない設計
(Neonのコールドスタート・レイテンシを避けるため)。

使い方:
    python -m swap_pricing.morning_batch [--days 365] [--include-odd-tenors]
"""

import argparse
from datetime import date, timedelta

from swap_pricing.curve import bootstrap_curve
from swap_pricing.local_cache import cached_dates, save_rates
from swap_pricing.neon_client import get_ois_rates_for_range


def run(days: int = 365, include_odd_tenors: bool = False, force: bool = False) -> None:
    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    rates_by_date = get_ois_rates_for_range(start_date, end_date)
    already_cached = set(cached_dates()) if not force else set()

    targets = sorted(d for d in rates_by_date if d not in already_cached)
    print(f"対象日数: {len(targets)} / Neon上の利用可能日数: {len(rates_by_date)}")

    n_ok, n_err = 0, 0
    for d in targets:
        try:
            rates = rates_by_date[d]
            bootstrap_curve(d, rates, include_odd_tenors=include_odd_tenors)  # 検証のみ
            save_rates(d, rates)
            n_ok += 1
        except Exception as exc:
            print(f"  [ERROR] {d}: {exc}")
            n_err += 1

    print(f"完了: {n_ok}件キャッシュ更新, {n_err}件エラー")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--include-odd-tenors", action="store_true")
    parser.add_argument("--force", action="store_true", help="既にキャッシュ済みの日付も再計算する")
    args = parser.parse_args()
    run(days=args.days, include_odd_tenors=args.include_odd_tenors, force=args.force)
