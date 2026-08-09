Attribute VB_Name = "BOJ_Analysis"
' ================================================================
' BOJ Swap Distortion Analysis - 更新マクロ
'
' 【使い方】
'   1. このファイルを Excel VBA エディタ（Alt+F11）でインポート
'      ファイル > ファイルのインポート > BOJ_Analysis.bas
'   2. PYTHON_DIR と PYTHON_EXE を自分の環境に合わせて変更
'   3. "Analysis" シートに図形（ボタン）を置き、
'      UpdateBOJAnalysis マクロを割り当てる
'
' 【動作】
'   ボタンクリック
'     → Python（main.py）をバックグラウンド実行（完了まで待機）
'     → output\boj_latest.xlsx が生成される
'     → そのデータをこのブックの "Analysis" シートに取り込む
' ================================================================

' ----------------------------------------------------------------
' ★ 環境設定（ここを変更してください）
' ----------------------------------------------------------------
Private Const PYTHON_DIR As String = "C:\path\to\pricing_sheet"   ' main.py があるフォルダ
Private Const PYTHON_EXE As String = "python"                      ' "python" or "python3" or フルパス
Private Const OUTPUT_FILE As String = "\output\boj_latest.xlsx"    ' Pythonが出力するファイル（相対パス）

' ----------------------------------------------------------------
' メイン更新マクロ（ボタンに割り当てる）
' ----------------------------------------------------------------
Public Sub UpdateBOJAnalysis()
    Dim startTime As Double
    startTime = Timer

    ' ステータスバー表示
    Application.StatusBar = "BOJ Analysis: Python 実行中..."
    Application.ScreenUpdating = False

    On Error GoTo ErrHandler

    ' --- 1. Python 実行（完了まで待機） ---
    Dim exitCode As Long
    exitCode = RunPythonAndWait(PYTHON_DIR, PYTHON_EXE)

    If exitCode <> 0 Then
        MsgBox "Python の実行に失敗しました（終了コード: " & exitCode & "）" & vbNewLine & _
               "コマンドプロンプトで以下を実行して確認してください:" & vbNewLine & _
               "  cd " & PYTHON_DIR & vbNewLine & _
               "  " & PYTHON_EXE & " main.py", _
               vbCritical, "BOJ Analysis Error"
        GoTo Cleanup
    End If

    ' --- 2. 出力ファイルからデータ取り込み ---
    Application.StatusBar = "BOJ Analysis: データ取り込み中..."

    Dim outputPath As String
    outputPath = PYTHON_DIR & OUTPUT_FILE

    If Not FileExists(outputPath) Then
        MsgBox "出力ファイルが見つかりません:" & vbNewLine & outputPath, _
               vbCritical, "BOJ Analysis Error"
        GoTo Cleanup
    End If

    Call ImportFromLatest(outputPath)

    ' --- 完了 ---
    Dim elapsed As Double
    elapsed = Timer - startTime
    Application.StatusBar = "BOJ Analysis: 更新完了 (" & Format(elapsed, "0.0") & "秒)  " & Now()

    GoTo Cleanup

ErrHandler:
    MsgBox "エラーが発生しました:" & vbNewLine & Err.Description, vbCritical, "BOJ Analysis"
    Application.StatusBar = "BOJ Analysis: エラー"

Cleanup:
    Application.ScreenUpdating = True
End Sub

' ----------------------------------------------------------------
' Python を実行して終了コードを返す（同期実行）
' ----------------------------------------------------------------
Private Function RunPythonAndWait(projectDir As String, pythonExe As String) As Long
    Dim wsh As Object
    Dim cmd As String

    ' WScript.Shell 経由で実行（waitOnReturn=True で完了まで待機）
    Set wsh = CreateObject("WScript.Shell")

    ' cmd /c でコマンドプロンプト経由実行（パス・環境変数を正しく認識させるため）
    cmd = "cmd /c cd /d """ & projectDir & """ && " & pythonExe & " main.py"

    ' 第3引数: ウィンドウスタイル 0=非表示, 1=通常
    ' 第4引数: waitOnReturn = True（完了まで待機）
    RunPythonAndWait = wsh.Run(cmd, 0, True)

    Set wsh = Nothing
End Function

' ----------------------------------------------------------------
' boj_latest.xlsx からデータを取り込んで Analysis シートに貼り付け
' ----------------------------------------------------------------
Private Sub ImportFromLatest(filePath As String)
    Dim srcWb  As Workbook
    Dim dstWs  As Worksheet
    Dim srcWs  As Worksheet

    ' 取り込み先シート（なければ作成）
    dstWs = GetOrCreateSheet(ThisWorkbook, "Analysis")

    ' 出力ファイルを画面非表示で開く
    Application.DisplayAlerts = False
    Set srcWb = Workbooks.Open(Filename:=filePath, ReadOnly:=True, _
                               UpdateLinks:=False, Notify:=False)
    Application.DisplayAlerts = True

    Set srcWs = srcWb.Sheets("BOJ Swap Analysis")

    ' 既存データをクリア（チャートは保持）
    dstWs.Cells.ClearContents
    dstWs.Cells.Interior.ColorIndex = xlNone

    ' 全データをコピー（値・書式）
    srcWs.UsedRange.Copy
    dstWs.Range("A1").PasteSpecial Paste:=xlPasteAllUsingSourceTheme
    Application.CutCopyMode = False

    ' ソースを閉じる（保存しない）
    srcWb.Close SaveChanges:=False

    ' Analysis シートをアクティブに
    dstWs.Activate
    dstWs.Range("A1").Select
End Sub

' ----------------------------------------------------------------
' ユーティリティ
' ----------------------------------------------------------------
Private Function GetOrCreateSheet(wb As Workbook, sheetName As String) As Worksheet
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = wb.Sheets(sheetName)
    On Error GoTo 0
    If ws Is Nothing Then
        Set ws = wb.Sheets.Add(After:=wb.Sheets(wb.Sheets.Count))
        ws.Name = sheetName
    End If
    Set GetOrCreateSheet = ws
End Function

Private Function FileExists(path As String) As Boolean
    FileExists = (Dir(path) <> "")
End Function

' ----------------------------------------------------------------
' テスト用：Python だけ実行（データ取り込みなし）
' ----------------------------------------------------------------
Public Sub TestRunPython()
    Dim exitCode As Long
    exitCode = RunPythonAndWait(PYTHON_DIR, PYTHON_EXE)
    MsgBox "Python 終了コード: " & exitCode, vbInformation, "Test"
End Sub
