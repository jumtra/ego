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
isolation = "auto"
cleanup_worktree = true
git_exclude = true
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

### 管理実行（`ego work run`）

| キー | 既定値 | 範囲 | 効果 |
|---|---|---|---|
| `models` | `{claude = {model = "sonnet", effort = "auto"}, codex = {model = "auto", effort = "medium"}, opencode = "auto", agy = {model = "auto", effort = "auto"}}` | ホスト → モデル | ホストごとの既定モデル。[モデル](#モデル) を参照 |
| `max_parallel` | `2` | 1〜8 | 独立したノードを同時に実行する数 |
| `max_attempts` | `3` | 1〜5 | ノードごとの試行回数の上限（初回 + 修正） |
| `timeout_seconds` | `3600` | 5〜7200 | ノードごとの上限。run 全体の時間制限はありません |
| `max_rounds` | `2` | 0〜5 | reviewer が 1 ノードで差し戻せる回数。使い切ったら `on_exhausted` に従う |
| `max_replans` | `1` | 0〜5 | task 1 件あたりの計画の作り直し回数 |
| `max_run_tokens` | `0` | `0` または 1000 以上 | 1 回の実行（`run`/`resume`）あたりの使用トークン上限（既定 `0` = 無制限。1000 以上を指定した場合のみ `resume` ごとにリセットで上限停止。`cache_read` は除外） |
| `max_turns` | `30` | 1〜200 | ワーカー 1 回の呼び出しにおけるエージェントのターン数 |
| `worker_context` | `"lean"` | `lean`、`inherit` | `lean` は Claude ワーカーを MCP サーバーなしで起動。`inherit` はそのまま引き継ぎます |
| `worker_segment_turns` | `10` | 1〜200 | Claude ワーカーをこのターン数ごとの区間で実行し、区間の間に実測コンテキストを `clear_suggest_tokens` と比べます（Codex / OpenCode / AGY はストリーム/セッション監視）。上限に達すると checkpoint を保存（他ホストで未保存時は機械的フォールバック）し、新しいセッションで続きから再開します |
| `isolation` | `"auto"` | `auto`、`copy`、`worktree` | run の作業場所。`auto` は同時に書き込むノード（互いに依存関係のない coder/tester/code/worker ノード）が 2 つ以上ある run だけ Git worktree、それ以外はステージング用コピー。`worktree` は常に worktree、`copy` は常にコピー。`--worktree` で run ごとに上書き |
| `cleanup_worktree` | `true` | bool | run 後に worktree を削除。`--keep-worktree` で run ごとに上書き |
| `git_exclude` | `true` | bool | `apply`/`sync`/`update` が `.ego/` と生成ファイルを `.git/info/exclude`（ローカルのみ・コミットされない）に追加します。すでに追跡されているファイルは追跡されたままなので、外すには `git rm -r --cached` を使います。`false` にすると git の無視設定に触れず、`.ego/config.toml` や spec を自分でコミットできます |
| `allow_commands` | 一般的なツールチェーン | コマンド名のリスト | `checks` と `--done-check` が起動してよい実行ファイル。シェルと `python -c` は常に拒否されます |

### セッション

| キー | 既定値 | 範囲 | 効果 |
|---|---|---|---|
| `clear_suggest_tokens` | `200000` | `0` または 20000〜2000000 | このコンテキストサイズに達したらセッションをチェックポイント保存してクリアします。各 CLI の生成フック（[セッション](sessions.md) を参照）と `ego run` が使います |
| `usage_switch_percent` | `99` | `0` または 50〜99 | Claude Code・Codex・AGY（使用上限を報告する CLI。OpenCode は報告しない）のみ。5 時間枠または 7 日枠の使用量がこの割合に達したら、チェックポイントを保存し、別の CLI への切り替えを提案します（`0` でオフ） |
| `run_yolo` | `true` | bool | `ego run` が CLI を権限確認スキップのスイッチ付きで起動します。`--yolo` / `--no-yolo` で run ごとに上書き |

詳細は [セッション](sessions.md) を参照してください。

## 細かいパラメーター（`.ego/defaults.toml`）

上限と既定値の多くは、パッケージ同梱の `src/ego/defaults.toml` から読み込まれます。プロジェクトごとに変えるには `.ego/defaults.toml` に `[tuning]` や各機能セクション（`[usage]`、`[lifecycle]`、`[loopguard]`、`[toolprune]`、`[mcp]`、`[analysis]`）を書きます。CLI フラグでこれらを上書きすることはできません。`.ego/config.toml` に新しいキーがなくても、パッケージの既定値で動きます。

### `[tuning]`

| キー | 既定値 | 用途 |
|---|---|---|
| `max_feedback_chars` | `4000` | 再試行に渡す、チェック出力と reviewer の指摘の文字数 |
| `max_summary_chars` | `800` | イベント、チェックポイント、再計画の履歴に載せる要約の文字数 |
| `max_record_chars` | `20000` | `record.json` に残す試行ごとの出力の文字数 |
| `max_context_hits` | `8` | 計画時のコード検索の結果件数 |
| `max_save_turns` | `3` | チェックポイント保存のターン数 |
| `max_handoff_chars` | `1000` | ノード間で渡す handoff の文字数 |
| `max_role_candidates` | `8` | 役割ごとのルーティング候補数の上限 |
| `[tuning.plan_schema]` | 同梱の `defaults.toml` を参照 | 計画・タスクスキーマの文字数・項目数の上限（`max_goal_chars` など 12 キー） |
| `[tuning.session_schema]` | `max_goal_chars` 800、`max_item_chars` 350、`max_notes` 8、`max_prompt_chars` 1600 | チェックポイントと再開プロンプトの上限 |

### `[usage]`

| キー | 既定値 | 効果 |
|---|---|---|
| `track_cache_tiers` | `true` | キャッシュ作成トークンを 5m / 1h の層ごとに分けて集計する |
| `track_overhead_separately` | `true` | 使用量台帳で `agent` と `overhead`（ego 制御側）を分けて記録する |
| `session_cost_warning_usd` | `5.0` | セッションコストの警告しきい値（USD） |

### `[lifecycle]`

| キー | 既定値 | 効果 |
|---|---|---|
| `enable_envfacts` | `true` | セッション終了後の fold で環境ファクト（EnvFacts）の抽出とライフサイクル更新を有効にする |
| `min_confirmations` | `2` | `candidate` から `active` に昇格するために必要な別セッションでの成功回数 |
| `contradiction_demote_threshold` | `2` | `active` から `demoted` に降格する失敗（矛盾）回数 |
| `max_injected_facts` | `12` | プロンプト末尾に注入する `active` ファクトの最大件数 |
| `max_injected_chars` | `2400` | プロンプト末尾に注入する `active` ファクトの最大文字数 |
| `alter_feedback` | `true` | `demoted` または `forgotten` 時に `alter_knowledge_feedback` を送信する（`knowledge = "alter"` 時） |

### `[loopguard]`

既定では `dry_run = true` で出荷されます（停滞を検知して `loopguard` 進捗イベントを記録するだけで、nudge や `STAGNATION` 停止は行いません）。実際に介入させるには、`.ego/defaults.toml` で次のように上書きします。

```toml
[loopguard]
dry_run = false
```

| キー | 既定値 | 効果 |
|---|---|---|
| `enabled` | `true` | LoopGuard による停滞検知を有効にする |
| `dry_run` | `true` | `true` の場合は観測とイベント出力のみを行い、nudge や停止を行わない |
| `envelope_min_samples` | `5` | リポジトリ固有のターン数エンベロープ（パーセンタイル）を使うために必要な最小サンプル数 |
| `envelope_percentile` | `95` | エンベロープ上限の算出に使うパーセンタイル |
| `min_turns` | `20` | 停滞判定を開始する最小アシスタントターン数（サンプル不足時のフォールバック基準も兼ねる） |
| `edit_churn` | `4` | 同一ファイルの繰り返し編集回数しきい値 |
| `distinct_edit_files` | `3` | 編集が分散した別ファイル数のしきい値 |
| `error_outputs` | `6` | エラー出力回数のしきい値 |
| `stagnation_ratio` | `0.2` | 失敗シグネチャの重複率（`1 - distinct / total`）しきい値 |
| `envelope_margin` | `1.25` | エンベロープ上限に掛ける余裕倍率（超過で判定対象） |
| `backstop_mult` | `2.5` | 強制停止のバックストップ倍率 |
| `max_nudges_per_node` | `1` | 1 ノードあたりに送る方向転換 nudge の最大回数 |
| `escalate_to_replan` | `true` | `STAGNATION` 到達時に再計画（replan）へのエスカレーションを許可する |

### `[toolprune]`

| キー | 既定値 | 効果 |
|---|---|---|
| `unused_days` | `14` | 未使用とみなす期間（日数） |
| `min_sessions` | `5` | 整理候補を提案するために必要な直近セッション数の最小値 |

### `[mcp]`

| キー | 既定値 | 効果 |
|---|---|---|
| `alter_worker_profile` | `"ego-worker"` | ワーカー起動時に alter MCP サーバーへ渡すツールプロファイル名 |

### `[analysis]`

| キー | 既定値 | 効果 |
|---|---|---|
| `min_user_chars` | `400` | セッション後分析の対象とするユーザー発話の最小文字数 |

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
roles = {reviewer = {host = "agy", model = "gemini-2.5-pro", effort = "high"}, coder = {host = "claude", model = "sonnet"}}
```

- キーは `coder`、`tester`、`reviewer`。各テーブルは任意の `host`、`model`、`effort` を取ります（`hosts` / `models` と同じ語彙）。
- 省略した項目は `models` / `hosts` に従います。effort だけの指定はホストとモデルを保ったまま effort のみ設定します。
- 優先順位: ノード > タスク `defaults.roles` > `roles` > タスク `defaults` > `models`。`ego apply --roles '{"reviewer":{"host":"agy"}}'` で設定できます。

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
- **書く:** 自動。Stop フックと `ego session checkpoint` が、秘匿化した会話とチェックポイントの要点を alter に送ります（`alter_record_conversation`、`alter_session_digest`。alter 側で確認を経ます）。`EGO_ALTER_AUTO_REVIEW=0` で無効にできます。このモードでは `ego knowledge ...` はエラーです。
- **alter に接続できないとき:** SessionStart と UserPromptSubmit のフックが、黙らずに `ego ERROR: alter に接続できません (...)` をユーザーとエージェントに伝えます。ターンはブロックしません。`ego doctor` の `knowledge_health` でも確認できます。

`memory` は `knowledge` キーの旧名です。`memory = "..."` が残る古い `.ego/config.toml` は `knowledge` として読まれ、次の `ego apply` で書き換えられます。`--memory` は `--knowledge` の別名として引き続き使えます。

### セッション後の分析（`alter_analysis`）

オプトインです。`knowledge = "alter"` のときだけ有効になります。モデル費用が発生するため、既定では無効です。

`alter_analysis = true` にすると、alter に記録済みのセッションが終了した後に分析されます。ego が分析ホストを headless で（`ego session analyze <sid>`、切り離し実行で）起動し、`alter-session-analyzer` スキルを実行して、分析結果を同じ記録済みの会話 ID に付与します。分析はセッション終了後に行われるため、セッション終了をブロックしません。失敗は無視されます（fail open）。手動では `ego session analyze <sid>` で実行できます。

| キー | 既定 | 範囲 | 効果 |
|---|---|---|---|
| `alter_analysis` | `false` | bool | 上記のセッション後分析を有効にする |
| `alter_analyzer` | `{opencode = {model = "opencode/glm-5.3-flash-free"}}` | ホスト → `{model = "..."}` の表 | ホスト別の分析モデル。一覧にないホストはセッションのホストを使うため、上書きしない限り分析ホストはセッションのホストです |
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
