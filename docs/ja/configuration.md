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
memory = "file"
runtime_mode = "native"
max_parallel = 2
max_attempts = 3
timeout_seconds = 3600
max_turns = 30
worker_context = "lean"
worker_segment_turns = 10
clear_suggest_tokens = 200000
usage_switch_percent = 0
run_yolo = false
models = {claude = {model = "sonnet", effort = "medium"}, codex = "auto"}
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
| 管理実行で各 CLI が使うモデル | `models` | 空のままにすると各 CLI の既定モデルを使います |
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
| `memory` | `"file"` | `file`、`engram` | `ego memory ...` の裏で動くプロバイダ。[メモリ](#メモリ) を参照 |
| `runtime_mode` | `"native"` | `native`、`managed` | 記録される既定の作業方式。コマンドの動作は変えません。CLI 内の `/work` は native、`ego work run` は常に managed です |
| `tests` | `[]` | argv 配列のリスト | `--detect-tests` が記録するテストコマンド。例: `[["pytest", "-q"]]` |

### 管理実行（`ego work run`、`ego work loop`）

| キー | 既定値 | 範囲 | 効果 |
|---|---|---|---|
| `models` | `{}` | ホスト → モデル | ホストごとの既定モデル。[モデル](#モデル) を参照 |
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
| `usage_switch_percent` | `0`（オフ） | `0` または 50〜99 | Claude Code と Codex（使用上限を報告する CLI）のみ。5 時間枠または 7 日枠の使用量がこの割合に達したら、チェックポイントを保存し、別の CLI への切り替えを提案します |
| `run_yolo` | `false` | bool | `ego run` が CLI を権限確認スキップのスイッチ付きで起動します。`--yolo` / `--no-yolo` で run ごとに上書き |

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

## メモリ

| プロバイダ | 保存先 | 備考 |
|---|---|---|
| `file` | プロジェクト内の `.ego/memory/items.json` | ローカルのみ。`add`、`search`、`list`、`delete` をサポート |
| `engram` | 自分で動かす外部 MCP サーバー | `add` と `search` をサポート。下記の環境変数が必要です。ファイルメモリへのフォールバックはありません |

チェックポイントはメモリ項目とは別に、常に `.ego/memory/handoffs/` に保存されます。

## 最適化の設定

`ego optimize`（オプトインの prompt・skill 実験）は、`ego optimize configure ... --apply` が書き出す `.ego/optimization.toml` を読みます。既定ではオフで、通常のコマンドは読み込みません。

| キー | 既定値 | 範囲 |
|---|---|---|
| `mode` | `"off"` | `off`、`analyze`、`shadow`、`assisted` |
| `profile` | `"lite"` | `lite`、`full` |
| `cost_policy` | `"manual_consent"` | `manual_consent`、`require_metered` |
| `max_candidates` | `1` | 1〜8 |
| `max_rounds` | `1` | 1〜5 |
| `max_probe_runs` | `2` | 0〜40 |
| `max_wall_seconds` | `900` | 30〜7200 |
| `max_candidate_bytes` | `8192` | 100〜32768 |
| `require_human_approval` | `true` | `true` のまま変更不可 |

## 環境変数

| 変数 | 用途 |
|---|---|
| `EGO_HOME` | プロジェクトレジストリ（`projects.json`）のディレクトリ。既定 `~/.config/ego` |
| `EGO_CACHE_DIR` | 検出した CLI の機能のキャッシュ。既定 `~/.cache/ego` |
| `EGO_ENGRAM_MCP_COMMAND` | Engram MCP サーバーを起動するコマンドライン（`memory = "engram"` に必須） |
| `EGO_ENGRAM_WRITE_TOOL` | `ego memory add` が使う MCP ツール名 |
| `EGO_ENGRAM_SEARCH_TOOL` | `ego memory search` が使う MCP ツール名 |
| `CLAUDE_CONFIG_DIR`、`CODEX_HOME`、`XDG_DATA_HOME` | `ego run` が各 CLI のセッション記録を読むときに参照 |

`EGO_RUN` は `ego run` が起動する CLI 向けに設定する変数です。手動で設定しないでください。
