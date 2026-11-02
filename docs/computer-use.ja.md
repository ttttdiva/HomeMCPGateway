# Computer Use（視覚付きデスクトップ操作）

`desktop_observe`、`desktop_act`、`desktop_hold_keys` は、特定のAIクライアントに依存しない通常のMCPツールです。
クライアント側の分岐や専用処理はありません。MCPクライアントに Gateway を登録するだけで使えます。

```text
desktop_observe(window="Blender")   → 画像(PNG)＋メタデータ
        ↓ LLMが画像を見て座標を決める
desktop_act(window="Blender", action="click", x=0.62, y=0.47, coordinate_space="normalized")
        ↓
desktop_observe(window="Blender")   → 結果を再確認
```

`desktop_act` に `observe_after=true` を付けると、操作後の画像を同じ応答で受け取れます。
入力がOSに受理されても目的の達成は保証されないため、必ず画像で結果を確認してください。

## desktop_observe

| 引数 | 意味 |
| --- | --- |
| `window` | タイトル/プロセス名の一部（`"Blender"`）、`"pid:1234"`、`"hwnd:0x1A2B"`。最も適切な1ウィンドウを選ぶ |
| `hwnd` | HWND直指定（従来互換） |
| `mode` | `windows`（一覧）/`desktop`/`monitor`/`window`/`uia`。`window`指定時の既定は`window` |
| `capture_backend` | `auto`（既定: wgc→printwindow）/`wgc`/`printwindow`/`screen` |
| `with_uia` | 画像にUI Automation要素（意味情報）を加える |

画像は MCP ツール結果の `content` に `type: "image"`（`image/png`）として入り、先頭の `text`
ブロックと `structuredContent` に同じJSONメタデータが入ります。

後続の座標操作に使うメタデータ:

- `bounds` / `image_origin`: 画像が画面上で占める矩形（物理ピクセル）。既定はクライアント領域
- `window.bounds`・`window_geometry.extended_frame_bounds`・`client_bounds`・`client_size`
- `window_geometry.monitor_dpi`・`scale_percent`・`scale_factor`・`dpi_awareness`
- `capture_backend`（`wgc` 等）・`capture_area`（`client`/`window`）・`capture_attempts`・`uniform_image`
- `window_resolution`: `window`指定がどのウィンドウに一致したか（候補一覧を含む）

Windows Graphics Capture は DWM の合成結果を取得するため、Blender の 3D Viewport など GPU 描画領域も
撮影できます。保護された内容は黒/単色になることがあり、`uniform_image=true` で判別できます。
`auto` は単色結果のとき次のバックエンドを試します。

## desktop_act

座標 `x` `y` `end_x` `end_y` は `coordinate_space` で解釈されます。

| coordinate_space | 意味 |
| --- | --- |
| `screen`（既定） | 仮想デスクトップの絶対物理ピクセル（負値可） |
| `window` | 観測画像のピクセル。左上が(0,0) |
| `normalized` | 観測画像に対する0〜1。左上(0,0)、右下(1,1)。DPI倍率やリサイズに依存しない |

`window`/`normalized` は `window`/`hwnd` か `observation_id` が必要です。`observation_id` を渡すと、
その観測の画像矩形を基準にし、ウィンドウの移動・リサイズ・モニター構成の変更を検出して失敗します。
`normalized` が0〜1の範囲外なら入力せずエラーになります。全座標は物理ピクセルで計算されるため、
100%以外のDPI倍率でもずれません。応答の `resolved_screen_coordinates` に変換後の座標が入ります。

| action | 内容 |
| --- | --- |
| `move` `click` `double_click` | `button` = left/right/middle |
| `right_click` `middle_click` | `click` のエイリアス |
| `drag` | `x,y`→`end_x,end_y`、`duration_ms`、`button`（左/右/中ボタンドラッグ） |
| `scroll` | `delta_y`（正=上）/`delta_x`（正=右）、120が1ノッチ。`x,y`で先にカーソル移動 |
| `text` `type_text` | Unicode文字列をそのまま入力（IME非依存） |
| `key` `hotkey` | `keys=["Ctrl","Shift","a"]` を同時押し |
| `press_key` | `keys=["Down","Down","Enter"]` を順番に押す |
| `activate` `restore` `uia` | 従来どおり |

`modifiers=["Ctrl","Shift","Alt","Win"]` はポインター操作の間だけ押し続けます（Ctrl+クリック、
Alt+中ボタンドラッグなど）。操作が失敗しても必ず離します。キーはCtrl/Shift/Altを左側の仮想キーで送り、
テンキーは `NumPad0`〜`NumPad9` で指定します。

## desktop_hold_keys（ゲームQA用）

`desktop_hold_keys` は有限時間だけゲーム移動キーを押し続け、関数内で解放する専用ツールです。通常の
`desktop_act` のキー操作は従来どおり短い押下です。例:

```text
observed = desktop_observe(mode="window", hwnd=<対象HWND>, expected_pid=<対象PID>)
desktop_hold_keys(
    observation_id=observed.observation_id,
    hwnd=observed.window.hwnd,
    expected_pid=observed.window.pid,
    expected_process_create_time=observed.window.process_create_time,
    keys=["shift", "w"],
    duration_ms=4550,
)
```

必須引数は `observation_id`、`hwnd`、`expected_pid`、`expected_process_create_time`、`keys`、
`duration_ms` です。観測は呼出し時点から15秒以内で、キャッシュ内に残っている必要があります。HWND、PID、
プロセス作成時刻の3つが観測内容と完全一致し、対象ウィンドウが表示中・最小化されておらず、現在の前景
ウィンドウである場合だけ入力します。対象の自動アクティブ化はしません。

対象は `D:\Dev\95_Unferat_Game\UnferatGame.uproject` を開いている Unreal Editor、または同プロジェクト
配下の `UnferatGame` パッケージ実行ファイルに限定します。別の Unreal プロジェクトや他アプリには入力しません。
`keys` は `W`、`A`、`S`、`D`、`Space`、左 `Shift`、左 `Ctrl` のみで、1〜4個の重複しないキーを指定します。
左Ctrlは `LeftCtrl` または `LCtrl` で指定し、`VK_LCONTROL`（0xA2）として送ります。右Ctrl、左右不定の `Ctrl` /
`Control`、Alt、Win、ファンクションキー、OSショートカットなどは拒否します。`duration_ms` は整数100〜8000です。1回に
最大4個のキーを押し、同時Held-key要求は1つ、前景・プロセス確認は50ms間隔（20Hz）です。

押下前に `GetAsyncKeyState` の現在状態（high bit）を見て、すでに物理的に押されているキーがあれば入力を
拒否します。操作が押したキーだけを逆順に解放し、`SendInput` が解放を受理したことと、解放後にhigh bitが
下がったことを応答の `release.keys[].verified` で報告します。`release.verified` が `true` でない場合は成功を
返しません。前景喪失、ウィンドウ消失、PID再利用、キャンセル、入力エラー時は追加の押下を止め、受理済みの
キーだけを解放します。アプリ内で移動できたかは自動判定しないため `verification.status` は `unknown` です。

通常のキャンセルは非同期ツール内で同期解放してから `status="cancelled"` のreceiptを返します。検出遅延は
通常50ms以内ですが、イベントループの停止やOSの入力API遅延はこの上限に加わります。プロセス強制終了、OS停止、
ハードウェア障害時にPythonのfinally処理を保証することはできません。`SendInput` はHWNDを指定するAPIではなく、
前景に対するシステム入力です。各押下直前にも前景を検査しますが、検査からWin32呼出しまでの極短い競合や、
フォーカス喪失後に必要な解放キーの配送先を原子的に固定できません。応答の
`input_scope="global_sendinput_foreground_guarded"` はこの制約を示します。Windowsのキー状態APIは、押下後に
ユーザーが物理キーを押した場合に物理入力と合成入力を区別できず、その場合は解放後の状態を未確認として報告する
ことがあります。

## 例

```text
desktop_observe(window="Blender")
desktop_act(window="Blender", action="click", x=0.62, y=0.47, coordinate_space="normalized")
desktop_act(window="Blender", action="drag", button="middle", x=0.5, y=0.5, end_x=0.6, end_y=0.45,
            coordinate_space="normalized", modifiers=["Shift"])
desktop_act(window="Blender", action="scroll", x=0.5, y=0.5, coordinate_space="normalized", delta_y=120)
desktop_act(window="Blender", action="hotkey", keys=["Ctrl", "s"])
desktop_act(window="Blender", action="press_key", keys=["NumPad1"], observe_after=true)
```

UI Automation で要素が取れない領域（3D Viewport、ゲーム、canvas）では、画像から決めた座標で操作します。
意味情報が取れる場面では `with_uia=true` で併用できます。
