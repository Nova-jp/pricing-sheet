# 生シートの見本

会社のファイルを貼る生シート(PositionSheetの`Raw_IR`・`Raw_IRVOL`・`Raw_Swaption`・`Raw_BondOption`・`Raw_JGB`)の
見本の値。雛形(`templates/<ブック>`)の生シートは空なので、雛形から作ったブックで動きを確かめるときに貼る。

```
python -m tools.raw_sample load PositionSheet.xlsm   # 見本 → ブックの生シート(空の生シートにだけ貼る)
python -m tools.raw_sample dump PositionSheet.xlsm   # ブックの生シート → 見本(差分を出してから上書き)
```

- **置くのは作り物の値だけ**。本物の会社のデータ(実際の約定番号・取引先・ポジション)を貼ったブックからは
  `dump`しない。見本を直したら、`git diff`で中身を確かめてからコミットする。
- 1ファイル = 1シート、A1からの格子そのまま(1行目は会社のファイルの見出し)。日付は`2026-11-27`の形。
- 書き方の決まりは`tools/raw_sample.py`の先頭。
