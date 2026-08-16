"""
Cloud Run Job のエントリポイント。

1. swap_pricing.morning_batch を実行し、data/curve_cache.db を更新する
   (NEON_CONNECTION_STRINGは環境変数から取得。Secret Manager経由で注入する想定)
2. 生成された curve_cache.db を GCS の固定パス(推測困難なUUID付き)に
   アップロードし、そのオブジェクトのみを公開読み取り可能にする

会社PC側は、この公開URLをcurl/PowerShellで叩いてファイルを取得するだけで、
Neon等の認証情報は一切持たない。

必須環境変数:
  NEON_CONNECTION_STRING : Neonの接続文字列(Secret Manager経由)
  GCS_BUCKET             : アップロード先バケット名
  GCS_OBJECT_PATH         : アップロード先オブジェクトパス(UUID/curve_cache.db等)
"""

import os
import sys

from google.cloud import storage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swap_pricing import morning_batch
from swap_pricing.local_cache import CURVE_CACHE_DB_PATH


def main() -> None:
    bucket_name = os.environ["GCS_BUCKET"]
    object_path = os.environ["GCS_OBJECT_PATH"]
    days = int(os.environ.get("BATCH_DAYS", "400"))

    print(f"[entrypoint] morning_batch.run(days={days}) を開始")
    morning_batch.run(days=days)

    print(f"[entrypoint] {CURVE_CACHE_DB_PATH} を gs://{bucket_name}/{object_path} にアップロード")
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_path)
    blob.upload_from_filename(str(CURVE_CACHE_DB_PATH))
    blob.make_public()  # このオブジェクトだけを読み取り専用で公開(バケット全体は非公開のまま)

    print(f"[entrypoint] 完了: {blob.public_url}")


if __name__ == "__main__":
    main()
