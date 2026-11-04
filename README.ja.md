# self-evolve-harness 1.4.2 スタンドアロン実装（多言語対応）

**本ZIPには設計書だけでなく、実行可能なPythonソース、CLI、4ホスト向けテンプレート、サンプル、テストを含めています。**

Pythonは**ハーネス本体の実行言語**です。開発対象はPythonに限らず、JavaScript/TypeScript、Go、Rust、Java/Kotlin、C#/F#、Swift、Ruby、PHP、Dart/Flutter、C/C++、Godot/Unity/Unrealなどに対応します。モノレポのサブプロジェクト単位でビルド・テスト候補を検出でき、未知の言語もTOMLで追加できます。詳細は [多言語対応ガイド](docs/LANGUAGE_SUPPORT.md) を参照してください。

これは `jumtra/self-evolve-harness` のGit checkoutではなく、本会話の要件に基づいた独立した実装です。既存プロジェクトを上書きする前に、`setup`の差分表示とGitバックアップで確認してください。

## 必要なもの

- Python 3.11以上（macOS/Linux）。Pythonパッケージの実行自体にAgent SDK・外部APIは不要です。
- 実際にAIにコーディングさせる場合のみ、**Claude Code / Codex / OpenCode の対応CLI・ログイン設定**が別途必要です。
- **AGYは設定・Skill・Agentファイル生成のみ対応**。外部CLIによる自動呼び出しは利用条件を理由に無効化しています。
- CodeGraph・Graphify・Engram MCP・GitHub CLIは選択制。未導入時に存在すると偽りません。

## かんたん導入（pip不要・対話式）

```bash
unzip seh_standalone_easy_setup.zip
cd seh_standalone_easy_setup
./setup.sh /path/to/your/project
```

セットアップは言語・モノレポ構成・インストール済みのAIコーディングCLIを自動検出し、Host・Profile・CodeGraph/Graphify・Memoryを選んで、**変更予定ファイルを確認してから適用**できます。非対話環境ではプレビューだけを表示し、ファイルを書きません。

```bash
./setup.sh /path/to/your/project --yes --hosts auto   # 明示承認で一括適用
./setup.sh /path/to/your/project --yes --hosts all    # 4ホスト向けファイルを生成
./bin/seh status --root /path/to/your/project       # 設定の確認
./bin/seh project checks --root /path/to/your/project # テスト候補（実行しない）
```

既存の`CLAUDE.md`・`AGENTS.md`は**専用管理ブロックだけ**追加し、ユーザーの内容を維持します。再実行は冪等で、衝突時は安全に停止します。元に戻すには:

```bash
./bin/seh uninstall --root /path/to/your/project     # 削除プレビュー
./bin/seh uninstall --root /path/to/your/project --yes
# 実行後に表示されるsnapshot IDで復元可能
./bin/seh migrate rollback snapshot-XXXXXXXXXXXX --root /path/to/your/project
```

**自動では行いません：** 外部ツールのインストール、AIモデルの呼び出し、プロジェクトのテスト実行、Harness²最適化の有効化。詳細は[かんたん導入ガイド](docs/EASY_SETUP.md)。

## CLIをオフラインでインストールする（任意）

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index ./dist/self_evolve_harness_standalone-1.4.2-py3-none-any.whl
seh --version
```

これでホスト内のSkillから`seh`コマンドを呼び出せます。pipを使わない場合は`./bin/seh`または`python3 run_seh.py`が使用できますが、ホストから`seh`として呼ぶなら`bin`をPATHへ追加してください。

## 多言語プロジェクトの検出・チェック

```bash
python3 run_seh.py project detect --root /path/to/project
python3 run_seh.py project checks --root /path/to/project
# レビュー用プレビュー（実行しません）
python3 run_seh.py project check --root /path/to/project --kind test --project services/api --ecosystem go
# 確認後のみ実行（リポジトリのスクリプトは任意の処理を実行し得ます）
python3 run_seh.py project check --root /path/to/project --kind test --project services/api --ecosystem go --execute --approve
```

Pythonだけに依存する指示・テストコマンドは生成せず、言語とプロジェクト構成に合わせます。依存インストールは自動化しません。

## オフラインの実動デモ

サンプルを作業用ディレクトリへコピーして、DAG実行と評価を試せます。

```bash
cp -R examples/demo_project /tmp/seh-demo
python3 run_seh.py setup --root /tmp/seh-demo --yes --hosts claude
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
