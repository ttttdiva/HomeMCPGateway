# QAブラウザの操作後検証

既存の隔離Playwrightセッションの `browser_click` / `browser_fill` に、後方互換の任意引数と結果情報を追加した。実ユーザーのログイン済みEdgeと共有したり、自律エージェント・Jevキー・検索機能を追加する変更ではない。

## 呼び出し

`browser_fill(session_id, selector, value, timeout_sec=30, verify_value=true, verify_timeout_sec=5)` はPlaywrightの既存fillを行い、値をローカルで照合する。contenteditableはinnerTextを比較する。Enter/submitは追加せず、入力値を結果や検証エラーへ含めない。

`browser_click(session_id, selector, button="left", timeout_sec=30, expected_text=null, expected_url=null, expected_checked=null, result_selector="body", verify_timeout_sec=5)` は既存のactionabilityを通して一度だけclickする。任意のtext/URL/checked条件を指定した場合だけ、その条件を有限時間検証する。URLはglobではなく完全一致である。checkedを指定した場合の対象はクリックしたselector、textだけの場合はresult_selectorとなる。

検証待機は0秒超30秒以下。Playwrightの操作timeoutと、その後の条件待機は別の予算である。取消は伝播する。

## 結果の意味

従来のfilled/clicked/urlを残し、executed、execution_status、verification、retry_policyを追加する。操作受付後の検証が失敗しても `executed=true` のまま、`verification.status=failed` とする。同じクリックを再実行しない。検証条件なしのclickはunknownであり、目的達成の証明ではない。返却された状態を確認してから次の操作を判断する。

## 検証と起動元

`tests/test_browser_postconditions.py` は隔離Edgeで日本語入力、contenteditable、controlled inputの差し戻し、表示・checkbox条件、失敗時に再クリックしないこと、引数の事前検証、実MCP stdioのschema/入出力を検証する。

stdioランチャーは自分の配置先のrepository rootをsys.pathの先頭に置く。これにより別checkoutを指定しても既存editable installを黙って実行する問題を防ぐ。稼働中Gatewayの再起動や環境変更は行わない。

2026-09-23の検証では、既存の未commit自律エージェント差分を除いたHEAD＋今回の差分を一時snapshotに展開し、全112テスト中109成功・3skipを確認した。未commit差分が含まれるローカル全体とは区別する。稼働中Tunnelへの新引数公開はGatewayの更新後再起動とChatGPT側の再接続が必要であり、この作業中には実行しない。

検証budgetはlocator評価も含めた全体のdeadlineに適用する。fill自体のSDKエラーは入力値を含む可能性があるため、詳細を返さずoutcome_unknownとして再観測を要求する。追加の2回帰テストで確認する。
