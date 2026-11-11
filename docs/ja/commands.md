# コマンドリファレンス

[ドキュメント索引](README.md) · [English](../en/commands.md)

`ego <command> --help` で、インストール済みバージョンのフラグを確認できます。`seh` は `ego` のエイリアスです。

## 共通事項

- **プロジェクトルート:** `--root <path>` を指定しない限り、すべてのコマンドはカレントディレクトリに対して動作します。`--root` はコマンドの後、またはネストしたアクションの後に置けます（例: `ego work run --root /tmp/app ...`）。
- **出力:** stdout には JSON を出力します。エラーは stderr に `{"status": "ERROR", "reason_code": "...", "message": "..."}` の形式の JSON で出力されます。
- **既定は dry run:** プロジェクトファイルを書き込むコマンド（`init`、`sync`、`modules scaffold`、`optimize configure`）は、`--apply` がない限りプレビューのみです。`ego apply` は代わりに確認を求めます。
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
| `ego apply [flags]` | プロジェクトを設定し、ホスト向けファイルを生成。フラグ: `--hosts`、`--profile`、`--policy`、`--models`、`--graph`、`--memory`、`--runtime`、`--clear-suggest-tokens`、`--detect-tests`、`--dry-run`、`--check`、`--interactive`、`--non-interactive` |
| `ego status` | ドリフトの状態、導入済みと実行中のバージョン、最後のスナップショット |
| `ego sync [--apply] [--all]` | `.ego/config.toml` から再生成。`--all` は登録済みの全プロジェクトを対象にする |
| `ego rollback [snapshot]` | 直前の（または指定した）apply のスナップショットを復元 |
| `ego uninstall [-y] [--cli]` | プロジェクトから ego を削除、または CLI のシンボリックリンクを削除 |
| `ego init [--apply]` | プロジェクト検出を伴う、非対話のプレビュー / 作成。フラグ: `--profile`、`--hosts`、`--graph`、`--memory`、`--detect-tests`。`/harness-init` skill が使用 |
| `ego projects list` / `scan <dir> [--depth N]` / `add <path>...` / `remove <path>...` | 適用済みプロジェクトのレジストリ |
| `ego update [--check] [--yes] [--sync] [--sync-all]` | ego 自体を更新。オフライン: `--wheel <file> --sha256 <digest>` |
| `ego doctor` | ホスト CLI、コード解析、メモリ、サンドボックス、skill の lint、modules |
| `ego hosts inspect` / `refresh [--host <cli>]` | インストール済み CLI のバージョンと検出されたフラグ（キャッシュ） |
| `ego models list [--host <cli>] [--no-refresh]` / `refresh [--host <cli>]` | ホストごとのモデルカタログ |
| `ego migrate plan` / `remove-legacy --hashes <file> --approve` / `restore-legacy <snapshot> --approve` / `rollback <snapshot>` | 旧バージョンのハーネスのファイルを検出し、削除 |

## 作業

詳細: [ワークフロー](workflows.md)

| コマンド | 用途 |
|---|---|
| `ego work plan --goal <text> [--host <h>] [--scope <globs>] [--out <file>] [--execute]` | タスク JSON を作成。実在のホストには `--execute` が必要。`mock` ではオフラインで 1 ノードの計画を作成 |
| `ego work run --task <file> [flags]` | タスク JSON を隔離環境で実行 |
| `ego work resume <run_id> --task <file> [flags]` | run を続行、または検証済みの run を `--apply --approve` で適用 |
| `ego work status <run_id> [--verbose]` | run を表示 |
| `ego work cancel <run_id\|loop_id>` | 中止を要求 |
| `ego work loop --goal <text> --done-check <cmd> [flags]` | ゴールループ |
| `ego work loop-status <loop_id>` / `loop-apply <loop_id> --approve` | ループを確認、または適用 |
| `ego work native` | native モードとは CLI 内で `/work` を使うことだと報告する。モデル呼び出しは行わない |

`run` / `resume` のフラグ: `--host`、`--trust-cli`、`--apply`、`--approve`、`--hosts`、`--strategy`、`--model`、`--models`、`--autonomy`、`--attest-external-sandbox`、`--worktree`、`--keep-worktree`、`--verbose`。

`loop` のフラグ: `--host`、`--scope`、`--max-iterations`、`--max-budget-usd`、`--max-wall-seconds`、`--execute`、`--plan-first`、`--trust-cli`、`--apply`、`--approve`、`--autonomy`、`--attest-external-sandbox`。

## セッション

詳細: [セッション](sessions.md)

| コマンド | 用途 |
|---|---|
| `ego run <claude\|codex\|opencode\|agy> [--clear-tokens N] [--yolo\|--no-yolo] [-- <cli args>]` | ego がチェックポイント、クリア、再開を行う CLI を起動（しきい値到達時） |
| `ego session checkpoint --task-id <id> --goal <text> [notes] [--status <s>] [--host <cli>]` | チェックポイントを保存。メモ: `--completed`、`--next`、`--decision`、`--blocker`、`--evidence` |
| `ego session list [--all]` / `status <task_id>` | チェックポイントを一覧、または表示 |
| `ego session resume [task_id] [--host <cli>] [--from <cli>]` | クリア指示と再開プロンプトを表示。`--from` は、クリアせずに別の CLI のチェックポイントを続ける |
| `ego session handoff [task_id] --to <cli> [--from <cli>] [--launch]` | 別の CLI で続けるための起動コマンド |

## メモリ、コード、コンテキスト

| コマンド | 用途 |
|---|---|
| `ego memory add <text> [--tags <t>]... [--evidence <ref>]...` | 事実を保存 |
| `ego memory search <query>` / `list` / `delete <id>` | 事実を検索、または管理（`list` と `delete` はファイルプロバイダのみ） |
| `ego code doctor` | プロバイダの利用可否と機能 |
| `ego code search <query>` / `symbol <name>` | 検索。組み込みのテキスト検索にフォールバック |
| `ego code impact <name>` / `affected <files>...` | 影響範囲と影響を受けるテスト（プロバイダが必要） |
| `ego code index [--execute]` | プロバイダのインデックス作成コマンドを表示。`--execute` を付けると実行 |
| `ego context audit` | 常時読み込みと必要時読み込みのコンテキストを予算と比較。超過時は終了コード 5 |
| `ego events tail [-n N]` | 最近のイベント（既定 20 件） |

## スキルとプロンプトの改善

生成された skill やプロンプトを変更するコマンドです。常に評価と明示的な承認を経ます。通常の開発では開始されません。

| コマンド | 用途 |
|---|---|
| `ego eval run --suite <file> [--host <h>] [--trials N] [--model <id>] [--split <name>] [--timeout S]` | 評価スイートを実行。全ケースが合格しない限り終了コード 5 |
| `ego eval compare <baseline> <candidate>` | 2 つの結果を比較。昇格可能でない限り終了コード 5 |
| `ego author lint` | skill ファイルを検証 |
| `ego author plan --goal <text> [--skill work] [--field skill.description]` | skill の編集を計画 |
| `ego author candidate --replacement-file <file> --goal <text>` | 候補の変更を記録 |
| `ego author gate --candidate <file> --suite <file> [--host <h>] [--model <id>] [--out <file>]` | 候補をスイートで評価 |
| `ego author promote --candidate <file> --gate <file> --approval-ref <ref> --approve` | ゲートを通過した候補を採用 |
| `ego author rollback <promotion_id>` | 昇格を取り消す |
| `ego evolve observe <message> [--outcome FAIL] [--task-class <c>] [--evidence <ref>]...` | 観察結果を記録 |
| `ego evolve propose <observation_id> [--replacement-file <file>]` | 観察結果を候補に変換 |
| `ego evolve lesson <summary> --evidence <ref>...` | 根拠付きで教訓を記録 |
| `ego optimize status` / `doctor` | オプトインのオプティマイザーの状態 |
| `ego optimize configure [--mode ...] [--profile ...] [--cost-policy ...] [limits] [--apply]` / `disable [--apply]` | `.ego/optimization.toml` を変更（[設定](configuration.md#最適化の設定)） |
| `ego optimize plan --fixture <file>` / `propose <exp>` / `run <exp>` / `report <exp>` / `promote <exp> --approval-ref <ref> --suite <file> --approve` / `full --fixture <file>` | 実験を実行。モデル呼び出しには `--execute` が必要で、従量課金でない場合は `--spend-approval` も必要。検証済みのサンドボックスがない場合は `--trusted-local` も必要 |

`author gate` / `promote` の `--allow-mock-test` は、パイプラインをテストするためにオフラインの `mock` ホストを許可します。mock の結果は実在のモデルについての証拠ではありません。

## 拡張と内部

| コマンド | 用途 |
|---|---|
| `ego modules list` / `resolve <a,b,...>` / `scaffold <name> [--apply]` | 組み込みとプロジェクトの modules（`.ego/modules/<name>/module.toml`）、依存関係の解決 |
| `ego tool list` / `call <name> '<json args>' [--approve-external]` | 決定的なツール、および modules が宣言したツール |
| `ego prompt register <name> <file> [--role worker]` / `show <name> [--version V]` / `extract <file> [--contract json\|reply]` | バージョン管理されたプロンプトと、出力コントラクトの解析 |
| `ego pr create --title <t> --body <b> [--base main] --approve` / `reviews <number>` | `gh` CLI 経由のプルリクエスト操作 |
| `ego hook <pre-tool\|stop\|session-start\|statusline\|context-probe>` | 生成されたホストのフックの入口。フックのペイロードを stdin から読む。手動で実行するものではない |
| `ego bench [--trials N]` | ego 自身の CLI のディスパッチ時間を計測 |
| `ego --version` | バージョン |
