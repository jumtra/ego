# self-evolve-harness 1.4.1 スタンドアロン実装

**本ZIPには設計書だけでなく、実行可能なPythonソース、CLI、4ホスト向けテンプレート、サンプル、テストを含めています。**

これは `jumtra/self-evolve-harness` のGit checkoutではなく、本会話の要件に基づいた独立した実装です。既存プロジェクトを上書きする前に、`init`の差分表示とGitバックアップで確認してください。

## 必要なもの

- Python 3.11以上（macOS/Linux）。Pythonパッケージの実行自体にAgent SDK・外部APIは不要です。
- 実際にAIにコーディングさせる場合のみ、**Claude Code / Codex / OpenCode の対応CLI・ログイン設定**が別途必要です。
- **AGYは設定・Skill・Agentファイル生成のみ対応**。外部CLIによる自動呼び出しは利用条件を理由に無効化しています。
- CodeGraph・Graphify・Engram MCP・GitHub CLIは選択制。未導入時に存在すると偽りません。

## 1分で試す（pip不要）

```bash
unzip seh_v141_autoupdate.zip
cd seh_v141_autoupdate
python3 run_seh.py --version
python3 run_seh.py modules list
python3 run_seh.py optimize status
```

他の既存プロジェクトへ導入するときは、まず**変更内容を確認**します。

```bash
python3 run_seh.py init --root /path/to/project --profile balanced --hosts claude,codex,opencode,agy --graph none
python3 run_seh.py init --root /path/to/project --profile balanced --hosts claude,codex,opencode,agy --graph none --apply
python3 run_seh.py doctor --root /path/to/project
```

`balanced`は不要な常駐フックや自動LLM呼び出しを追加しません。最適化は初期OFFです。

## CLIをオフラインでインストールする（任意）

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index ./dist/self_evolve_harness_standalone-0.0.1-py3-none-any.whl
seh --version
```

これでホスト内のSkillから`seh`コマンドを呼び出せます。pipを使わない場合は`./bin/seh`または`python3 run_seh.py`が使用できますが、ホストから`seh`として呼ぶなら`bin`をPATHへ追加してください。

## オフラインの実動デモ

サンプルを作業用ディレクトリへコピーして、DAG実行と評価を試せます。

```bash
cp -R examples/demo_project /tmp/seh-demo
python3 run_seh.py init --root /tmp/seh-demo --apply
python3 run_seh.py work run --root /tmp/seh-demo --task /tmp/seh-demo/work-task.json --host mock --apply --approve
python3 run_seh.py eval run --root /tmp/seh-demo --suite /tmp/seh-demo/eval-suite.json --host mock
cat /tmp/seh-demo/result.txt
```

`mock`はオフライン検証用の決定論的テストドライバーで、AIモデルではありません。

## 実装した主な領域

| 領域 | 概要 |
|---|---|
| 導入・同期・移行 | 検出、差分プレビュー、所有権、スナップショット、復元、不要Skillの安全な削除 |
| ホスト対応 | Claude/Codex/OpenCode/AGYのSkill・Agent・Instructions生成、CLI Runnerアダプター |
| スキル整理 | 初期構成2 Skill・4 Agent。新しいSkillをむやみに追加しない |
| 開発 | TaskSpecのDAG、依存関係、並列実行、テスト、回数制限付き修正、失敗証拠、明示的な適用 |
| 品質 | Eval First、4種類のFixture、比較、出力契約、Skill/PromptのLint・承認付き昇格 |
| 自己改善 | 観測→提案→独立評価→承認→昇格→ロールバック、Evidence付きLesson |
| Harness² | 任意のLite比較、およびOpt-in Fullの候補生成・Probe・Compose。通常タスクでは実行しない |
| Tool/Provider | JSON入力検証付きツール、FileMemory、Engram接続アダプター、CodeGraph/Graphify/ソース検索切替 |

## 動作確認と残る限界

配布前に自動テストと、初期化・DAG実行・Evalの実動デモを実行しています。**実ホストでのAIモデルの作業成功率や起動時間は未検証**です。実ホストCLI・MCP/CodeGraph/Graphify・`gh`の実環境接続テストは、各バイナリ・認証情報がないためできていません。CLIから実行するコードはOSの安全なサンドボックスで隔離されたわけではありません。オプトインの`--trust-cli`と`--trusted-local`はリスクを理解した場合だけ使用してください。金額の強制上限を保証できない場合、`require_metered`設定は実行を拒否します。

不確定な2件目X投稿のオプション機能は、内容が確定できていないため推測で実装していません。

詳細は [README.md](README.md)、[機能一覧](docs/IMPLEMENTATION_MATRIX.md)、[制約と互換性](docs/COMPATIBILITY.md)、[セキュリティ](docs/SECURITY.md)、[テスト](docs/TESTING.md) を参照してください。

## v1.4.1: Task単位のHost/Model選択と更新機構

- **Task/NodeでHost・Modelを指定**: `goal`・`instructions_file`・`nodes[].prompt`・`host`・`model`・`models`、DAG並列、条件付きFallback、Compare。
- **CLIのバージョン差分検出**: `seh hosts inspect` / `seh hosts refresh`。実行ファイル変更で自動再検査。未知の互換性差分は安全に停止。
- **Model一覧更新**: `seh models list`（TTLで自動更新）/ `seh models refresh`。`model=auto`はHostの最新デフォルトに委譲します。
- **自動化の許可モード**: `--autonomy auto`が通常既定。危険な`--yolo`/`--dangerously-skip-permissions`は外部Sandbox下での明示的な使用に限定。
- **更新コマンド**: v1.4.1以降は`harness update --check` / `harness update` / `harness update --yes --sync --root PROJECT`。公開リリースと検証可能なWheelが前提。
- **Python以外のコードも対象**: TypeScript、Go、Rust、C#、Swift、Javaなどのプロジェクト検出と検証コマンド候補を拡充。

詳細: [Host/Task/Model/更新の完全仕様](docs/HOST_TASK_MODEL_UPDATES.md) / [Task JSON例](examples/multihost/task.json)。

## 最新CLI・ハーネスを更新する場合

```bash
# 互換性情報を再取得（通常のTaskではモデル一覧を更新しない）
harness hosts refresh
harness models refresh --host opencode

# 現在公開されている最新版を確認
harness update --check

# 公開済みの検証可能なwheelがある場合のみ、確認付きで更新
harness update

# 更新後にプロジェクトの管理対象ファイルも再同期
harness update --yes --sync --root /path/to/project
```

- 外部のClaude/Codex/OpenCode自体はこれらのコマンドで更新しません。各ベンダーの正規アップデーターで更新してください。更新後、実行ファイルの変化を検知してCLI引数を再評価します。
- 未知の引数変更に対しては動作を推測せず停止します。対応版を公開後、`harness update`で導入してください。
- `harness update`はこの独立版のホイールが `jumtra/self-evolve-harness` のGitHub Releasesへ正しいタグとダイジェスト付きで公開されて初めて利用できます。**現状、リモート更新の成功は未検証**です。
- 低摩擦の既定値は`--autonomy auto`です。`--yolo` / `--dangerously-skip-permissions` 相当を使う場合は、外部で隔離した環境に限定し、`--autonomy bypass --attest-external-sandbox --trust-cli` を指定してください。
- 既存プロジェクト内のユーザー管理ファイルは自動で上書きせず、差分・バックアップ・ロールバックを確認してください。
