# ワークフロー

[ドキュメント索引](README.md) · [English](../en/workflows.md)

作業の進め方は 3 つあります。軽いものから重いものへ順に並んでいます。まず `/work` を使ってください。タスクが必要とすれば、AI が自分で重い方へ移ります。

| ワークフロー | 用途 | 実行者 |
|---|---|---|
| [`/work`](#work) | 日常的な変更 | あなた（AI CLI 内で） |
| [管理実行](#管理実行) | 大規模、複数コンポーネントにまたがる、リスクの高い、複数ホストにまたがる変更 | `ego work run`（通常は `/work` が起動） |
| [ゴールループ](#ゴールループ) | コマンドで達成を証明できるゴールで、複数回の試行が見込まれるもの | `ego work loop` |

## `/work`

```text
/work Add timeout input validation and run the related tests
```

`/work` は選択したすべての CLI に生成される skill です。依頼の規模を見て、最も軽いワークフローを選びます。

| 依頼 | 何が起きるか | 自動では作られないもの |
|---|---|---|
| 小さく低リスク | 関連ファイルを読む → 編集 → 対象を絞ったテスト → 差分の確認 → 報告 | 要件書、設計書、計画書 |
| 中規模 | 同じ流れ。必要なら短いインラインのチェックリスト | 完全な仕様書 |
| 大規模、リスクが高い、複数ホスト | `ego work plan` で始まる管理実行 | 誰も求めていないドキュメント |
| 達成を証明するコマンドがあるゴール | ゴールループ | |

期待される振る舞い:

- 編集の前に、AI はその変更が必要な理由と、完了を示す条件を述べます。質問するのは、未決の点が実装内容を変え、誤った推測のコストが高い場合だけです。それ以外は前に進め、前提を報告に列挙します。
- **完了**と報告するのは、検証に合格し、変更が適用され、実際にテストを実行した後だけです。それ以外は「検証済み、未適用」または「未完了」と報告します。
- 再利用できる検証済みのプロジェクト事実には `ego memory search` / `ego memory add` を使い、区切りでは `ego session checkpoint` を使います（[セッション](sessions.md)）。

`/harness-init` は生成されるもう 1 つの skill で、ego の導入状態を確認・再設定します。

## 管理実行

管理実行は**タスク JSON**（ノードのグラフ）を実行します。agent ノードはプロジェクトの隔離されたコピーで AI CLI が行い、code ノードと gate ノードはコマンドを実行します。実行が合格し、あなたが承認するまで、作業ツリーには何も反映されません。

```bash
# 1. 計画（モデル呼び出しには --execute が必要。--host mock ならオフラインで 1 ノードの計画を作成）
ego work plan --goal "Reject negative timeouts in the API" --host claude --scope 'src/**,tests/**' --execute --out task.json

# 2. 隔離環境で実行。実在の CLI には --trust-cli が必要
ego work run --task task.json --host claude --trust-cli

# 3. 結果を確認し、検証済みの結果を適用
ego work resume <run_id> --task task.json --host claude --apply --approve --trust-cli
```

`ego work run ... --apply --approve` を使えば、実行と適用を 1 ステップで行えます。

### run の動作

1. プロジェクトを `.ego/state/runs/<run_id>/` 配下のステージング用ディレクトリにコピーします（`--worktree` または `isolation = "worktree"` の場合は Git worktree）。
2. 依存関係の順にノードを実行します。独立したノードは最大 `max_parallel` 件まで同時に実行します。
3. agent ノードごとに、CLI をノードのプロンプトで実行し、変更された全ファイルが `write_allowlist` に一致するか確認し、ノードの `checks` を実行します。失敗したら、チェックの出力をフィードバックとして修正し、`max_attempts` まで繰り返します。
4. 各 wave の後、run ID の下にチェックポイントを保存します。
5. `--apply --approve` を指定すると、run の開始後にプロジェクト側の対象ファイルが変更されていないことを確認したうえで、変更ファイルをプロジェクトにコピーします（変更されていれば `STALE_BASE`）。

### `ego work run` / `ego work resume` のフラグ

| フラグ | 意味 |
|---|---|
| `--task <file>` | タスク JSON（必須） |
| `--host <name>` | ホストを指定しないノードの既定ホスト: `mock`（既定、オフライン）、`claude`、`codex`、`opencode`、`agy` |
| `--trust-cli` | 実在の AI CLI を起動するのに必要。CLI は自身のプロセス内で任意のツールを実行できます |
| `--apply --approve` | 検証済みの結果をプロジェクトに適用。`--approve` のない `--apply` は拒否されます |
| `--hosts <list>` / `--strategy <s>` | タスク全体の既定ホストと戦略（`single`、`fallback`、`compare`） |
| `--model <id>` / `--models <json>` | タスク全体の既定モデル、またはホスト → モデルの対応表 |
| `--autonomy <mode>` | `auto`（既定）、`safe`（計画・読み取り専用モード）、`bypass`（権限確認をスキップ。`--attest-external-sandbox` が必要） |
| `--worktree` / `--keep-worktree` | Git worktree を使う。終了後も残す |
| `--verbose` | 要約ではなく、完全な run の記録を表示 |

### 結果

出力はコンパクトな JSON の要約です。`run_id`、`final_status`、`applied`、ノードごとの状態、変更ファイル、切り詰めた失敗情報、使用量、`checkpoint_path` を含みます。完全な記録は `record_path` に保存されます。

| `final_status` | 意味 | 終了コード |
|---|---|---|
| `PASSED` | すべてのノードが検証済み。`applied` でプロジェクトが変更されたかを示す | 0 |
| `COMPARISON_READY` | compare ノードが比較用の候補を生成。何も適用されない | 0 |
| `FAILED` | ノードが試行回数を使い切っても失敗した | 5 |
| `BLOCKED_DEPENDENCY`、`MERGE_CONFLICT` | 依存するノードが失敗した、または並列ノードが同じファイルを矛盾して編集した | 5 |
| `TIMEOUT`、`BUDGET_EXHAUSTED`、`CANCELLED` | 上限に達した、または `ego work cancel <run_id>` を実行した | 5 |

`ego work status <run_id>` で run を再表示できます。使用量の行（例: `plan 1.2k tokens · work 45.6k tokens (task A 9.0k) · total 46.8k tokens`）は、入力 + 出力 + キャッシュ読み取り + キャッシュ作成のトークン数です。コストはホスト CLI 自身の見積もりです。`cost_known: false` はホストがコストを報告しなかったことを意味します。

### タスク JSON

完全な例: [`examples/multihost/task.json`](../../examples/multihost/task.json)

```json
{
  "schema_version": 1,
  "goal": "Add a /health endpoint with tests",
  "instructions": "Prefer minimal changes.",
  "defaults": {"hosts": ["claude"], "strategy": "single", "model": "auto"},
  "nodes": [
    {
      "id": "implement",
      "kind": "agent",
      "intent": "The endpoint is what monitoring will call.",
      "prompt": "Add GET /health returning 200 and a test for it.",
      "done_when": ["tests/test_health.py passes"],
      "write_allowlist": ["src/**", "tests/**"],
      "checks": [["pytest", "-q", "tests/test_health.py"]]
    },
    {"id": "gate", "kind": "gate", "depends_on": ["implement"], "argv": ["pytest", "-q"]}
  ]
}
```

タスクレベルのフィールド:

| フィールド | 意味 |
|---|---|
| `goal`、`instructions` | すべての agent ノードにユーザー指示として渡される |
| `instructions_file` | プロジェクト相対のパス。共有指示を追加で記述したファイル |
| `defaults` | `host` / `hosts`、`strategy`、`model`、`models`、`fallback_on`。ノードが独自に設定しない場合の既定値 |
| `nodes` | 空でないノードのリスト |

ノードのフィールド:

| フィールド | 対象 | 意味 |
|---|---|---|
| `id` | すべて | 一意の ID（英字、数字、`-`、`_`） |
| `kind` | すべて | `agent`（既定）、`code`、`gate` |
| `depends_on` | すべて | 先に合格している必要がある ID |
| `prompt` | agent | 達成すること（必須） |
| `write_allowlist` | agent、code | ノードが変更してよいファイルの glob パターン。agent と code では必須 |
| `checks` | すべて | 終了コード 0 で成功すべきコマンド（argv 配列） |
| `argv` | code、gate | 実行するコマンド（`argv` か `checks` のどちらかが必要） |
| `role` | agent | プロンプトに含まれる役割ラベル |
| `intent`、`done_when`、`non_goals`、`assumptions`、`provides` | agent | そのステージの理由、受け入れ基準、やらないこと、依拠する事実、後続が必要とするもの。短い文字列（`intent`）か、各 400 文字以内の文字列を最大 6 件含むリスト |
| `host` / `hosts`、`strategy`、`model` / `models`、`fallback_on` | agent | ルーティング。下記参照 |
| `instructions_file`、`context_files` | agent | 追加の指示。参照ファイルは最大 24 件 |
| `max_attempts`、`max_turns`、`timeout_seconds` | すべて | ノードごとの上限。設定値で頭打ちになる |

コマンドは argv 配列で指定します。シェル文字列は使えません。また、必ず `allow_commands` にある名前で始まる必要があります。

### ホストとモデルの選び方

優先順位（高い順）: ノード → タスクの `defaults` → CLI フラグ（`--host`、`--model`、`--models`） → `.ego/config.toml` の `models` → CLI 自身の既定。`"auto"` は常に CLI の既定を意味します。

| 戦略 | ホスト数 | 動作 |
|---|---|---|
| `single` | ちょうど 1 | 既定 |
| `fallback` | 2 以上 | 前のホストが `UNAVAILABLE`、`TIMEOUT`、`RATE_LIMIT` のときに次のホストを試す（`fallback_on` で変更可） |
| `compare` | 2 以上 | 全ホストを実行し、各結果をレビュー用に残す。終端ノードのみ。含む run は適用できない |

複数のホストを持つノードは、戦略を明示する必要があります。これにより、意図しない別ベンダーの CLI が起動されることを防ぎます。code ノードと gate ノードはホストやモデルを選べません。

```bash
ego work run --task task.json --hosts claude,codex --strategy fallback --models '{"claude":"sonnet","codex":"auto"}' --trust-cli
```

## ゴールループ

コマンドでゴールを証明できる場合（テストが通る、ビルドが成功する）に使います。そのようなコマンドがなければ、ループは使わないでください。

```bash
ego work loop --goal "Reject negative timeouts in the API" --done-check "pytest -q tests/api" \
  --host claude --scope 'src/**,tests/**' --max-iterations 3 --max-budget-usd 5 --execute --trust-cli
ego work loop-status <loop_id>
ego work loop-apply <loop_id> --approve      # 確認後、検証済みのループを適用
ego work cancel <loop_id>                    # 次のイテレーションの前に停止
```

| フラグ | 意味 |
|---|---|
| `--goal` | 達成すること（必須） |
| `--done-check` | ゴールが成立したときに終了コード 0 となるコマンド（必須、繰り返し指定可）。コマンド行または JSON の argv 配列 |
| `--host` | `mock`（既定）、`claude`、`codex`、`opencode` |
| `--scope` | ループが変更してよいファイルのカンマ区切り glob（既定 `src/**,tests/**`） |
| `--max-iterations` | 既定 3、1〜10 |
| `--max-budget-usd`、`--max-wall-seconds` | 任意の上限 |
| `--execute` | プランナーのモデル呼び出しを許可 |
| `--plan-first` | 最初のイテレーションの前にプランナーを呼ぶ |
| `--trust-cli`、`--autonomy`、`--attest-external-sandbox`、`--apply --approve` | 管理実行と同じ |

仕組み:

- 最初のイテレーションではプランナーを省略します。ワーカーを 1 つ実行し、done-check に対して修正します。プランナーは、イテレーションが失敗した後、または `--plan-first` を指定したときに呼ばれます。
- 各イテレーションは、新しい CLI セッションで計画と実行を行います。状態は会話ではなく `.ego/state/loops/<loop_id>/ledger.json` に保存されます。
- 再計画に渡されるのは、上限付きの記録だけです。失敗したノード、切り詰めたチェック出力、変更されたパスです。
- 検証済みの変更は次のイテレーションに引き継がれます。失敗したノードの編集は引き継がれません。

| 終了状態 | 意味 |
|---|---|
| `VERIFIED` | ステージング用コピーでゴールが成立。まだ何も適用されていない |
| `COMPLETED` | ゴールが成立し、結果が適用された |
| `STALLED` | 2 回のイテレーションが同じ内容と同じ失敗で終わった |
| `MAX_ITERATIONS`、`BUDGET_EXHAUSTED`、`TIMEOUT`、`CANCELLED` | 上限に達した |

1 回の対話的な Claude Code セッションに収まる小さなゴールなら、CLI 自身の `/goal` で十分です。

## 補助コマンド

| コマンド | 用途 |
|---|---|
| `ego memory add '<fact>' --evidence <ref>` / `ego memory search '<words>'` | 検証済みのプロジェクト事実を保存・検索 |
| `ego code search <query>` / `symbol` / `impact` / `affected` | 設定されたプロバイダによるコード検索。テキスト検索にフォールバック |
| `ego context audit` | 常時読み込まれるコンテキストと、必要時に読み込まれるコンテキストを予算と比較（超過時は終了コード 5。トークン数は推定） |
| `ego events tail -n 20` | 最近のハーネスイベント |
| `ego pr create --title ... --body ... --approve` / `ego pr reviews <n>` | `gh` 経由のプルリクエスト操作 |

skill やプロンプトそのものを改善する（`ego eval`、`ego author`、`ego evolve`、`ego optimize`）については、[コマンドリファレンス](commands.md#スキルとプロンプトの改善) を参照してください。
