# セットアップ

[ドキュメント索引](README.md) · [English](../en/setup.md)

ego のインストール、プロジェクトへの適用、更新、生成ファイル、ロールバック、アンインストールを扱います。設定キーの詳細は [設定](configuration.md) を参照してください。

## 要件

- macOS または Linux の Python **3.11 以上**。ego 自体は Python の依存パッケージを持ちません。
- 使用する AI CLI（`claude`、`codex`、`opencode`、`agy`）を個別にインストールし、サインインしておいてください。ego が CLI を導入することはありません。
- Git（worktree による隔離と、開発用チェックアウトの更新に使用）。

## インストール

```bash
# リポジトリからのグローバル CLI（どちらか一方）
uv tool install git+https://github.com/jumtra/ego
pipx install git+https://github.com/jumtra/ego

# 公開済みリリース wheel から（バージョンは最新リリースに置き換えてください）
pipx install https://github.com/jumtra/ego/releases/download/v0.0.16/ego-0.0.16-py3-none-any.whl
pip install https://github.com/jumtra/ego/releases/download/v0.0.16/ego-0.0.16-py3-none-any.whl   # 仮想環境内で

ego --version
```

パッケージは `ego` コマンドを導入します。

**ego 本体の開発:** リポジトリをクローンし、`make install`（`bin/ego` を `~/.local/bin` にシンボリックリンク。`PREFIX=` / `BINDIR=` で変更可）または `pip install -e .` を実行します。`~/.local/bin` が `PATH` に含まれていることを確認してください。

## 更新

```bash
ego update --check              # 確認のみ
ego update --yes                # ego を更新
ego update --yes --sync-all     # 更新後、登録済みの全プロジェクトを再生成
ego update --yes --sync --root /path/to/project   # 更新後、1 つのプロジェクトを再生成
```

`ego update` はインストール方法を判定して動作を変えます。

| インストール方法 | `ego update` の動作 |
|---|---|
| Git チェックアウト（`make install`、`pip install -e .`） | upstream ブランチを `git fetch` し、fast-forward のみでマージ。ローカルコミットや競合する編集があれば更新を止めます。マージ・rebase・stash・破棄は行いません |
| wheel（仮想環境、pipx、`uv tool`） | 最新の GitHub Release wheel を SHA256 検証のうえ導入 |

どちらも `--yes` か対話での確認が必要です。オフラインでは `ego update --wheel <file> --sha256 <digest> --yes` を使います。

## プロジェクトへの適用

```bash
cd /path/to/project
ego apply
```

`ego apply` はホームディレクトリや `/` に対しては実行を拒否します。結果は stdout に JSON で出力され、ウィザードの表示は stderr に出ます。

### ウィザード

対話ターミナルで、後述の選択フラグを 1 つも指定しないと、ウィザードが次の項目を尋ねます。

| 質問 | 選択肢 | 既定値 |
|---|---|---|
| Profile | `minimal` / `balanced` / `full` | `balanced` |
| Hosts（複数選択） | `claude` / `codex` / `opencode` / `agy` | 4 つすべて |
| コード解析 | `codegraph` / `graphify` / `none` | `none` |
| メモリ | `file` / `engram` | `file` |
| 既定の実行方式 | `native` / `managed` | `native` |
| テストコマンドを検出 | はい / いいえ（テストが未設定のときのみ） | いいえ |
| この変更を適用しますか？ | はい / いいえ（変更ファイル一覧の後に表示） | **いいえ** |

番号か名前で答えます。Hosts は `1,2` や `all` と入力できます。Enter で現在の値を維持、`q` でキャンセルです。再適用時は既存の `.ego/config.toml` が既定値になります。プロンプトは現在日本語で表示されます。

ウィザードは Policy、規定モデル、セッションのしきい値を尋ねません。これらはフラグか `.ego/config.toml` で設定してください。

### フラグ

| フラグ | 意味 |
|---|---|
| `--hosts <list>` | 生成対象の CLI（カンマ区切り）: `claude,codex,opencode,agy` |
| `--profile <name>` | `minimal`、`balanced`、`full`。現時点で出力が変わるのは `full` のみで、Claude Code の `PreToolUse` ポリシーフックを追加します |
| `--policy <mode>` | `relaxed`（既定）または `strict`。[設定](configuration.md#ポリシーモード) を参照 |
| `--models <json>` | ホストごとの既定モデル。例: `'{"claude":"sonnet","codex":"auto"}'` |
| `--graph <provider>` | `none`、`codegraph`、`graphify` |
| `--memory <provider>` | `file` または `engram` |
| `--runtime <mode>` | `native` または `managed`（記録される既定値） |
| `--clear-suggest-tokens <N>` | セッションをチェックポイント保存してクリアするコンテキストサイズ。`0` で無効。[セッション](sessions.md) を参照 |
| `--detect-tests` | テストコマンド候補を検出して設定に保存（実行はしません） |
| `--dry-run` | 計画を表示するだけで、何も書き込まない |
| `--check` | `--dry-run` と同様だが、未適用の変更または衝突があると終了コード `5`（CI 向け） |
| `--interactive` | フラグや標準入力のリダイレクトがあっても、必ずウィザードを実行 |
| `--non-interactive` | 一切プロンプトを出さない |
| `--root <path>` | 適用先のプロジェクト（既定: カレントディレクトリ） |

`--hosts`、`--profile`、`--graph`、`--memory`、`--runtime`、`--detect-tests` のいずれかを指定するとウィザードを省略して直接適用します。`--policy`、`--models`、`--clear-suggest-tokens` だけではウィザードは省略されないため、スクリプトでは `--non-interactive` を付けてください。

```bash
ego apply --hosts claude,codex --policy relaxed --models '{"claude":"sonnet"}' --profile balanced
ego apply --non-interactive        # 保存済みの設定で再適用
ego apply --interactive --dry-run  # 書き込まずに選択を試す
```

### 結果の status

| `status` | 意味 |
|---|---|
| `UP_TO_DATE` | 変更なし |
| `CHANGES_PENDING` | `--dry-run` / `--check` で変更を検出 |
| `APPLIED` | ファイルを書き込み。`snapshot_id` がバックアップを示す |
| `BLOCKED` | ego が書き込もうとしたファイルをユーザーが編集済み。何も書き込まない。`conflicts` を参照 |
| `CANCELLED` | ウィザードを拒否またはキャンセルした |

## 生成されるファイル

| パス | 内容 | 生成条件 |
|---|---|---|
| `.ego/config.toml` | プロジェクト設定 | 常に |
| `.ego/ownership.json` | ego が管理するファイルのハッシュ、導入済みバージョン | 常に |
| `.ego/spec/skills/{work,harness-init}/` | 正本の skill（ホスト向けコピーの元） | 常に |
| `.ego/spec/prompts/tool-guide.md`、`.ego/knowledge/domain.md` | ツールガイド、プロジェクト知識のひな形 | 常に |
| `CLAUDE.md` | 管理対象の指示ブロック | ホスト `claude` |
| `.claude/skills/`、`.claude/agents/`、`.claude/rules/` | `/work` と `/harness-init` の skill、agent 4 種、rule 2 種 | ホスト `claude` |
| `.claude/settings.json` | フック / ステータスライン | ホスト `claude` かつ、Profile `full`、`auto_resume`（既定で有効）、`clear_suggest_tokens > 0`、`usage_switch_percent > 0` のいずれか |
| `.codex/hooks.json` | フック | ホスト `codex` かつ、`auto_resume`（既定で有効）、`clear_suggest_tokens > 0`、`usage_switch_percent > 0` のいずれか |
| `.agents/hooks.json` | フック | ホスト `agy` かつ、`auto_resume`（既定で有効）または `clear_suggest_tokens > 0` |
| `.opencode/plugins/ego.ts` | プラグイン（フック） | ホスト `opencode` かつ、`auto_resume`（既定で有効）または `clear_suggest_tokens > 0`。プラグインは `ego apply` 時の `ego` の絶対パスを固定します |
| `AGENTS.md` | 管理対象の指示ブロック | ホスト `codex`、`opencode`、`agy` |
| `.agents/skills/` | skill | ホスト `codex`、`opencode`、`agy` |
| `.codex/agents/*.toml` | agent 4 種 | ホスト `codex` |
| `.opencode/agents/*.md` | agent 4 種 | ホスト `opencode` |
| `.agents/agents/*.md` | agent 4 種 | ホスト `agy` |

agent は `implementer`、`reviewer`（読み取り専用）、`fixer`、`evolver` の 4 種です。

`CLAUDE.md` / `AGENTS.md` では、ego は `EGO:BEGIN` / `EGO:END` マーカーの間のブロックだけを管理します。それ以外の部分はユーザーのものです。その他の生成ファイルはハッシュで追跡され、ego が書いた内容のままの場合にのみ置き換えます。変更されていれば衝突（`BLOCKED`）として報告します。

実行時の状態も `.ego/` 配下にあります。`state/`（run、loop、イベント）、`memory/`（メモリ項目と、`handoffs/` のチェックポイント）、`backups/`（適用時のスナップショット）、`locks/` です。`.ego/state/`、`.ego/backups/`、`.ego/locks/`、`.ego/memory/handoffs/` は Git に含めないでください。

## 設定を後から変更する

1. `.ego/config.toml` を編集する（[設定](configuration.md)）か、フラグを付けて `ego apply` を再実行する。
2. ホスト向けファイルを再生成する:

```bash
ego sync            # プレビューのみ
ego sync --apply    # 書き込み
```

`ego sync` は `--apply` がない限り dry-run です。

## 状態の確認・ロールバック・アンインストール

```bash
ego status                # UP_TO_DATE / CHANGES_PENDING / BLOCKED、導入済みと実行中のバージョン
ego rollback              # 直前の apply のスナップショットを復元
ego rollback <snapshot>   # .ego/backups/ の指定スナップショットを復元
ego uninstall             # 確認を求める。-y / --yes で省略
ego uninstall --cli       # ~/.local/bin の ego シンボリックリンクを削除
```

`ego rollback` は、適用後に編集されたファイルを上書きしません。

> [!WARNING]
> `ego uninstall` は `CLAUDE.md` / `AGENTS.md` から管理ブロックを削除し、`.ego/`、`.claude/`、`.agents/`、`.codex/`、`.opencode/` の**ディレクトリ全体**を削除します。ego が作成していないファイルも含まれます。これらのディレクトリにある自作の skill、agent、設定は先にバックアップしてください。

## 複数のプロジェクト

`ego apply` と `ego sync --apply` は、プロジェクトのパスをユーザー単位のレジストリに記録します。場所は `~/.config/ego/projects.json`（または `$EGO_HOME/projects.json`）です。

```bash
ego projects list                 # 各プロジェクトの installed_version、version_differs、ドリフト状態
ego projects scan ~/Develop       # ego が適用済みのプロジェクトを登録（--depth N、既定 4）
ego projects add <path>...        # 明示的に登録
ego projects remove <path>...     # 登録解除（ファイルは変更しない）
ego sync --all                    # 登録済み全プロジェクトの再生成をプレビュー
ego sync --all --apply            # 各プロジェクトの .ego/config.toml から再生成
```

ユーザーが編集した管理ファイルを持つプロジェクトは `BLOCKED` と報告され、何も変更されません。他のプロジェクトは適用され、終了コードは `5` になります。

## 導入状態の確認

```bash
ego doctor            # 検出された CLI、コード解析、メモリ、サンドボックス、skill の lint、modules
ego hosts inspect     # CLI のバージョンと検出されたフラグ
ego models list       # ホストごとのモデル一覧
```
