"""
オプションの共有の部品。ブックごとのUDF・表は engine/bond_option/(BondOption)と engine/position/(PositionSheet)。

  term     : 満期までの時間(営業日数・252・期間T・セータの1営業日)。下の両方が使う
  swaption/: スワップション(原資産 = フォワード・スワップレート、バシュリエ = レートが正規分布)
    convert: TraditionのATMストラドルのプレミアム → 営業日ベースのノーマルvol、契約(満期日・起算日)
    forward: 契約の原資産のフォワード・スワップレート(DBの swaption_forwards)
  bond/    : 国債店頭オプション(原資産 = 債券の先渡clean単価、Black = 単価が対数正規分布)
    pricer : 先渡MD・期間・Black式・グリークス・ITMバイライトのストライク(docs/bond_option.md の F7〜F11)
    vol    : スワップションのATMノーマルvolの格子から債券オプションのボラを補間する
    hv     : 現物とスワップのヒストリカル・ボラの比(Vol ratio)
LSEGからのプレミアムの取得は engine/common/swaption_source.py(データの取得の層なのでここには置かない)。
"""
