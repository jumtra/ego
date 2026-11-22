# ワークフロー

[ドキュメント索引](README.md) · [English](../en/workflows.md)

作業の進め方は 3 つあります。軽いものから重いものへ順に並んでいます。まず `/work` を使ってください。タスクが必要とすれば、AI が自分で重い方へ移ります。

| ワークフロー | 用途 | 実行者 |
|---|---|---|
| [`/work`](#work) | 日常的な変更 | あなた（AI CLI 内で） |
| [管理実行](#管理実行) | 大規模、複数コンポーネントにまたがる、リスクの高い、複数ホストにまたがる変更 | `ego work run`（通常は `/work` が起動） |

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

期待される振る舞い:

- 編集の前に、AI はその変更が必要な理由と、完了を示す条件を述べます。質問するのは、未決の点が実装内容を変え、誤った推測のコストが高い場合だけです。それ以外は前に進め、前提を報告に列挙します。
- **完了**と報告するのは、検証に合格し、変更が適用され、実際にテストを実行した後だけです。それ以外は「検証済み、未適用」または「未完了」と報告します。
- プロジェクトのナレッジは `knowledge` が示す場所（`.ego/knowledge` の index、または alter）から読み、処理に関する事実には `ego memory search` / `ego memory add`、区切りでは `ego session checkpoint` を使います（[セッション](sessions.md)）。

他に生成される skill として、編集前に聞き取りとノードグラフ計画の承認を行う `/work-plan` と、ego の導入状態を確認・再設定する `/harness-init` があります。

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

1. プロジェクトを `.ego/state/runs/<run_id>/` 配下のステージング用ディレクトリにコピーします。`--worktree` または `isolation = "worktree"`、もしくは既定の `isolation = "auto"` で同時に書き込むノードが 2 つ以上ある場合、ステージング用ディレクトリは Git worktree になり、書き込むノードはそれぞれ run の HEAD から切った branch の worktree で作業します。読み取り専用のノード（reviewer・splitter・gate）は worktree を作りません。`auto` で書き込みノードが直列にしかない場合はステージング用コピーのみです。
2. 依存するノードがすべて成功した時点で、そのノードを開始します。同時実行は最大 `max_parallel` 件です。空いた枠は実行中のどれかが終わるとすぐ埋められるため、ノードが wave の残りを待つことはありません。
3. agent ノードごとに、CLI をノードのプロンプトで実行し、変更された全ファイルが `write_allowlist` に一致するか確認し、ノードの `checks` を実行します。失敗したら、チェックの出力をフィードバックとして修正し、`max_attempts` まで繰り返します。ノードが失敗しても止まるのはその下流だけで、独立したノードは最後まで実行します。
4. 成功したノードは、終わるたびにステージングへマージされます。worktree 隔離の場合はそのノードのコミットを 3-way マージします。隔離しない場合はノードのファイルを直接書き込み、このノードの開始後に別のノードが変更したファイルは競合になります。競合が起きると run は `MERGE_CONFLICT` で終了します。並列に動きうる2つのノードが `write_allowlist` に同じパスをそのまま書いている場合は、run の開始前に拒否されます（`PARALLEL_WRITE_OVERLAP`）。一方をもう一方に依存させてください。git が無視するファイル（チェックが書く `dist/` など）は、`write_allowlist` で明示しないかぎりノードの編集として数えません。`code` と `gate` のノードは1回だけ実行します。同じ入力で同じコマンドを再実行しても結果は変わらないためです。
5. ノードが終わるたびに、ほかのノードが実行中でも、ステージング済みファイル、`record.json`、run ID の下のチェックポイントを保存します。失敗した兄弟ノードがあっても、または process が落ちても、成功したノードは保存されたまま残ります。
6. `--apply --approve` を指定すると、run の開始後にプロジェクト側の対象ファイルが変更されていないことを確認したうえで、変更ファイルをプロジェクトにコピーします（変更されていれば `STALE_BASE`）。

**ステージングの保持。** ignored ファイルは、ファイルシステムが対応していれば copy-on-write の clone でコピーされ、実容量をほとんど使いません。最終更新から 3 日を過ぎたステージングと、合計 3GB を超えた分の古いステージングは自動で削除されます（実行中の run と最新のステージングは容量上限では削除しません）。`record.json` と progress は残ります。`--from` で引き継いだ元 run のステージングは、新しい run の作成直後に削除されます。ステージングが削除された run は resume・`--apply`・`--from` できません（`STAGING_MISSING` / `SEED_MISSING`）。

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
| `--run-id <id>` | （`run` のみ）実行 ID を固定し、`ego work watch <id>` で追えるようにします |
| `--from <run_id>` | （`run` のみ）失敗した run を、修正したタスクで続けます。その run の検証済みの変更から始め、定義と上流が変わっていない検証済み agent ノードは再実行しません |
| `--worktree` / `--keep-worktree` | Git worktree を使う。終了後も残す |
| `--max-run-tokens <N>` | この実行における `max_run_tokens` を上書き（`0` で無制限、または `1000` 以上） |
| `--verbose` | 要約ではなく、完全な run の記録を表示 |

### 結果

出力はコンパクトな JSON の要約です。`run_id`、`final_status`、`applied`、ノードごとの状態、変更ファイル、切り詰めた失敗情報、使用量、`checkpoint_path` を含みます。完全な記録は `record_path` に保存されます。

| `final_status` | 意味 | 終了コード |
|---|---|---|
| `PASSED` | すべてのノードが検証済み。`applied` でプロジェクトが変更されたかを示す | 0 |
| `COMPARISON_READY` | compare ノードが比較用の候補を生成。何も適用されない | 0 |
| `FAILED` | ノードが試行回数を使い切っても失敗した | 5 |
| `BLOCKED_DEPENDENCY`、`MERGE_CONFLICT` | 依存するノードが失敗した、または並列ノードが同じファイルを矛盾して編集した | 5 |
| `BUDGET_EXHAUSTED`、`CANCELLED` | run のトークン上限（`max_run_tokens`）に達した、または `ego work cancel <run_id>` を実行した | 5 |

**失敗した run の続け方。** 失敗したノードだけをタスクファイルで直し、`ego work run --task <file> --from <run_id>` で実行すると、直したノードとその下流だけが動きます。タスクを変えずに `ego work resume <run_id>` すると、失敗したノードが前回の失敗内容をフィードバックとして受け取って再実行されます（reviewer の REJECT なら対象の coder に指摘を渡します）。未完了の run の worktree は、続けられるように削除しません。

`ego work status <run_id>` で run を再表示できます。使用量の行（例: `plan 1.2k tokens · work 45.6k tokens (task A 9.0k) · total 46.8k tokens`）は、入力 + 出力 + キャッシュ読み取り + キャッシュ作成のトークン数です。コストはホスト CLI 自身の見積もりです。`cost_known: false` はホストがコストを報告しなかったことを意味します。

### チェックポイント・中断・ロック

各ノードは終わった瞬間に保存されます。同じ batch の他のノードが終わるまで待つことはありません。ステージング済みファイル、`record.json`、run ID の下のチェックポイント（`ego session list` に表示されます）がその時点で書かれるため、process が落ちた run でも、それまでに成功したノードは残ります。

- **中断された run。** まだ `RUNNING` と記録されているのに、ロックが解放されている run は、process が途中で失われたものです。`ego work status <run_id>` は `final_status: INTERRUPTED` と、`next_action` に `ego work resume` のコマンドを返します。`ego work watch` は `run_stopped` イベントで終了します。再開するには `ego work resume <run_id> --task <file> --host <host> --trust-cli` を実行します。PASSED のノードは再実行されず、実行中だったノードは再実行されます。
- **run ロック。** 管理実行を動かしている process は、`.ego/locks/<run_id>.lock` に排他的な非ブロッキングロックを保持します。同じ run を二重に開始・再開すると `RUN_ACTIVE` で失敗し、run には触れません。`ego session resume --from`、`ego session handoff`、コンテキストのクリア案内は、ロックが保持されている管理実行のチェックポイントを拒否します。タスク ID を指定しない場合は、そのチェックポイントを読み飛ばします。

### 途中経過

実行中、ego は `.ego/state/runs/<run_id>/progress.jsonl` にイベントを 1 行ずつ JSON で追記します（stderr にも同じ行を出します）。`ego work watch <run_id> [--after <seq>]` は実行が終わるまでその行を流し、`ego work status <run_id>` は直近 10 件を `progress` として表示します。

| イベント | 主な項目 |
|---|---|
| `run_started` | `nodes`, `resume` |
| `nodes_carried` | `nodes`, `from_run`（前の run で検証済みのため実行しない agent ノード） |
| `node_started` | `node`, `kind`, `role` |
| `attempt_finished` | `node`, `attempt`, `host`, `model`, `status`, `checks`（argv と結果）, 切り詰めた `feedback`, `changed`, `tokens`, `segments`, `rotations` |
| `worker_segment` | `node`, `attempt`, `segment`, `tokens`, `action`（`continue` か `rotate`） |
| `node_finished` | `node`, `status`, `attempts`, `changed`。reviewer は `verdict` と `findings` も |
| `review_round` | `reviewer`, `reviews`, `round`, `cause`（`reject` か `gate`）。停滞した場合は `loop_stalled` |
| `run_finished` / `run_error` | `final_status`, `applied`, `changed_files`, `message` / `code`, `message` |

要約には、ノードごとの状態・試行回数・check 結果を持つ `nodes` と、検証済みで未適用の run の適用コマンド `next_action` が加わります。

**`/work-plan` はこの経路で実行されます。** 計画を承認すると、AI は計画を Task JSON にし、`ego work run --run-id ...` を起動して、届いたイベントを順にあなたへ伝えます（Claude Code は Monitor、他のホストは `ego work status`）。適用の前には確認を取ります。

**ワーカーのコンテキスト上限。** 全ホスト（`claude`、`codex`、`opencode`、`agy`）の書き込み可能なワーカーは `clear_suggest_tokens` で自動的にローテーションします。Claude ワーカーは `worker_segment_turns` ターンごとの区間で実行して区間間に計測し、Codex / OpenCode / AGY は実行中のストリームとセッション記録を監視して上限で中断します。Claude で `clear_suggest_tokens` 未満なら同じセッションを `--resume` で続け、以上ならチェックポイントを保存させて新しいセッションで続きを実行します（`worker_segment` イベントの `action: rotate`）。Claude 以外のホストでチェックポイントが未保存だった場合は、ワークスペースの変更ファイル一覧と直前出力から機械的フォールバック引き継ぎ文を生成して新セッションで続行します（`fallback: true`）。読み取り専用ノードと `mock` は従来どおり 1 回で実行します。

### タスク JSON

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
      "requirement": {"what": "GET /health returns 200", "where": "src/app.py, tests/test_health.py", "how": "Add a route and a pytest"},
      "steps": ["Add a GET /health route in src/app.py returning 200", "Add tests/test_health.py asserting the 200"],
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
| `defaults` | `host` / `hosts`、`strategy`、`model`、`models`、`fallback_on`、`roles`（役割ごとの経路。例 `{"reviewer": {"host": "codex"}}`。優先順位は node、role、defaults）。ノードが独自に設定しない場合の既定値 |
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
| `role` | agent | `coder` / `tester` / `reviewer`（権限を強制）。`tester` は `tests/` 配下のみ書き込める（`write_allowlist` は `tests/` で始まる必要がある）。`reviewer` は読み取り専用で `write_allowlist` を持たない |
| `inputs`、`outputs` | agent | I/O 契約。読むべき既存ファイル（最大 6 件）と、変更するファイルまたは `path::関数名`。各 output は `write_allowlist` に一致する必要がある。依存先ノードには前段の `outputs` が渡る |
| `reviews`、`max_rounds`、`on_exhausted` | reviewer | `reviews`（必須）は差し戻す `coder` ノード。`max_rounds` は 0〜5（既定は config の `max_rounds`、2）。`on_exhausted` は `fail`（既定）か `replan` |
| `intent`、`done_when`、`non_goals`、`assumptions`、`provides` | agent | そのステージの理由、受け入れ基準、やらないこと、依拠する事実、後続が必要とするもの。短い文字列（`intent`）か、各 400 文字以内の文字列を最大 6 件含むリスト。上限は [設定](configuration.md) の `[tuning.plan_schema]`（`.ego/defaults.toml`）から取ります |
| `requirement`、`steps` | agent | 5W1H（`who` `what` `when` `where` `why` `how`、各 400 文字以内）と、ワーカーが書かれたとおりに実行する 1〜10 件の手順 |
| `host` / `hosts`、`strategy`、`model` / `models`、`fallback_on` | agent | ルーティング。下記参照 |
| `agent` | agent | 任意のカスタムエージェント名（`.claude/agents/*.md`、`.agents/agents/*.md`、`.opencode/agents/*.md`、`.codex/agents/*.toml` から検出）。定義内容がノードのプロンプトに埋め込まれる |
| `mcp` | agent | 必要な MCP サーバー名のリスト（1〜8 件、例: `["alter"]`）。`worker_context = "lean"` でもホストの MCP サーバーを有効のままにし、ドメイン知識・用語集の取得指示を付与する。`knowledge = "alter"` のときは alter（`alter_task_context` / `alter_search` / `alter_get_note`）だけから取得し、file ナレッジのときは `.ego/knowledge/index.md`（`ego knowledge`）を読む。`knowledge = "alter"` 時は自動適用 |
| `instructions_file`、`context_files` | agent | 追加の指示。参照ファイルは最大 24 件 |
| `max_attempts`、`max_turns`、`timeout_seconds` | すべて | ノードごとの上限。config の上限を超える値は `INVALID_TASK` で拒否される（黙って丸めない） |

コマンドは argv 配列で指定します。シェル文字列は使えません。また、必ず `allow_commands` にある名前で始まる必要があります。

### Reviewer とループ

`reviewer` ノードは `gate` または `code` ノードに最低 1 つ依存する必要があり、決定論的チェックが通った後にだけ実行されます。出力の最後は `VERDICT: APPROVE` か `VERDICT: REJECT` です。指摘は `- path:line: 内容` の形式で、証拠（パスと行）のない指摘は破棄されます（証拠が残らない REJECT は APPROVE 扱い）。

証拠付きの REJECT、またはループ内の gate / code の失敗では、対象の `coder` ノードと reviewer が待つノードだけを、指摘をフィードバックにして再実行します。これを 1 ノードあたり最大 `max_rounds` 回まで繰り返します（既定 2）。

- `on_exhausted = "fail"`（既定）: 回数を使い切ると reviewer ノードが失敗します。
- `on_exhausted = "replan"`: 回数を使い切ると、計画を作り直します（[再計画](#再計画max_replans)）。
- 停滞: 再試行で差分とフィードバックが前回と同じになる場合は、回数が残っていてもループを止め、reviewer を失敗させます（`loop_stalled` イベント）。この場合は再計画しません。

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

## 再計画（max_replans）

task の `max_replans` は、計画全体を作り直せる回数です（既定 1、上限は config の `max_replans`）。`on_exhausted = "replan"` の reviewer が回数を使い切ると、検証済みのノードを残し、残りを作り直します。作り直した計画で同じ id のノードは、検証済みのノードを置き換えます。`on_exhausted = "replan"` には `max_replans >= 1` が必要です。config の上限を超える値は `INVALID_TASK` で拒否されます。

## トークン上限

`max_run_tokens`（既定 `0` = 無制限、通常は途中で停止しません）は、設定または `--max-run-tokens` で `1000` 以上を明示指定した場合にのみ、1 回の `run` または `resume` 実行あたりの使用トークンの上限として機能します。`cache_read` トークンは数えず、`resume` ごとに上限枠がリセットされます。上限に達すると、run はステップの区切りで止まり、`final_status` は `BUDGET_EXHAUSTED` になります。run 全体の時間制限はありません。

## セッション後の自動学習と LoopGuard

### LoopGuard（停滞検知と段階的介入）

管理実行の `claude` ワーカーでは、セグメントごとに LoopGuard がトレースを畳み込み、堂々巡りを検知します（`[loopguard]` で設定）。

1. **dry-run 観測（既定 `dry_run = true`）:** 停滞条件（`min_turns`、編集チャーン、エラー回数、失敗シグネチャの反復率、またはリポジトリ固有のターンエンベロープ超過）を判定し、`loopguard` 進捗イベントのみを出力します。実行は止めません。
2. **1 回の nudge（`dry_run = false` 時）:** 最初の停滞検知では、繰り返している失敗シグネチャと試行済みコマンド（最大 3 件）をミラーリングする短い方向転換プロンプトを 1 回だけ（`max_nudges_per_node = 1`）次のセグメントに注入します。
3. **`STAGNATION` 停止:** nudge 後も停滞が続く場合、ワーカーを `FAIL`（理由 `STAGNATION`）で終了し、`stagnation` 要約を `record.json` とフィードバックに残します。
4. **再計画 / alter への報告:** ノードの失敗を受けて `max_replans` に従い再計画（replan）へ進むか、alter 連携プランの場合は `alter ego-plan report` 経由で失敗・停滞状態を報告します。

### EnvFacts ライフサイクルと `alter_knowledge_feedback`

環境ファクト（成功したビルド・テストコマンドや利用可能なツールなど）は、`.ego/state/envfacts.json` で証拠ベースの状態遷移を行います。

1. **`candidate` → `active`:** 異なるセッションで `min_confirmations` 回（既定 2）成功が確認されると `active` に昇格し、以降のセッション開始時・プロンプト末尾へ最大 `max_injected_facts` 件（`max_injected_chars` 文字まで）注入されます。
2. **`active` → `demoted`:** 失敗（矛盾）が `contradiction_demote_threshold` 回（既定 2）に達すると `demoted` に降格し、注入対象から外れます。
3. **`ego knowledge facts` / `forget <id>`:** `ego knowledge facts` で状態を確認し、`ego knowledge forget <id>` で `forgotten` にすると以後二度と注入されません。
4. **`alter_knowledge_feedback`:** `knowledge = "alter"` かつ `lifecycle.alter_feedback = true` の場合、`demoted` への降格時または `ego knowledge forget <id>` 実行時に `alter_knowledge_feedback` を送信します（送信失敗時は `.ego/state/alter-feedback-pending.jsonl` に保留し、次回送信時に再送します）。

### セッション終了後の非同期 fold

session end フックは重い後処理を直接実行せず、`ego fold` を切り離したバックグラウンドプロセスで起動します（fail-open）。

- 非ブロッキングロック（`.ego/locks/fold.lock`）を取得し、二重実行を防ぎます。
- トランスクリプトのトークン使用量を集計して使用量台帳（`ego usage summary`）に追記します。
- EnvFacts を抽出・更新し、必要なら `alter_knowledge_feedback` を送ります。
- トランスクリプト欠損や破損などのエラーは `.ego/state/fold-errors.jsonl` に記録して終了し、セッション終了をブロックしません。

## 補助コマンド

| コマンド | 用途 |
|---|---|
| `ego knowledge add '<fact>' --keyword <w>` / `ego knowledge search '<words>'` | プロジェクトのナレッジを記録・検索（`knowledge = "file"`） |
| `ego memory add '<fact>' --evidence <ref>` / `ego memory search '<words>'` | 検証済みの処理上の事実を保存・検索 |
| `ego code search <query>` / `symbol` / `impact` / `affected` | 設定されたプロバイダによるコード検索。テキスト検索にフォールバック |
