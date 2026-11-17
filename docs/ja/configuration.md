# 設定

[ドキュメント索引](README.md) · [English](../en/configuration.md)

プロジェクトの設定は **`.ego/config.toml`** にあり、`ego apply` が作成します。セクションのない平らな TOML ファイルです。編集後は `ego sync --apply` でホスト向けファイルを再生成してください。未知のキーや範囲外の値は `INVALID_CONFIG` で拒否されます。

## 例

```toml
schema_version = 1
profile = "balanced"
policy_mode = "relaxed"
hosts = ["claude", "codex", "opencode", "agy"]
code_intelligence = "none"
knowledge = "alter"
runtime_mode = "native"
max_parallel = 2
max_attempts = 3
timeout_seconds = 3600
max_turns = 30
worker_context = "lean"
worker_segment_turns = 10
clear_suggest_tokens = 200000
usage_switch_percent = 99
run_yolo = true
models = {claude = {model = "sonnet", effort = "auto"}, codex = {model = "auto", effort = "medium"}, opencode = "auto", agy = {model = "auto", effort = "auto"}}
isolation = "copy"
cleanup_worktree = true
tests = []
allow_commands = ["python", "python3", "pytest", "git", "ruff", "npm", "pnpm", "yarn", "bun", "node", "deno", "tsc", "cargo", "rustc", "go", "dotnet", "swift", "swiftc", "cmake", "ctest", "mvn", "gradle", "gradlew", "java", "javac", "ruby", "bundle", "php", "composer"]
```

## 決めておくとよい項目

ほとんどのキーは既定値のままで構いません。決めておく価値があるのは次の項目です。

| 決めること | キー | 指針 |
|---|---|---|
| 使用する CLI | `hosts` | 列挙した CLI にだけ skill、agent、指示が生成されます |
| AI が無人でどこまでできるか | `policy_mode` | 共有リポジトリや重要なリポジトリでは `strict`。AI に依頼されたら push や設定編集をしてよい個人プロジェクトでは `relaxed` |
| 管理実行で各 CLI が使うモデル | `models` | 既定では `claude` (`sonnet`)、`codex` (`auto`, `medium`)、`opencode` (`auto`)、`agy` (`auto`) を設定しています |
| 長いセッションをいつクリアするか | `clear_suggest_tokens` | 既定: `200000`。`0` でオフ |
| チェックで実行してよいコマンド | `allow_commands` | 使っているテストランナーが一覧にない場合は追加してください |

## 全キー

### 生成

| キー | 既定値 | 値 | 効果 |
|---|---|---|---|
| `schema_version` | `1` | `1` | 設定形式のバージョン |
| `profile` | `"balanced"` | `minimal`、`balanced`、`full` | `full` は Claude Code の `PreToolUse` フック（`ego hook pre-tool`）を追加し、Bash コマンドを検査します。`minimal` と `balanced` は現時点で同じファイルを生成します |
| `hosts` | 4 つすべて | `claude`、`codex`、`opencode`、`agy` | ファイルを生成する CLI。ルーティングの設定ではありません。タスクを実行するホストはタスクごとに選ばれます |
| `policy_mode` | `"relaxed"` | `strict`、`relaxed` | [ポリシーモード](#ポリシーモード) を参照 |
| `code_intelligence` | `"none"` | `none`、`codegraph`、`graphify` | `ego code ...` の裏で動くプロバイダ。外部ツールを ego が導入することはありません。ない場合は組み込みのテキスト検索を使います |
| `knowledge` | `"alter"` | `file`、`alter` | プロジェクトのナレッジの置き場所（旧 `memory`）。[ナレッジとメモリ](#ナレッジとメモリ) を参照 |
| `runtime_mode` | `"native"` | `native`、`managed` | 記録される既定の作業方式。コマンドの動作は変えません。CLI 内の `/work` は native、`ego work run` は常に managed です |
| `tests` | `[]` | argv 配列のリスト | `--detect-tests` が記録するテストコマンド。例: `[["pytest", "-q"]]` |

### 管理実行（`ego work run`、`ego work loop`）

| キー | 既定値 | 範囲 | 効果 |
|---|---|---|---|
| `models` | `{claude = {model = "sonnet", effort = "auto"}, codex = {model = "auto", effort = "medium"}, opencode = "auto", agy = {model = "auto", effort = "auto"}}` | ホスト → モデル | ホストごとの既定モデル。[モデル](#モデル) を参照 |
| `max_parallel` | `2` | 1〜8 | 独立したノードを同時に実行する数 |
| `max_attempts` | `3` | 1〜5 | ノードごとの試行回数の上限（初回 + 修正） |
| `timeout_seconds` | `3600` | 5〜7200 | ノードごとの上限。run 全体の上限はこの値 × ノード数 |
| `max_turns` | `30` | 1〜200 | ワーカー 1 回の呼び出しにおけるエージェントのターン数 |
| `worker_context` | `"lean"` | `lean`、`inherit` | `lean` は Claude ワーカーを MCP サーバーなしで起動。`inherit` はそのまま引き継ぎます |
| `worker_segment_turns` | `10` | 1〜200 | Claude ワーカーをこのターン数ごとの区間で実行します。区間の間に実測コンテキストを `clear_suggest_tokens` と比べ、上限なら checkpoint を保存して新しいセッションで続きから再開します |
| `isolation` | `"copy"` | `copy`、`worktree` | run の作業場所。ステージング用コピーか Git worktree か。`--worktree` で run ごとに上書き |
| `cleanup_worktree` | `true` | bool | run 後に worktree を削除。`--keep-worktree` で run ごとに上書き |
| `allow_commands` | 一般的なツールチェーン | コマンド名のリスト | `checks` と `--done-check` が起動してよい実行ファイル。シェルと `python -c` は常に拒否されます |

### セッション

| キー | 既定値 | 範囲 | 効果 |
|---|---|---|---|
| `clear_suggest_tokens` | `200000` | `0` または 20000〜2000000 | このコンテキストサイズに達したらセッションをチェックポイント保存してクリアします。各 CLI の生成フック（[セッション](sessions.md) を参照）と `ego run` が使います |
| `usage_switch_percent` | `99` | `0` または 50〜99 | Claude Code と Codex（使用上限を報告する CLI）のみ。5 時間枠または 7 日枠の使用量がこの割合に達したら、チェックポイントを保存し、別の CLI への切り替えを提案します（`0` でオフ） |
| `run_yolo` | `true` | bool | `ego run` が CLI を権限確認スキップのスイッチ付きで起動します。`--yolo` / `--no-yolo` で run ごとに上書き |

3 つの詳細は [セッション](sessions.md) を参照してください。

## ポリシーモード

| | `strict` | `relaxed` |
|---|---|---|
| AI への指示 | リモートを変更する Git 操作と機微な設定編集は、ユーザーが手動で行う必要がある | ユーザーが依頼した `git push`、PR 作成、設定編集を AI が自律的に行ってよい |
| チェックコマンドでの `git push` / `reset` / `clean` / `rebase` | 拒否 | 許可 |
| `PreToolUse` フック（Profile `full`） | 破壊的なシェルコマンドと、`.ego/ownership.json`、`.ego/locks/`、`.git/` の編集をブロック | 破壊的なシェルコマンドのみブロック |

破壊的なパターン（`rm -rf /`、`curl | sh`、`mkfs` など）は両モードでブロックされます。ポリシーは助言的なもので、OS のサンドボックスではありません。[セキュリティ](security.md) を参照してください。

## モデル

```toml
models = {claude = "sonnet", codex = "auto", opencode = "anthropic/claude-sonnet-4-5", agy = {model = "auto", effort = "high"}}
```

- 値はモデル ID の文字列か、テーブル `{model = "...", effort = "..."}` です。
- `"auto"` は「CLI 自身の現在の既定」を意味し、ego はモデルのフラグを渡しません。
- `effort` は `auto`、`low`、`medium`、`high`、`xhigh`、`max` のいずれかです。
- CLI が受け付ける ID を使ってください。`ego models list --host <cli>` で ego が読み取れたカタログを確認できます。

これらは管理実行とプランナーの既定値です。タスク JSON や `--model` / `--models` フラグがあればそちらが優先されます（[ワークフロー](workflows.md#ホストとモデルの選び方) を参照）。対話中の CLI セッションのモデルは変わりません。

### 役割別ルーティング（`roles`）

```toml
roles = {judge = {host = "agy", model = "gemini-2.5-pro", effort = "high"}, coder = {host = "claude", model = "sonnet"}}
```

- キーは `coder`、`tester`、`judge`。各テーブルは任意の `host`、`model`、`effort` を取ります（`hosts` / `models` と同じ語彙）。
- 省略した項目は `models` / `hosts` に従います。effort だけの指定はホストとモデルを保ったまま effort のみ設定します。
- 優先順位: ノード > タスク `defaults.roles` > `roles` > タスク `defaults` > `models`。`ego apply --roles '{"judge":{"host":"agy"}}'` で設定できます。

ホスト別・複数指定: role の中にホスト名をキーとして書けます。`host` で使うホストを選びます（未指定なら実行時の既定ホスト）。

```toml
roles = {coder = {host = "claude", claude = [{model = "haiku", effort = ["low", "middle"]}, {model = "sonnet"}], codex = {model = "gpt-5-codex", effort = "medium"}}}
```

- 指定は `{model, effort}` テーブル、またはそのリストです。`effort` はリスト（`["low", "middle"]`）にでき、各要素が候補になります。上の例は haiku/low, haiku/medium, sonnet/auto です。`middle` は `medium`、省略または `auto` はホスト既定です。
- 候補が2つ以上ある場合は `fallback` として実行し、`UNAVAILABLE` / `TIMEOUT` / `RATE_LIMIT` で次の候補を試します（最大8件）。ノード自身が `model`/`models` を指定した場合はホスト別指定を無視します。

## ナレッジとメモリ

保存するものは 2 種類あり、それぞれ置き場所が違います。

| 内容 | 保存先 | 備考 |
|---|---|---|
| **プロジェクトのナレッジ**（ドメイン用語、用語集、決定事項） | `knowledge = "file"`: `.ego/knowledge/`<br>`knowledge = "alter"`: alter MCP サーバー | `knowledge` キーで選択。フック、`ego knowledge ...`（file）または alter のツール（alter）で読み書きします |
| **処理メモリ**（作業そのものに関する事実） | `.ego/memory/items.json` | `knowledge` に関わらず常にローカル。`ego memory add` / `search` / `list` / `delete` |

チェックポイントはさらに別で、常に `.ego/memory/handoffs/` に保存されます。

### `knowledge = "file"`

```
.ego/knowledge/
  index.md          トピックごとに 1 行（slug・要約・キーワード）。最初に読む
  topics/<slug>.md  読み込みの単位。1 トピックの事実
```

- **読む:** `CLAUDE.md` / `AGENTS.md` が「まず `index.md` を読み、タスクに必要なトピックファイルだけ開く」よう指示します（`ego knowledge search <words>`、`ego knowledge get <slug>`）。managed ノードにも、index があれば同じ指示が入ります。
- **書く:** `ego knowledge add '<fact>' [--topic <slug>] [--keyword <word>]...` は、先に index を読みます。既存トピックに合致する事実（キーワード一致、または共通語が十分）はそのファイルに統合され、同一の行はスキップされ、より詳しい行は短い方を置き換えます。合致するトピックがない場合だけ、新しいファイルと index 行を作ります。
- **自動記録:** `ego session checkpoint` が、チェックポイントの `decisions` を同じ方法でストアに書きます（モデルは呼びません。goal の単語がキーワードになります）。`EGO_KNOWLEDGE_AUTO_RECORD=0` で無効にできます。
- 以前のバージョンの単一ファイル `.ego/knowledge/domain.md` は、読み取り専用のトピック `domain` として index に表示されます。

### `knowledge = "alter"`

- **ルール:** `CLAUDE.md` / `AGENTS.md` と `.claude/rules/` に加えて、alter のプロジェクト AI 利用ルール（`alter_ai_usage`。セッション開始時に注入）。
- **読む:** プロンプトごとに `alter_task_context`（UserPromptSubmit、AGY は `PreInvocation`、OpenCode は `chat.message`）。managed 実行では alter MCP サーバーを使えるままにします（`lean = False`）。
- **書く:** 自動。Stop / SessionEnd フックと `ego session checkpoint` が、秘匿化した会話とチェックポイントの要点を alter に送ります（`alter_record_conversation`、`alter_session_digest`。alter 側で確認を経ます）。`EGO_ALTER_AUTO_REVIEW=0` で無効にできます。このモードでは `ego knowledge ...` はエラーです。
- **alter に接続できないとき:** SessionStart と UserPromptSubmit のフックが、黙らずに `ego ERROR: alter に接続できません (...)` をユーザーとエージェントに伝えます。ターンはブロックしません。`ego doctor` の `knowledge_health` でも確認できます。

`memory` は `knowledge` キーの旧名です。`memory = "..."` が残る古い `.ego/config.toml` は `knowledge` として読まれ、次の `ego apply` で書き換えられます。`--memory` は `--knowledge` の別名として引き続き使えます。

### セッション後の分析（`alter_analysis`）

オプトインです。`knowledge = "alter"` のときだけ有効になります。モデル費用が発生するため、既定では無効です。

`alter_analysis = true` にすると、alter に記録済みのセッションが終了した後に分析されます。ego が分析ホストを headless で（`ego session analyze <sid>`、切り離し実行で）起動し、`alter-session-analyzer` スキルを実行して、分析結果を同じ記録済みの会話 ID に付与します。分析はセッション終了後に行われるため、セッション終了をブロックしません。失敗は無視されます（fail open）。手動では `ego session analyze <sid>` で実行できます。

| キー | 既定 | 範囲 | 効果 |
|---|---|---|---|
| `alter_analysis` | `false` | bool | 上記のセッション後分析を有効にする |
| `alter_analyzer` | `{opencode = {model = "opencode/glm-5.3-flash-free"}}` | ホスト → `{model = "..."}` の表 | ホスト別の分析モデル。一覧にないホストはセッションのホストを使うため、上書きしない限り分析ホストはセッションのホストです |
| `alter_analysis_budget_usd` | `0.5` | 数値 ≥ 0 | 1回の分析の費用上限（USD） |
| `alter_analysis_timeout_seconds` | `600` | 整数 > 0 | 1回の分析の制限時間（秒） |

## 環境変数

| 変数 | 用途 |
|---|---|
| `EGO_HOME` | プロジェクトレジストリ（`projects.json`）のディレクトリ。既定 `~/.config/ego` |
| `EGO_CACHE_DIR` | 検出した CLI の機能のキャッシュ。既定 `~/.cache/ego` |
| `EGO_ALTER_MCP_COMMAND` | フックと `ego session checkpoint` が Alter MCP サーバーを直接呼び出す場合の任意の起動コマンド（既定: ホストが設定済みの alter サーバー） |
| `EGO_ALTER_AUTO_REVIEW` | `0` でフックと `ego session checkpoint` による会話・チェックポイントの alter への送信を止める（既定は有効） |
| `EGO_KNOWLEDGE_AUTO_RECORD` | `0` で `ego session checkpoint` による `.ego/knowledge` への決定事項の記録を止める（既定は有効） |
| `CLAUDE_CONFIG_DIR`、`CODEX_HOME`、`XDG_DATA_HOME` | `ego run` が各 CLI のセッション記録を読むときに参照 |

`EGO_RUN` は `ego run` が起動する CLI 向けに設定する変数です。手動で設定しないでください。
