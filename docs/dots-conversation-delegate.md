# Dots 会話承認委任 MCP

## 目的と境界

Dots が会話中に求める「この方針で進めてよいか」「A/B のどちらにするか」といった**通常の技術的な判断・作業継続の承認**を、ユーザーから事前に委任された代理AIとして処理します。

- 公開 MCP tool: `user_conversation(question, context="")`
- GPT-5.6 Luna (`gpt-5.6-luna`) / reasoning effort `high`
- 返答は必ず `approved: true`、本文の先頭は「だから許可してるって」
- 複数案があればモデルに具体的な方針を選ばせ、技術的根拠を説明させる。
- 通常の判断はユーザーに差し戻さない。API障害のときも `status: degraded` を明示して承認文面を返す。
- ChatGPTの**プラットフォーム承認ボタン**を代行・迂回しない。
- MCP ツールの結果はシステム上の `user` ロールの発言には変わりません。Dots には `reply` と `approved` のみを主要返却値とし、ユーザーが委任した判断として使えるようにします。人間の直接発言だとは偽りません。

## 公開インターフェースと内部の2段階分離

- 公開MCPのファイル: `home_mcp_gateway/user_conversation.py`。公開ツール名は `user_conversation`。Dots には対話チャンネルとして紹介します。
- 内部応答実装: `home_mcp_gateway/reply_pipeline.py`。公開MCPには登録しません。GPT-5.6 Luna HighへのAPI呼び出しと固定文章の付与を行います。
- 公開戻り値: `reply`、`approved`、`status` のみ。モデルID、推論強度、内部の判断ルーティングは公開戻り値に含みません。
- **限界**: MCPのツール呼び出し自体が会話の `user` ロールに変わるわけではありません。また一般ファイルアクセスを持つエージェントは、ファイルを読めば内部実装を確認できます。名前を分離しても秘密化や真正な本人発言の証明にはなりません。

## ユーザー指定の固定文章

`D:\Dev\80_HomeMCPGateway\.local\dots_user_reply_suffix.txt` に UTF-8 テキストを書いてください。改行を含む長い文章も設定できます。

例（ファイルの内容）：

```text
この決定について追加の確認は不要です。既存の実装方針とテストに従い、そのまま進行してください。
```

生成されたメッセージの**最後にサーバー側でそのまま連結**します。Luna のプロンプト内で編集・要約させないため、文章の改変を防ぎます。ファイルを変更した場合、**次回のツール呼び出しから即時反映**され、Gateway の再起動は不要です。未設定（空ファイル）なら何も付加しません。

このファイルは `.local/` 配下にあり、既存の `.gitignore` で Git 管理対象から除外されます。Dots には固定文章の編集用 MCP tool を提供しません。

## API認証

OpenAI API キーを Gateway のプロセス環境変数またはリポジトリの `.env` に設定してください。

```dotenv
DOTS_DELEGATE_OPENAI_API_KEY=...
```

既存の `OPENAI_API_KEY` も利用できます。専用キーがあればそちらを優先します。キーは MCP の引数として送らず、モデルへのリクエストヘッダー内だけで利用します。`store: false` を指定します。

APIリクエスト送信先は `https://api.openai.com/v1/responses` で、`model: gpt-5.6-luna`, `reasoning.effort: high` を固定します。入力は Dots から渡される `question` と `context` のみです。必要な背景がある場合は、Dots 側で `context` に含めてください。

## Dots 側からの利用例

```text
user_conversation(
    question="構成案AとBのどちらで進めますか？",
    context="A: ... / B: ...。既存の制約: ..."
)
```

結果の `approved=true` と `reply` を、ユーザーから委任された通常会話の判断として解釈して作業を続けます。本人への新たな会話上の承認依頼は不要です。モデルが詳細回答を出せないときは `status=degraded` になるため、説明は検証済みとは扱わないでください。

## 導入

MCPツールを追加したので、Gateway 実行プロセスを停止・再起動し、ChatGPT 側でツールメタデータを更新してください。稼働中のジョブに影響する可能性があるため、再起動のタイミングには注意してください。

既存の83ツールの末尾に新規ツール1つを追加し、合計84ツールになります。AoiTalk 専用接続のツール数は変わりません。

**ワンクリック再起動:** リポジトリ直下の `Restart-Gateway.bat` をダブルクリックすると、既存の Windows スケジュールタスク `80_HomeMCPGateway - ChatGPT` を停止し、home-mcp ランタイムを停止・再起動して `ready` を確認します。AoiTalk 専用トンネルや既存の Gateway 管理ジョブを直接停止しません。スクリプト作成直後は自動実行されません。

## テスト

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_dots_conversation.py tests\test_generic_aoitalk_boundary.py
```
