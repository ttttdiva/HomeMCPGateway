# Muse Workspace MCP の運用

この文書は、`muse_workspace_mcp` を既存の Muse Review MCP と入れ替えて運用するための手順です。サーバーは登録簿に登録した作業用 worktree または読み取り専用 snapshot だけを公開します。Muse から見える URL と tool の名前は workspace を切り替えても変わりません。

## 前提

- サーバーは `<python-path>\python.exe` で起動する。
- `workspaces.json` は既存の秘密ファイルと同じ owner-only の場所に置く。registry と監査ログは `<state-root>\secrets\workspaces.json` と `<state-root>\audit.jsonl` に置く。
- `secret` は URL の秘密部分、`token` は `Authorization: Bearer` の値であり、どちらもファイルから読み込む。コマンドラインと環境変数には秘密を置かない。
- worktree は `<repo-root>\.worktrees\muse-<task>` の形式で作り、対象作業の branch を使う。main / master や main の作業ツリーは登録しない。
- サーバーは `127.0.0.1` にだけ bind し、既存と同じ ingress の `https://<public-host>/muse-mcp/<path-secret>/mcp` から公開する。

## 登録簿の初期化と workspace の切り替え

管理 CLI は Muse に公開せず、ローカルの担当セッションだけで実行する。

```powershell
$py = '<python-path>\python.exe'
$packageRoot = '<package-root>'
$repoRoot = '<repo-root>'
$env:PYTHONPATH = $packageRoot
Set-Location $packageRoot
$state = '<state-root>'
$secrets = Join-Path $state 'secrets'
$registry = Join-Path $secrets 'workspaces.json'
$secretFile = Join-Path $secrets 'path.secret'

& $py -m muse_workspace_mcp.admin register `
  --registry $registry `
  --secret-file $secretFile `
  --id '<task-id>' `
  --kind worktree `
  --path (Join-Path $repoRoot '.worktrees\muse-<task>') `
  --repo-label '<repository-label>' `
  --checks pytest ruff `
  --ttl-hours 24

& $py -m muse_workspace_mcp.admin list --registry $registry --secret-file $secretFile
& $py -m muse_workspace_mcp.admin use --registry $registry --secret-file $secretFile --id '<task-id>'
```

既存の snapshot を登録する場合は、snapshot root を明示する。snapshot は常に読み取り専用である。

```powershell
$snapshots = Join-Path $state 'snapshots'
& $py -m muse_workspace_mcp.admin register `
  --registry $registry `
  --secret-file $secretFile `
  --id review-snapshot `
  --kind snapshot `
  --path (Join-Path $snapshots 'review-snapshot') `
  --snapshots-root $snapshots `
  --read-only `
  --ttl-hours 24
```

`register` は realpath、linked worktree、branch、期限、symlink / junction を検証する。snapshot を登録する場合は `--kind snapshot` とし、`writable=false` が強制される。期限切れ、登録解除済み、存在しない id は、Muse の tool 呼び出しで固定エラーになる。登録簿は各 tool 呼び出しで再読込されるため、切り替えのためにサーバーを再起動する必要はない。

不要になった登録は、作業が終わってから次で削除する。

```powershell
& $py -m muse_workspace_mcp.admin unregister --registry $registry --secret-file $secretFile --id '<task-id>'
```

登録簿を直接編集する場合も、owner-only の権限と JSON のバックアップを維持する。`active` を登録されていない id にしない。

PowerShell では次のように登録簿と秘密ディレクトリを現在のユーザーだけに許可し、継承を外してから確認する。既存の ACL を確認してから適用する。

```powershell
icacls $secrets
icacls $registry /inheritance:r /grant:r "${env:USERNAME}:(F)"
icacls $secrets /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)(F)"
icacls $registry
```

## 起動

既存の `Start-MuseMcp.ps1` が読む秘密ファイルをそのまま利用する。引数には秘密本文を渡さない。

```powershell
& $py -m muse_workspace_mcp.server `
  --workspaces-file $registry `
  --audit (Join-Path $state 'audit.jsonl') `
  --secret-file (Join-Path $secrets 'path.secret') `
  --token-file (Join-Path $secrets 'token.secret') `
  --port 8765 `
  --prefix /muse-mcp `
  --ttl-minutes 0
```

本番では既存のスケジュールタスク `MuseReviewMcp` を変更する前に、同じ引数を別の検証用タスクまたは別 port で確認する。既存 port 8765 の稼働中プロセスを停止して検証しない。`checks_enabled` は既定で false のままにする。

## 旧サーバーからの切り替え

1. 新しい worktree を作成し、`admin register` で登録する。
2. 別 port の検証用プロセスで、`list_workspaces`、読み取り、書き込み、git tool、認証を確認する。
3. `Start-MuseMcp.ps1` の呼び出し対象を `python -m muse_workspace_mcp.server` に差し替えたコピーを作り、起動前に package worktree へ `Set-Location` する。secret file / token file / prefix が旧設定と同じであることを確認する。
4. `Register-MuseMcpTask.ps1` の登録内容を確認してから、運用の切り替え時刻にだけタスクを更新する。この作業は本 job の変更対象ではない。
5. Muse の connector は同じ URL と同じ tool 名を維持するため、既存の「常に許可」の承認をそのまま使える。workspace の id は tool 引数または `use_workspace` で選ぶ。

同じ URL を二つのプロセスが同時に listen しないよう、切り替え時だけ旧プロセスを停止して新プロセスを起動する。認証失敗が出た場合は ingress と秘密ファイルを変更せず、監査ログの `_auth` 記録だけを確認する。

## ロールバック

1. Muse の connector が使用する URL、secret、token を変更しない。
2. `Start-MuseMcp.ps1` の対象を旧 `review_mcp.py` に戻す。
3. 必要なら `Register-MuseMcpTask.ps1` の旧引数でタスクを再登録する。稼働中の新プロセスを停止する操作は、運用担当が切り替えを決めた後だけ実施する。
4. 新サーバーの登録簿は削除せず、監査ログとともに保全して原因を調べる。作業完了後に `unregister` で期限切れ登録を整理する。

## run_check とサンドボックス

`run_check` は実装されているが、サーバー設定 `checks_enabled=false` の間は必ず `checks_disabled` を返す。許可される検査は登録 entry の `checks` にある `pytest`、`ruff`、`tsc` だけで、指定できるパスと引数も検査ごとの固定 allow-list に制限する。shell、任意の環境変数、秘密を含む環境、worktree 外の path は渡さない。

有効化する場合は、チェック実行を `Runner` に注入し、MCP サーバーの権限から隔離した実装を先に用意する。候補は次のとおり。

- Windows の制限付き token と Job Object: 導入しやすく、CPU / メモリ / 子プロセスを制限できる。Windows の同一カーネル上なので、特権設定や既存の共有資源に対する隔離が完全ではない。
- WSL2: Linux の user / namespace / filesystem を使える。Windows 側ファイルや共有 mount、ホスト連携機能を明示的に制限しないと境界を越える。
- コンテナ（Windows では Linux VM / Docker Desktop を含む）: 再現性と filesystem / network の deny-by-default を作りやすい。Docker daemon と共有 mount 自体が強い権限を持つため、daemon 操作者と設定を別途保護する必要があり、起動コストもある。

現状の推奨は、まず制限付き token + Job Object を採用し、ネットワークを無効、作業用の一時ディレクトリだけを read-write、worktree は必要な範囲だけを mount として検証すること。その境界を測定するまで `checks_enabled=false` を維持し、実装済みの `Runner` は fake を使うテストだけで確認する。

## 監査・制限・残る前提

監査ログは tool 名、workspace id、相対 path、サイズ、結果コードだけを記録し、ファイル内容、パッチ本文、環境変数、秘密を記録しない。認証拒否は固定の `_auth` 記録になる。read / write / patch / git の各呼び出しでは、相対 path、`.git`、拒否名、symlink / junction / reparse point、realpath の最終的な包含を検証する。

`write_file` は UTF-8 の NUL なしテキストを 256 KiB 以下、atomic replace、ファイル数とレートの上限付きで扱う。path の親階層は最大 6 階層まで検査でき、1 回の write で新規作成する親 directory は最大 4 個である。深さ超過、拒否名、予約デバイス名、alternate data stream、`.git`、`..` は拒否する。write が失敗した場合は、その呼び出しで作成した空の親 directory だけをロールバックする。

`apply_patch` は全 path を検査し、rename / copy / binary / mode change を拒否したうえで `git apply --check` 後にだけ適用する。CRLF の target に LF の patch を適用する場合は、通常の check が失敗したときだけ `--ignore-whitespace` を付けた check を試し、成功時は同じ引数で適用する。実際の context が異なる patch は失敗し、成功後も target の CRLF bytes を保持する。改行コードの保持で読み込む既存 target は 4,000,000 bytes 以下に限り、それを超える target は変更前に `limit_exceeded` で拒否する。fallback でも外部 path、`.git`、拒否名、binary、mode、rename の policy は緩和しない。git の read-only tool は固定 argv、`--no-pager`、`--no-ext-diff`、textconv 無効、hook 無効で実行する。

write tool で生成されたコードは、人間が内容と diff を確認してから commit する。サーバー自体は commit、push、merge、branch 操作、任意 shell、worktree 外の読み書きを提供しない。秘密ファイル、credential、cookie、token、state root、`.env` 系、鍵、DB、`private`、`.claude`、`.codex` は read / write とも拒否する。

パスは各呼び出しで realpath と包含を検証するが、Windows の directory handle に相対した操作ではない。別のローカルプロセスが検証と open / replace / Git 操作の間に親 directory を junction へ差し替える race は残る。この脅威も防ぐ必要がある運用では、handle-relative な Windows filesystem API を追加するまで workspace の同時外部変更を避ける。
