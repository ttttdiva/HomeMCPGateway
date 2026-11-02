# 実行中のMicrosoft Edgeへ接続する

通常のQAブラウザーと、普段使っているEdgeは別のモードです。

- `browser_start` はGatewayが起動する所有ブラウザーです。既定では独立したPlaywrightセッションを作ります。
- `browser_attach_edge` はAoiTalk（41）が起動時に管理する専用Edge brokerからcaller sessionを作ります。Gateway/Jevは認証付きWindows named pipe経由でbrokerへ問い合わせ、loopback限定のPlaywright WebSocketへ接続します。Gatewayはbrokerのクライアントとして動作し、brokerやEdgeを起動しません。

専用EdgeへのCDP接続はAoiTalk（41）のbrokerが管理します。Gateway/Jevは直接CDP接続や`DevToolsActivePort`の読取を行わず、認証付きnamed pipeから取得したendpointだけを使います。41が起動していない、またはbrokerに接続できない場合は`edge_broker_unavailable`を返します。別のブラウザーへ自動で切り替えません。

既存Edgeを使う場合は、`browser_attach_edge` の結果にある `tab_handle` を `browser_select_tab` に渡して対象を選びます。`browser_list_tabs` は再列挙と新しいタブの確認に使えます。タブハンドルは一覧順に依存せず、ページ遷移後も同じタブを指します。選択したタブが閉じられた場合は `browser_tab_closed` を返し、別タブへ切り替えません。曖昧さを避けるため、最初の対象は明示選択してください。

選択後の `browser_run_plan` はそのcaller sessionが選択したページを再利用します。共有コンテキストに開いたポップアップや別タブは自動採用しません。`browser_open_tab` は新しいQA-owned tabを作成し、そのcaller sessionの選択対象にします。以後の `browser_navigate` / `browser_run_plan` はその新tabを使います。これはOSのactive windowを切り替えません。既存タブに戻るときは `browser_list_tabs` のhandleを `browser_select_tab` で明示選択します。選択ページ、console/error監視、QA tab所有権はcaller sessionごとに分離します。`browser_detach` と添付セッションの `browser_close` はそのlogical sessionだけを解放します。Gateway/Jev終了もbroker内のEdge接続を閉じません。Gatewayが起動した所有ブラウザーは従来どおり閉じられます。

添付セッションで新しいタブを使う場合は、callerが `browser_open_tab(session_id)` を明示して空タブを作成します。返されたhandleは選択済みです。Gatewayはこのsessionが作成したtabだけを `browser_close_tab(session_id, tab_handle)` で閉じられます。既存タブや別sessionのhandleを指定すると `browser_tab_not_owned` で拒否します。detachとMCP終了はQA-owned tabも自動では閉じないため、使い終えたfixtureはdetach前に明示cleanupしてください。

### QA-owned tab の安全な drag

`browser_drag` は同じcallerが `browser_open_tab` で作成したtabだけで動きます。既存ユーザーtab、別callerのtab、isolated browser sessionには適用できません。実行前に必須の `expected_url`（完全一致）または `expected_url_prefix`（originとpathのprefix）で現在URLを照合し、不一致なら `browser_drag_url_mismatch` で拒否します。

引数は `session_id`, `tab_handle`, `source_selector`, `target_selector`, `expected_url` または `expected_url_prefix`, `steps`（1–120、既定12）, `timeout_sec`（0.1–120秒、既定10）, `hold_ms`（0–5000、既定100）, `settle_ms`（0–5000、既定0）, `target_offset_x`, `target_offset_y`, `release_delay_ms`（0–5000、既定0）, `modifiers`（Alt/Control/Meta/Shiftの配列、任意）です。source/target selectorはCSS selectorで、それぞれ要素1件に一致する必要があります。Gatewayは両要素をscroll-into-viewし、source中心と指定されたdrop位置がviewport内にあることを確認してから、mouse move → left button down → 分割move → upを実行します。中心/指定位置がviewport内に収まらない場合はドラッグを始めず拒否します。任意JavaScript実行は行いません。

`target_offset_x` / `target_offset_y` はtargetのbounding box左上を基準にします。省略値は各軸の中心です。`0 <= value <= 1` はboxサイズに対する比率（`0.75` は幅/高さの75%）、`value > 1` はCSS pixelです。比率`1`とbox幅/高さちょうどのpixel値はhit-test可能な右端/下端内側へ0.01pxだけ寄せます。負数、bool、NaN/Infinity、box寸法を越えるpixel offsetは `browser_drag_target_offset_x_invalid` / `browser_drag_target_offset_y_invalid` で拒否します。

`hold_ms`はmouse downの後、最初のdrag moveの前に待つ時間です。最後のmoveとmouse upの間には既定で追加待機はありません。`release_delay_ms`（0–5000、既定0）は最後のmoveの後、dropを発生させるmouse upの直前に待ちます。`settle_ms`（0–5000、既定0）はmouse upの後に待ちます。戻り値の`target.drop_point`は実際のCSS座標、`target_offset`は要求値とbox内の解決pixel値、`release_to_return_ms`はmouse upからtoolが戻るまでの実測値です。`final_url`はsettle完了時点のsanitized host/path、`url_guard_matched_after_settle`は実行前guardへの一致結果です。省略時は中央drop・待ちなしとなり、既存呼び出しの動作を保ちます。

例:

```python
browser_drag(
    session_id="<attached caller session>",
    tab_handle="<browser_open_tab が返したhandle>",
    source_selector="#drag-source",
    target_selector="#drop-target",
    expected_url="http://127.0.0.1:3002/at/qa/drag-fixture",
    steps=12,
    hold_ms=100,
    target_offset_x=0.6,
    target_offset_y=0.75,
    release_delay_ms=100,
    settle_ms=250,
)
```

成功時はsource/targetの解決結果、target中心と実drop座標、offsetの要求値/解決pixel値、実行steps、hold/release-delay/settle、modifiers、query/fragmentを除いた最終URLのhost/pathを返します。要素本文、cookie、token、selector文字列は返しません。拒否コードは `browser_drag_tab_not_owned`, `browser_drag_url_guard_required`, `browser_drag_url_guard_invalid`, `browser_drag_url_mismatch`, `browser_drag_source_selector_invalid`, `browser_drag_target_selector_invalid`, `browser_drag_source_not_found`, `browser_drag_target_not_found`, `browser_drag_source_ambiguous`, `browser_drag_target_ambiguous`, `browser_drag_source_not_visible`, `browser_drag_target_not_visible`, `browser_drag_source_outside_viewport`, `browser_drag_target_outside_viewport`, `browser_drag_target_offset_x_invalid`, `browser_drag_target_offset_y_invalid`, `browser_drag_steps_invalid`, `browser_drag_timeout_invalid`, `browser_drag_hold_invalid`, `browser_drag_release_delay_invalid`, `browser_drag_settle_invalid`, `browser_drag_modifiers_invalid`, `browser_drag_viewport_unavailable`, `browser_drag_failed` です。close済みtabは `browser_tab_closed` を返します。

### QA-owned tab の画面計測

### QA-owned tab のviewport

`browser_set_viewport(session_id, tab_handle, width, height, device_scale_factor=1.0, expected_url="", expected_url_prefix="")`は同じattached callerが`browser_open_tab`で作ったtabだけに適用します。guardはbrowser_dragと共有し、expected URLは必須です。幅は320–3840、高さは240–2160、device scale factorは0.5–3です。

対象pageのCDP sessionにEmulation.setDeviceMetricsOverrideを送り、Emulation.clearDeviceMetricsOverrideで戻します。Browser.setWindowBoundsは使わず、Edgeの他tabやウィンドウ境界は変更しません。CDP override中は対象pageのsessionを保持し、viewportを調べるdrag/計測もそのsessionを共有します。返り値の`effective_viewport`にはPage.getLayoutMetricsで確認したCSS viewport width/heightと、CDPへ適用したdevice scale factorを含みます。`original_viewport`は元のCSS width/heightを返し、Page.getLayoutMetricsでは元のdevice scale factorを取得できないためその値はnullです。`device_scale_factor_readback_available`はfalseです。実Edge fixtureではCSS resolution media queryとscreenshot pixel寸法で指定DSFを確認します。

`browser_reset_viewport(session_id, tab_handle, expected_url="", expected_url_prefix="")`は対象tabのoverrideをclearし、復元後のviewportを返します。overrideは`browser_close_tab`、`browser_detach`、session closeでも解除します。detach時にtab自体は残りますが、overrideを解除してからlogical sessionを解放します。計測中のtabは変更できず、`browser_viewport_measurement_active`で拒否します。

主要エラーは`browser_set_viewport_tab_not_owned`, `browser_reset_viewport_tab_not_owned`, `browser_tab_closed`, 各toolのurl_guard_required/url_guard_invalid/url_mismatch、browser_set_viewport_width_invalid、browser_set_viewport_height_invalid、browser_set_viewport_device_scale_factor_invalid、browser_set_viewport_failed、browser_reset_viewport_failed、browser_viewport_measurement_active、browser_viewport_cleanup_failedです。

`browser_capture_frames_start/stop` と `browser_track_boxes_start/stop` は、自分の `browser_open_tab` が返したowned tabだけを読み取ります。attached caller、同じcallerのowned `tab_handle`、`expected_url`（完全一致）または `expected_url_prefix`（HTTP(S) originとpath prefix。query/fragmentなし）が必要です。既存tab、別callerのtab、isolated sessionは `browser_*_tab_not_owned`、close済みtabは `browser_tab_closed`、URLが実行前またはサンプル前に変わった場合は `browser_*_url_mismatch` で拒否します。URL guardの照合とサンプルはpage evaluateを使わず、Playwrightのlocator読み取りとCDP `Page.captureScreenshot` だけで行います。

各 `*_start` は先頭サンプルを取得した後に戻り、計測タスクはsession lockを保持せずバックグラウンドで続きます。呼び出し側はstartの直後に `browser_drag` を実行し、dragの後に同じmeasurement IDでstopを呼べます。計測は指定durationまたはframe/sample上限まで続きます。1 attached callerにつき同時に4件までです。ページを閉じる前にstopを呼んでください。

**時系列スクリーンショット**

- `browser_capture_frames_start(session_id, tab_handle, expected_url|expected_url_prefix, interval_ms=100, max_frames=60, duration_ms=3000, clip_selector="", clip_rect=null, format="jpeg", quality=45, flicker_diff_threshold=0.004, vertical_shift_threshold_px=4, out_subdir="")`
- `expected_url`と`expected_url_prefix`の片方だけを渡します。`clip_selector`はCSSで1要素に一致し、現在viewport内にある必要があります。`clip_rect`は `{x,y,width,height}` のviewport座標です。selectorとrectは同時に指定できません。計測は画面をscrollしません。
- `interval_ms`は16–1000、`max_frames`は1–120、`duration_ms`は100–10000です。画像形式は`jpeg`または`png`、JPEG qualityは20–90、差分率閾値は0.0001–0.5、縦移動閾値は0.5–500 pxです。既定画像はJPEG quality45で、保存前に最大1280×720へ縮小します。1 frame 8 MiB、1回の画像合計32 MiBを超えると以後の保存を拒否します。
- `browser_capture_frames_stop(session_id, measurement_id)` は `sample_count`, `frames`（相対monotonic `t_ms`, ファイル名、寸法、byte数）, 隣接frame `comparisons`, 低頻度移動も拾う`window_comparisons`, `analysis`, manifest path, interval統計, `error_code`を返します。比較値は160×90 grayscale thumbnailの輝度差、差分bbox/連結領域、推定縦移動量です。window comparisonは実`t_ms`を見て、最低max(200ms, interval×3)離れたframeを比べます。`flicker_detected`は閾値以上の同じ静止領域で差分が複数回繰り返された場合、`vertical_jitter_detected`は閾値以上の縦移動が2回以上あり方向反転を観測した場合にtrueです。画像座標のpxは縮小保存後の画像pxです。
- frame取得loopはPage.captureScreenshotの生画像bytesと取得時刻を記録し、後処理の縮小・保存・比較はloop終了後に行います。各slotは開始時刻+n×intervalの絶対時刻に揃え、遅れたslotをskipして`dropped_frames`に加算します。1回のcapture失敗はerror_code付きslotとして記録して次へ進み、連続5回失敗で停止します。生画像bytesの合計は32 MiBまでです。`requested_interval_ms`, `actual_interval_ms`（min/median/p95/max）, `capture_duration_ms`（median/max）, `dropped_frames`, `failed_frames`, `warning`を返し、manifestにも保存します。実間隔medianが要求の1.5倍を超えるとwarningは`interval_not_achievable`です。比較は各frameの実t_msを使い、screencast方式は未対応です。

**要素のbounding box時系列**

- `browser_track_boxes_start(session_id, tab_handle, selectors, expected_url|expected_url_prefix, interval_ms=50, max_samples=60, duration_ms=3000, vertical_jitter_threshold_px=4, out_subdir="")`
- `selectors`はCSS selectorの配列で最大20個です。各selectorは最大2,000文字、複数一致はDOM順で最大10個/selector、全selector合計50要素まで追跡します。
- interval/duration/sample上限はframesと同じです。`vertical_jitter_threshold_px`は0.5–500 pxです。`browser_track_boxes_stop(session_id, measurement_id)` は `elements`を返します。selector文字列は返さず、`selector_index`と`element_index`だけで対応します。各sampleは `{t_ms,x,y,width,height,visible}` で、座標とサイズはCSS pxです。集計の`y_max_variation_px`, `y_reversal_count`, `disappear_reappear_count`, `max_jump_px`を使って縦揺れと点滅を確認できます。点滅判定は一度見えた要素が消えた後に再表示された回数、縦揺れ判定はy変動幅が閾値以上かつ方向反転があることを示します。

両toolの画像とmanifestは `.runtime/<out_subdir>/browser-measurements/<caller-hash>/<kind>-<UTC>-<id>/` に保存されます。`out_subdir`は相対pathだけで、絶対path、drive、`..`、`.runtime`外へ解決されるsymlinkは拒否します。既定は `.runtime/browser-measurements/` 以下です。古い測定ファイルは自動削除しません。スクリーンショットには画面上の秘密が写り得るため、合成fixtureや許可されたQA-owned tabに限定し、保存先を共有しないでください。出力とmanifestにはcookie、token、本文、selector文字列を含めません。

f18ff1a8向け呼び出し順の例です。fixture URLとCSS selectorは対象に合わせます。owned tabをopenしてviewportを設定し、2つの計測を開始、drag後にstop、viewport reset、最後にtabを閉じます。

```python
opened = await browser_open_tab(session_id)
owned_tab_handle = opened["tab_handle"]
fixture_url = "http://127.0.0.1:3002/at/qa/fixture"
await browser_navigate(session_id, fixture_url)
await browser_set_viewport(
    session_id=session_id,
    tab_handle=owned_tab_handle,
    width=1600,
    height=1000,
    device_scale_factor=1.5,
    expected_url=fixture_url,
)
frames = await browser_capture_frames_start(
    session_id=session_id,
    tab_handle=owned_tab_handle,
    expected_url_prefix="http://127.0.0.1:3002/at/qa/fixture",
    interval_ms=50,
    max_frames=80,
    duration_ms=4500,
    clip_selector="#board",
    out_subdir="f18ff1a8",
)
boxes = await browser_track_boxes_start(
    session_id=session_id,
    tab_handle=owned_tab_handle,
    selectors=[".task-row", ".drag-overlay"],
    expected_url_prefix="http://127.0.0.1:3002/at/qa/fixture",
    interval_ms=50,
    max_samples=80,
    duration_ms=4500,
    out_subdir="f18ff1a8",
)
drag = await browser_drag(
    session_id=session_id,
    tab_handle=owned_tab_handle,
    source_selector=".task-row:first-child",
    target_selector=".drop-zone",
    expected_url_prefix="http://127.0.0.1:3002/at/qa/fixture",
    steps=16,
    settle_ms=250,
)
frame_result = await browser_capture_frames_stop(session_id, frames["measurement_id"])
box_result = await browser_track_boxes_stop(session_id, boxes["measurement_id"])
await browser_reset_viewport(session_id, owned_tab_handle, expected_url=fixture_url)
await browser_close_tab(session_id, owned_tab_handle)
```

主な拒否コードは `browser_capture_frames_tab_not_owned`, `browser_track_boxes_tab_not_owned`, `browser_tab_closed`, 各toolの`url_guard_required`, `url_guard_invalid`, `url_mismatch`, `browser_measure_output_path_invalid`, `browser_measure_active_limit`, `browser_measure_frame_size_limit`, `browser_measure_total_size_limit`, `browser_capture_frames_consecutive_failures`, `browser_capture_frames_clip_*`, 各toolの`*_viewport_unavailable`, `browser_track_boxes_selector_*`, `interval_invalid`, `sample_limit_invalid`, `duration_invalid`です。stop結果の`error_code`も確認してください。URL不一致やCDP取得失敗は部分manifestとともに未完了として返ることがあります。

### JSON応答のnavigationとJev click

`browser_navigate(session_id, url, wait_until="domcontentloaded", timeout_sec=30)` はapplication/json・text/plainの応答にも通常のnavigationを行い、statusとquery/fragmentを除いたURLを返します。302後のstatus/URLは最終応答です。Content-Dispositionでdownloadが始まった場合はファイルを保存せず、`code=browser_navigation_download` を返します。`browser_run_plan` のstart_urlで同じdownloadを検出した場合は `reason=browser_navigation_download` でfailedになります。

JSONの内容を読む必要がなければ、attached callerで次のようにdone stepだけを使います。明示的なread stepは応答本文を結果へ含めるため、必要な場合だけ使います。

```python
browser_run_plan(
    session_id="<attached caller session>",
    start_url="http://127.0.0.1:3002/at/api/example",
    steps=[{"action": "done"}],
    completion_url="http://127.0.0.1:3002/at/api/example",
    max_steps=1,
    close_on_finish=False,
)
```

Jev clickのstep例:

```python
browser_run_plan(
    session_id="<attached caller session>",
    steps=[{"action": "click", "instruction": "Click Continue", "expected_text": "completed"}],
    completion_text="completed",
    max_steps=1,
    action_timeout_sec=5,
    close_on_finish=False,
)
```

同じlabel/placeholderの候補が複数ある場合は、element stepに `within`, `test_id`, `nth` を必要なものだけ追加できます。`within` は祖先のnamed `region` / named `section` のaccessible name（例: `"AoiDots composer"`）かCSS scope（`#chat-panel`, `.composer`, または任意selectorに使う `css:form`、最大512文字）です。accessible nameを明示する場合は `region:名前` prefixも使えます。`test_id` は対象要素の `data-testid` と完全一致し、最大256文字です。`nth` は絞り込み後の候補順に対する0始まり番号（0–99）です。`nth`を省略したときに最初の候補を自動採用することはありません。各条件は併用でき、指定なしなら従来どおりJevが候補を選びます。

```python
browser_run_plan(
    session_id="<attached caller session>",
    steps=[{
        "action": "fill",
        "instruction": "Message",
        "value": "fixture text",
        "within": "AoiDots composer",
    }],
    max_steps=1,
)
```

`browser_click`と`browser_fill`も同じ `within`, `test_id`, `nth` を省略可能引数として受け付けます。`selector`自体は従来どおり必須のCSS selectorです。例えば `browser_fill(session_id, "textarea[placeholder='Write a message']", value, test_id="chat-composer")` で、selector一致要素をdata-testidでも絞れます。直接toolに絞り込みを渡さない場合は、従来のlocatorがそのまま使われます。filter文字列はJev候補データへ追加されません。絞り込みで候補が0件なら `browser_candidate_not_found`、不正なscope CSSなら `browser_within_invalid`（直接click/fillは `browser_element_filter_invalid`）で止まります。複数候補を絞らずJevが曖昧と判断した場合は、従来どおり `jev_low_confidence` 等になります。

Jev MCPには決め打ちの `browser_click`, `browser_fill`, `browser_keyboard` と、boundedなDOM読み取り `browser_find` もあります。attached Edgeでは4 toolとも、同じcallerの `browser_open_tab` で作成され現在選択されているowned tabと、必須の `guard_url`（完全一致）または `guard_url_prefix`（同一originとpath prefix）を検証します。未所有/未選択tabは `browser_<tool>_tab_not_owned`、URL不一致は `browser_<tool>_url_mismatch`、guard欠落は `browser_<tool>_url_guard_required` です。`browser_click`の既存`expected_url`はclick後のpostcondition用なので、添付Edgeの事前guardには `guard_url` / `guard_url_prefix` を使います。`browser_fill`値は戻り値・telemetry logに含まれません。`browser_find`は最大50件で、form controlの本文/value属性を返さず属性をallowlistします。全体のrendered textを返す`browser_page_text`はJev MCPには公開しません。

```python
await browser_fill(
    session_id=session_id,
    selector="textarea[placeholder='Write a message']",
    value="QA fixture text",
    test_id="chat-composer",
    guard_url_prefix="http://127.0.0.1:3002/at/qa/fixture",
)
await browser_keyboard(
    session_id=session_id,
    key="Enter",
    guard_url_prefix="http://127.0.0.1:3002/at/qa/fixture",
)
```

click候補がまだDOMに現れていない場合、Jevへ問い合わせる前に最大1秒だけ候補を再観測します。選択後はscroll-into-viewとPlaywrightのactionability待ちを使います。遮蔽・非表示・不可操作・timeoutなどは `browser_element_obscured`, `browser_element_not_visible`, `browser_element_not_actionable`, `browser_action_timeout`, `browser_element_unstable`, `browser_element_not_found`, `browser_click_failed` のreasonになります。操作結果が不明なclickは再実行しません。実行前にtargetがstaleだったと確認できた場合だけ、候補を取り直して最大1回retryします。`reason_detail` は例外typeと秘密を除いた診断文です。navigation例外も `browser_navigation_failed` としてsafe detailを返します。

### 複数callerが共有するEdgeの背景tab

複数callerが同じEdgeウィンドウでtabを開くと、先に開いたowned tabが背景になり、`document.visibilityState` が `hidden` になることがあります。背景tabでは `requestAnimationFrame` が止まり、Playwrightのclickがactionabilityのstable判定で待ち続ける場合があります。Gatewayはowned tabへのclick、fill、keyboard、dragとrun planのclick/fill/typeの直前にtabを前面化し、最大1秒だけvisibleへの変化を待ちます。clickがhidden tabで終わり、実行前のtimeoutと確認できた場合に限り、全体timeoutの範囲内で再試行します。最終的にもhiddenなら固定エラー `browser_click_tab_hidden` を返します。自動前面化により、Edgeでユーザーが見ていたtabが切り替わる副作用があります。

Edge brokerへの接続が失敗しても、隔離ブラウザーへ自動で切り替えません。Gatewayは41が管理するbrokerへ再照会します。`browser_run_plan` をセッションIDなしで呼ぶ既存の隔離実行動作は別経路として残っています。隔離実行を使う場合は`browser_start`を明示してから、そのセッションIDを渡してください。

タブ一覧はタイトルやページ本文を取得せず、安定ハンドルとURLだけを返します。URLのユーザー情報、query、fragmentは除去されます。添付モードの`browser_errors`は選択中ページだけを監視し、コンソール本文と例外メッセージを`[redacted]`にします。Jevプランは明示的な`read`ステップがない限り、結果本文を返しません。

Jevは外部推論サービスです。選択ページの操作候補ラベルはJevへ送られるため、認証ページの無関係な内容を対象にせず、合成したQAページを使ってください。JevにはHTML全体やフォームの入力値を渡しません。

### 接続要件

### brokerの管理

専用Edge brokerはAoiTalk（41）の`src/services/local_browser/cdp_broker.py`で動き、41が起動時に管理します。Gatewayの`home_mcp_gateway/edge_cdp_broker.py`はクライアント専用で、`%LOCALAPPDATA%\AoiTalk\browser-broker\control.json`と`control.authkey`を読み、認証付きWindows named pipeへ`status`または`ensure_browser`を送ります。GatewayはbrokerやEdgeを起動・停止しません。brokerから受け取るWebSocket endpointはloopbackに限定し、endpointや鍵をログ・例外へ出しません。

旧Scheduled Task `Home MCP Edge CDP Broker` が残っている場合は、`scripts/retire_edge_cdp_broker_task.ps1`で停止・無効化してください。スクリプトは登録アクションが旧Gateway brokerの`pythonw`であることを確認し、該当する旧brokerプロセスだけを停止します。Edgeプロセスは停止しません。

41が動いていない場合、named pipeへ接続できず`browser_attach_edge`は固定エラー`edge_broker_unavailable`を返します。接続に失敗しても隔離ブラウザーへ自動切替しません。`browser_attach_edge(timeout_sec=...)`はbrokerへの問い合わせ待機を1〜60秒で指定でき、応答に応じて`edge_broker_connecting`などの固定コードを返します。

ローカルのAoiTalkへは、パスワードを使わず`python scripts/seed_dedicated_edge_login.py --user-id <uuid>`でセッションcookieを入れます（41側の`frontend/scripts/issue-local-browser-session.mjs`が通常のセッションを発行し、値はメモリ上だけでbroker経由のcookie jarへ渡します。有効期間は7日なので週に1回再実行します）。

GatewayはEdgeの起動・終了、プロファイル変更、debug設定変更、Cookie/Storage取得、拡張機能インストール、同意ダイアログ受諾を行いません。組織ポリシーが接続を無効にしている場合は、その制限を迂回しません。

この機能は汎用Gatewayと `Home MCP Jev Browser` 専用MCPの両方で公開されます。専用Jev MCPはattach/open/list/select/close/detachとJev付きプラン実行だけを公開します。
