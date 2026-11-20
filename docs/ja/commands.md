# コマンドリファレンス

[ドキュメント索引](README.md) · [English](../en/commands.md)

`ego <command> --help` で、インストール済みバージョンのフラグを確認できます。

## 共通事項

- **プロジェクトルート:** `--root <path>` を指定しない限り、すべてのコマンドはカレントディレクトリに対して動作します。`--root` はコマンドの後、またはネストしたアクションの後に置けます（例: `ego work run --root /tmp/app ...`）。
- **出力:** stdout には JSON を出力します。エラーは stderr に `{"status": "ERROR", "reason_code": "...", "message": "..."}` の形式の JSON で出力されます。
- **既定は dry run:** プロジェクトファイルを書き込むコマンド（`init`、`sync`）は、`--apply` がない限りプレビューのみです。`ego apply` は代わりに確認を求めます。
- **ホスト:** `claude`、`codex`、`opencode`、`agy`、加えて、実行や評価のためのオフラインの `mock` ホスト。

| 終了コード | 意味 |
|---|---|
| 0 | 成功 |
| 2 | 入力、タスク、または設定が不正 |
| 3 | 前提条件の欠如: CLI が未インストール、レコードが見つからない、隔離が証明されていない |
| 4 | ポリシーによる拒否、または承認の欠如 |
| 5 | チェックの失敗、保留中のドリフト（`--check`）、ブロックされたプロジェクト、またはインフラエラー |

## インストールとプロジェクト

詳細: [セットアップ](setup.md)

| コマンド | 用途 |
|---|---|
| `ego apply [flags]` | プロジェクトを設定し、ホスト向けファイルを生成。フラグ: `--hosts`、`--profile`、`--policy`、`--models`、`--graph`、`--knowledge`（`--memory` は別名）、`--runtime`、`--clear-suggest-tokens`、`--detect-tests`、`--install-alter-skills`、`--roles`、`--plan-schema`、`--session-schema`、`--dry-run`、`--check`、`--interactive`、`--non-interactive` |
| `ego status` | ドリフトの状態、導入済みと実行中のバージョン、最後のスナップショット |
| `ego sync [--apply] [--all]` | `.ego/config.toml` から再生成。`--all` は登録済みの全プロジェクトを対象にする |
| `ego rollback [snapshot]` | 直前の（または指定した）apply のスナップショットを復元 |
| `ego uninstall [-y] [--cli]` | プロジェクトから ego を削除、または CLI のシンボリックリンクを削除 |
| `ego init [--apply]` | プロジェクト検出を伴う、非対話のプレビュー / 作成。フラグ: `--profile`、`--hosts`、`--graph`、`--knowledge`、`--detect-tests`。`/harness-init` skill が使用 |
| `ego projects list` / `scan <dir> [--depth N]` / `add <path>...` / `remove <path>...` | 適用済みプロジェクトのレジストリ |
| `ego update [--check] [--yes] [--sync] [--sync-all]` | ego 自体を更新。ターミナルでは適用待ちのプロジェクト設定を適用するか尋ねる（このプロジェクトの差分 / 全プロジェクト / 適用しない）。オフライン: `--wheel <file> --sha256 <digest>` |
| `ego doctor` | ホスト CLI、コード解析、メモリ、サンドボックス、skill の lint |
| `ego hosts inspect` / `refresh [--host <cli>]` | インストール済み CLI のバージョンと検出されたフラグ（キャッシュ） |
| `ego models list [--host <cli>] [--no-refresh]` / `refresh [--host <cli>]` | ホストごとのモデルカタログ |

## 作業

詳細: [ワークフロー](workflows.md)

| コマンド | 用途 |
|---|---|
| `ego work plan --goal <text> [--host <h>] [--scope <globs>] [--out <file>] [--execute]` | タスク JSON を作成。実在のホストには `--execute` が必要。`mock` ではオフラインで 1 ノードの計画を作成 |
| `ego work run --task <file> [flags]` | タスク JSON を隔離環境で実行 |
| `ego work resume <run_id> --task <file> [flags]` | run を続行、または検証済みの run を `--apply --approve` で適用 |
| `ego work status <run_id> [--verbose]` | run を表示 |
| `ego work cancel <run_id>` | 中止を要求 |
| `ego work native` | native モードとは CLI 内で `/work` を使うことだと報告する。モデル呼び出しは行わない |

`run` / `resume` のフラグ: `--host`、`--trust-cli`、`--apply`、`--approve`、`--hosts`、`--strategy`、`--model`、`--models`、`--autonomy`、`--attest-external-sandbox`、`--worktree`、`--keep-worktree`、`--verbose`。


## セッション

詳細: [セッション](sessions.md)

| コマンド | 用途 |
|---|---|
| `ego run <claude\|codex\|opencode\|agy> [--clear-tokens N] [--yolo\|--no-yolo] [-- <cli args>]` | ego がチェックポイント、クリア、再開を行う CLI を起動（しきい値到達時） |
| `ego session checkpoint --task-id <id> --goal <text> [notes] [--status <s>] [--kind <k>] [--host <cli>]` | チェックポイントを保存。メモ: `--completed`、`--next`、`--decision`、`--blocker`、`--evidence` |
| `ego session list [--all]` / `status <task_id>` | チェックポイントを一覧、または表示 |
| `ego session resume [task_id] [--host <cli>] [--from <cli>]` | クリア指示と再開プロンプトを表示。`--from` は、クリアせずに別の CLI のチェックポイントを続ける |
| `ego session handoff [task_id] --to <cli> [--from <cli>] [--launch]` | 別の CLI で続けるための起動コマンド |

## メモリとコード

| コマンド | 用途 |
|---|---|
| `ego knowledge add <text> [--topic <slug>] [--summary <text>] [--keyword <w>]...` | `knowledge = "file"`: 検証済みのナレッジを記録。先に index を読み、合致するトピックファイルに統合し、合致がないときだけ新規作成 |
| `ego knowledge index` / `search <words>` / `get <slug>...` | `knowledge = "file"`: index を読む、トピックを探す、トピックファイルを表示。`knowledge = "alter"` では `ego knowledge` はすべてエラー |
| `ego memory add <text> [--tags <t>]... [--evidence <ref>]...` | 処理に関する事実を保存（常にローカル、`.ego/memory/items.json`） |
| `ego memory search <query>` / `list` / `delete <id>` | 処理に関する事実を検索、または管理 |
| `ego memory install-skills [--scope global\|project] [--clients <list>] [--no-agents] [--dry-run]` | `npx` 経由で外部ツール `alter-skills`（`github:jumtra/alter-skills`）をインストール |
| `ego code doctor` | プロバイダの利用可否と機能 |
| `ego code search <query>` / `symbol <name>` | 検索。組み込みのテキスト検索にフォールバック |
| `ego code impact <name>` / `affected <files>...` | 影響範囲と影響を受けるテスト（プロバイダが必要） |
| `ego code index [--execute]` | プロバイダのインデックス作成コマンドを表示。`--execute` を付けると実行 |

## 拡張と内部

| コマンド | 用途 |
|---|---|
| `ego hook <pre-tool\|stop\|session-start\|statusline\|context-probe>` | 生成されたホストのフックの入口。フックのペイロードを stdin から読む。`--host claude|codex|opencode|agy`（既定 `claude`）でペイロードと出力の形式を選ぶ。手動で実行するものではない |
| `ego --version` | バージョン |
