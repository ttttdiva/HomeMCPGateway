# Computer Use（視覚付きデスクトップ操作）

`desktop_observe` と `desktop_act` は、特定のAIクライアントに依存しない通常のMCPツールです。
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
