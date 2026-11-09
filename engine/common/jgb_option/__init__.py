"""
国債店頭オプションの共有の部品(BondOption.xlsm と PositionSheet.xlsm のどちらからも使う)。

  pricer: 先渡MD・期間・Black式・ITMバイライトのストライク(docs/bond_option.md の F7〜F11)
  vol   : スワップションのATMノーマルvolの格子から債券オプションのボラを補間する
  hv    : 現物とスワップのヒストリカル・ボラの比(Vol ratio)
ブックごとのUDF・表は engine/bond_option/(BondOption)と engine/position/(PositionSheet)。
"""
