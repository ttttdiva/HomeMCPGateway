# Desktopの対象確認と操作後観測（2026-09-23）

## 変更した実行側の契約

通常のWindows操作では、判断は呼出側、観測と入力はGatewayが担当する。
Jev/LLM/APIキーや検索機能をこの変更で追加していない。隔離Playwright QAと
ログイン済みデスクトップも従来どおり別経路である。

`desktop_act` に、後方互換の任意引数を追加した。

| 引数 | 既定 | 意味 |
| --- | --- | --- |
| `target_guard_ref` | `null` | 現在のwindow/UIA観測に結び付けたpointer入力の確認対象 |
| `observe_after` | `false` | 明示操作後のPNGと構造化観測を同じ応答に含める |
| `observe_with_uia` | `false` | 上記の観測に有限のUIA走査も含める |

`target_guard_ref` は `observation_id` と併用する。runtime ID、role/name、
AutomationId、PID/native HWND、bounds、enabled/offscreen、指定点の
ElementFromPoint/祖先照合を確認する。観測対象または子要素でなければ入力せず失敗する。
activationとUIA workerの後にもウィンドウの同一性・geometry・foregroundを確認する。

入力は引き続きSendInputであり、UIA Invokeへ置き換えない。従来の
`element_ref` は明示したUIA pattern操作のまま。guardを指定しないcanvas等の
物理座標操作、負座標、日本語・補助Unicode文字、Ctrl+A後の入力を維持する。
別プロセスのhit-testとSendInputはOS上の原子的な一操作ではなく、最後の確認後の
競合まで絶対に防げるという保証ではない。

## 応答と再試行

`ok=true` は従来どおりOS/操作経路の受付を意味する。目的の達成保証ではない。

- `execution_status="accepted"`
- `verification={"status":"unknown","kind":"application_effect"}`
- `retry_policy="observe_before_retry"`
- `target_guard="verified"` または `"not_requested"`

`observe_after=true` では `observation_after` とPNG ImageContentが付く。
観測取得に失敗しても、既に受け付けた入力を未実行へ戻さず、`observation_error`
に例外の種類だけを返す。操作のtextを通常の結果へ反射しない。ただし明示要求した
画像/UIA観測は実画面なので、そこに表示される情報を含む。

MCP SDKが `Union[dict, CallToolResult]` を直接登録できないため、
`qa_common.register_tools` で該当する混合返却だけを
`Annotated[CallToolResult, dict[str, Any]]` として登録する。
Python直接呼出しの既存dict応答、MCP上のdict構造、画像ブロックを維持する。

UIA pattern workerのtimeout、部分的SendInput失敗は、元の既実行可能性の注意を
維持する。自動で再送しない。結果の再観測は業務上の完了証拠とは区別する。

## 検証

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_desktop*.py"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_stdio.py"
```

`test_desktop_action_stdio.py` の通常試験はOSをmockした別stdioプロセスで、
新引数のtools/list、旧形式の結果、PNG/structured_contentを検証する。

実機試験は、先に既存の `tests/native_window_fixture.py` を専用ディレクトリを
指定して起動し、そのディレクトリを `HOME_MCP_ACTION_FIXTURE_DIR` に設定した
場合だけ有効になる。実ユーザーの任意のウィンドウを検索して操作する試験ではない。

2026-09-23の実測では、実機の専用WinForms画面で以下を確認した。

1. 公開中の旧Gateway経由で windows → PNG/UIA → SendInput → PNG の経路を実施。
2. 新ソースの別stdioプロセスで、guard付きクリックがGUIカウントを1だけ増やした。
3. 対象外座標が拒否され、カウントが増えなかった。
4. observe_afterの1枚のPNGと構造化結果を確認。
5. 日本語・絵文字・Ctrl+A置換を実GUIの値と最終PNG/UIAで確認。

実機には負座標のmonitorが無かった。負座標と移動/PID/runtime IDの各種不一致は
unit testの範囲であり、複数monitor上の新guard実機検証は未実施。
既存の独立QAブラウザはstdio回帰試験で確認したが、
今回 `browser_qa.py` の実装は変更・commit対象にしていない。

## 稼働中Tunnelへの反映は別工程

作業中のGateway/Tunnelは再起動していない。新引数の別プロセスtools/listが通っても、
現在のChatGPT接続に同じ引数が公開済みという意味ではない。

このcheckoutでは次の実在を確認した。

- `scripts/run_gateway.cmd`（stdio Gatewayの起動）
- `scripts/connect_tunnel.ps1`（既存.envから接続）
- `tunnel-client-v0.0.14-windows-amd64/tunnel-client.exe`
- runtime alias `home-mcp`（supervisor-statusの値）

全ての利用中セッションと他作業が終了した後、利用者が反映する場合の手順:

```powershell
Set-Location D:\Dev\80_HomeMCPGateway
.\tunnel-client-v0.0.14-windows-amd64\tunnel-client.exe runtimes stop home-mcp
.\scripts\connect_tunnel.ps1
```

停止後は既存supervisorが再接続する場合もある。これらは今回実行していない。
再接続後はChatGPTの公開ツールを更新し、`system_info` のGateway PID/start timeと、
`desktop_act` の上記3引数の公開、専用fixtureでのPNG応答を確認する。

注意: 作業開始時から別作業の `.env.example`、`README.md`、`browser_qa.py`、
`tests/test_stdio.py`、未追跡の `browser_agent.py` / `jev.py` /
`test_jev_browser_agent.py` が存在した。このcommitには含めず、上書きもしなかった。
現checkoutをそのまま再起動すると、それらの別作業も読み込まれ得る。
本変更だけの反映と混同せず、他作業の整理・確認後に再起動すること。
