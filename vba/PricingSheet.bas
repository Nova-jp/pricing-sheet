Attribute VB_Name = "PricingSheet"
' ================================================================
' Swap Pricing Sheet - 更新マクロ
'
' 【事前準備】
'   1. Python環境に xlwings をインストール（pip install xlwings）
'   2. xlwings Excel Add-inをインストール（コマンドラインで `xlwings addin install`）
'   3. このファイルを Excel VBA エディタ（Alt+F11）でインポート
'      ファイル > ファイルのインポート > PricingSheet.bas
'   4. "Calc" シート等に図形（ボタン）を置き、RefreshPricingSheet マクロを割り当てる
'
' 【動作】
'   ボタンクリック
'     → xlwings の RunPython 経由で swap_pricing.pipeline.refresh() を実行
'     → RealTime シートのライブ値 + ヒストリカルDBファイルの値が Calc シートに書き込まれる
'
' 【パスについて】
'   このリポジトリはGitHub経由で各PCへクローンされる想定のため、
'   プロジェクトのルートパスはハードコードせず、ThisWorkbook.Path から動的に解決する。
'   このブック（.xlsm）はリポジトリ直下に置くこと。
' ================================================================

Public Sub RefreshPricingSheet()
    Dim projectDir As String
    projectDir = ThisWorkbook.Path

    On Error GoTo ErrHandler
    Application.StatusBar = "Pricing Sheet: 更新中..."

    RunPython "import sys; sys.path.insert(0, r'" & projectDir & "'); " & _
              "from swap_pricing.pipeline import refresh; refresh()"

    Application.StatusBar = "Pricing Sheet: 更新完了  " & Now()
    Exit Sub

ErrHandler:
    MsgBox "エラーが発生しました:" & vbNewLine & Err.Description, vbCritical, "Pricing Sheet"
    Application.StatusBar = "Pricing Sheet: エラー"
End Sub
