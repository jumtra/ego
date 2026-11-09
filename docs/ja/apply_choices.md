# `ego apply`: 選択式セットアップ

## 動作モード

`cd <project> && ego apply` を**通常の対話ターミナル**で実行すると、既存の `.ego/config.toml` を既定値として選択画面が表示されます。CWDは現在のプロジェクトを表し、ホームやファイルシステムのルートへの誤適用は禁止します。

| 操作 | 選択内容 | 初期値 |
|---|---|---|
| Profile | `minimal` / `balanced` / `full` | `balanced`（再適用は現在値） |
| Hosts（複数選択） | `claude` / `codex` / `opencode` / `agy` | 初回全Host、再適用は現在値 |
| ポリシー（ゆるいモード） | `strict`（安全重視） / `relaxed`（ゆるいモード） | `strict`（再適用は現在値） |
| コード解析 | `codegraph` / `graphify` / `none` | `none` |
| Memory | `file` / `engram` | `file` |
| Runtime既定 | `native` / `managed` | `native` |
| テスト候補の検出 | Yes/No（既存設定にテストがない場合） | No |
| 変更の承認 | 差分一覧と選択内容を表示してYes/No | **No** |

数字または名称で選べます。Hostは `1,2` のように複数選択、`all` ですべて選択、空Enterで現在値を維持。`q`で中止します。標準出力は従来どおりJSON、案内文は標準エラーに出力します。

- **Host選択は永久固定ではありません。** これは各Host向けのSkill/Agent/Instructionsの**生成先**のみ。実際に実行するHostやModelはTask/Nodeごとに別途選びます。
- **ポリシーモード（`policy_mode`）:** `strict`は`git push`や設定ファイルの変更をフックで制限します。個人開発やプロトタイプでAIに自律的にpushや設定更新を任せたい場合は`--policy relaxed`（ゆるいモード）を指定してください。
- `full`はHookなど追加処理を含みます。日常作業を軽くしたい場合は`balanced`を推奨します。
- `engram`・`codegraph`・`graphify`は選択可能ですが、外部サービス/CLIの存在や認証を保証しません。`ego doctor`で確認してください。
- `managed`はCLI実行・検証等の設定既定値であり、危険な操作を自動許可するオプションではありません。

## コマンド例

```bash
# 端末では選択ウィザードを表示
cd my-project
ego apply

# 全部フラグで指定（対話なし・ゆるいモード）
ego apply --hosts claude,codex --policy relaxed --models '{"claude":"sonnet","codex":"auto"}' --profile balanced --graph none --memory file --runtime native

# 既存設定を引き継いでCI/スクリプトに利用
ego apply --non-interactive

# パイプ入力で対話を明示（標準出力はJSONのまま）
ego apply --interactive

# 書き込まないプレビュー／CIチェック（通常は非対話）
ego apply --dry-run
ego apply --check

# 選択を変えたプレビュー（明示的に対話を要求）
ego apply --interactive --dry-run

# 導入結果と復元
ego status
ego rollback
```

通常の対話端末でも、`--hosts`など1つでも明示的な選択フラグを指定した場合は**自動ウィザードを省略**します。他の値を対話で選びたい場合は `--interactive` を併用してください。スクリプト用に `--non-interactive` を指定すれば、TTYであっても質問しません。`--interactive` と `--non-interactive` の同時指定は禁止です。

## 変更保護・失敗時の挙動

1. 入力内容はまずメモリ上で検証し、プランを生成します。この段階ではディレクトリを変更しません。
2. 所有権衝突があれば`BLOCKED`で停止します。選択によってユーザーの変更を強制上書きすることはありません。
3. 差分がある場合はファイルごとの変更種別とパスを表示し、`y`の明示承認で適用します。Enter、`n`、`q`、EOFで**CANCELLED**を返し、書き込みません。
4. 適用前スナップショットを作成し、最新適用は`ego rollback`で復元できます。適用後に人が書き換えたファイルの巻き戻しは拒否します。
5. 差分がない場合は`UP_TO_DATE`で終了します。再適用時の既存選択は維持されます。
6. `--check`は差分があれば終了コード5、コンフリクトなら4。無変更なら0。デフォルトでは質問しません。

## 性能と非対応範囲

選択画面は`ego apply`で起動するCLIの標準入力/出力を使うだけで、追加のLLM呼び出し・外部API・モデル一覧更新をしません。`ego work`など通常のタスク実行ではウィザードをimportしません。

`apply`中にModelを選ぶ設計にはしていません。Modelはタスクごとに更新・変更できるべきであり、プロジェクト初期化で固定する意味がないためです。

**基準**: 前回のローカルv1.4.2実装。公開GitHub `main`にマージ済みという意味ではありません。
