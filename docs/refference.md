# 参考文献 (References)

本ハーネス（ego）のアーキテクチャ設計（コンテキスト圧縮・ハーネス制御・計画構造化）において参照・導入している主要論文一覧です。

---

## 1. コンテキスト管理・ハーネス基盤（導入済みコア要素）

### ① You don't need long context
* **リンク:** https://arxiv.org/abs/2611.05306
* **概要:**
  長距離タスクにおいて全履歴を巨大なコンテキストに保持する必要はなく、頻繁な文脈圧縮（Compaction）と状態の引き継ぎ、および選択的な入力読み取りで同等以上の精度を大幅に低いコストで維持できることを実証しています。
  本ハーネスでは `context.py` や `checkpoint.md` によるチェックポイント単位のコンテキスト引き継ぎ（Handoff）および `codeintel.py` による選択的参照としてこの知見を導入しています。

### ② Dive into Claude Code: The Design Space of Today's and Future AI Agent Systems
* **リンク:** https://arxiv.org/abs/2604.14228
* **概要:**
  エージェントシステムの98%以上は決定論的な運用インフラ（コンテキスト管理、権限ポリシー、状態永続化、実行環境）で構成されることをソースコード解析から明らかにしています。
  本ハーネスでも `policy.py` や `ownership.json` によるガードレール、`session.py` / `memory.py` による状態管理、標準の線形計画の限界を補うハーネス制御を採用しています。

### ③ From Plan to Action: How Well Do Agents Follow the Plan?
* **リンク:** https://arxiv.org/abs/2604.12147
* **概要:**
  コーディングエージェントは線形テキストの計画ではステップ数の増加に伴い「テスト工程のスキップ」や「計画からの逸脱」が頻発することを実証した論文です。
  本ハーネスでは計画（`work-plan` / `planner.py`）と実行（`work.py`）を分離し、`evals.py` による完了検証とループごとの状態再注入によって計画遵守を強制しています。

---

## 2. Graph Engineering・DAG計画（一部導入／計画構造化の設計指針）

### ④ Graph Engineering in the Era of LLM Agents: From Individual Intelligence to System Intelligence
* **リンク:** https://arxiv.org/abs/2608.21156
* **概要:**
  エージェント開発が `Prompt → Context → Harness → Loop → Graph Engineering` へと進化している過程を体系化したサーベイ論文です。
  複雑な長距離タスクを安定させるため、単一ループを超えて「タスク依存グラフ」「検証・リトライの循環エッジ」「共有状態」を明示的に設計する枠組みを示しています。

### ⑤ From Agent Loops to Structured Graphs: A Scheduler-Theoretic Framework for LLM Agent Execution
* **リンク:** https://arxiv.org/abs/2604.11378
* **概要:**
  テキスト履歴を伸ばしながら単一ループで実行する従来方式（Agent Loop）の欠陥を分析し、計画・実行・復旧を3層に分離してDAG（有向非巡回グラフ）で制御する「Graph Harness」を提唱しています。
  各タスクの依存関係（`depends_on`）を明示し、前提エッジが完了したノードのみをスケジューリングすることで、並列実行と正確な障害追跡を可能にします。

### ⑥ Atomic Task Graph: A Unified Framework for Agentic Planning and Execution
* **リンク:** https://arxiv.org/abs/2607.01942
* **概要:**
  粗いタスク計画を、入出力インターフェース（I/O契約）を保持したまま細かい部分グラフへと再帰的にコンパイルする手法を提案しています。
  各ノードが局所的な文脈のみを参照することでハルシネーションを抑え、エラー発生時にも全体を再計画せず失敗した部分グラフだけを差し替える「局所修復」を実現します。

### ⑦ RPG: A Repository Planning Graph for Unified and Scalable Codebase Generation
* **リンク:** https://arxiv.org/abs/2509.16198
* **概要:**
  自然言語による計画の曖昧さを排除するため、「機能階層（What）」「ファイル・クラス構造（Where）」「データフロー（How）」を1つのグラフに統合しトポロジカルソート順に実装する手法を提案しています。
  仕様と対象ファイル・データの流れを単一グラフで結びつけることで、モジュール間の整合性を保ったまま大規模なコードベースを生成できることを示しています。

---

## 3. 論文要素の導入状況（2026-11-14 時点）

凡例: ✅ 導入済み / 🟡 一部 / ❌ 未導入（意図的）。「inline」= `work-plan` Skill の会話内計画、「managed」= `ego work plan` / `ego work run` のタスクJSON。

| 論文 | 要素 | inline | managed | 実装箇所 / 判断 |
|---|---|---|---|---|
| ④ | タスク依存グラフ | ✅ | ✅ | `N1 ──► N2` / `depends_on`（`work.py` validate_task） |
| ④ | 検証・リトライの循環エッジ | 🟡 | ✅ | managed は `max_attempts` + 失敗出力つき修復、`loop.py` で再計画。inline 単体は「計画が誤りなら停止して承認」のみだが、承認済み計画を `--task` で managed に渡せばこの循環に乗る（E8） |
| ④ | 共有状態 | ✅ | ✅ | `ego session checkpoint` / `memory.py` / run record |
| ⑤ | 計画・実行・復旧の3層分離 | ✅ | ✅ | `planner.py` / `work.py` / `loop.py` |
| ⑤ | 前提完了ノードのみスケジュール・並列実行 | — | ✅ | `work.py` ready 判定 + `max_parallel` の wave 実行 |
| ⑤ | ノード単位の障害追跡 | — | ✅ | `node_status`、`BLOCKED_DEPENDENCY` |
| ⑥ | I/O 契約 | 🟡 | 🟡 | 出力側 `provides`（inline は Provides 行）のみ。入力側（何を消費するか）は `depends_on` で暗黙 |
| ⑥ | 局所文脈 | ✅ | ✅ | ワーカーは fresh session・`worker_context = "lean"` |
| ⑥ | 部分グラフへの再帰コンパイル | ❌ | ❌ | 意図的に未導入: 「最少ノード」方針と衝突し、計画コストが増える |
| ⑥ | 失敗部分グラフだけの局所修復 | ❌ | 🟡 | `loop.py` は検証済み変更を引き継ぎ「残作業のみ」再計画。部分グラフ差し替えではない。`ego work loop --task` では承認済み計画が1回目、失敗後だけ再計画される |
| ⑦ | What / Where / How を1グラフに統合 | ✅ | ✅ | ノードごとの 5W1H（下記 ego 独自要素 E1） |
| ⑦ | トポロジカル順の実装 | ✅ | ✅ | inline はグラフ順、managed は依存解決順 |
| ⑦ | データフロー（ノード間の受け渡し） | 🟡 | 🟡 | `provides` / inline の Provides 行で代替。ファイル間データフローのモデル化はしない（RPG はゼロからのリポジトリ生成向け） |

### 判断: 「全要素を入れる」より「現状維持 + 小さな整合」を推奨

- ❌ の2つ（再帰コンパイル・部分グラフ差し替え）は、ego の対象（既存リポジトリへの数ノードの変更）では効果よりも計画コストと複雑さが大きい。⑥⑦ の主な実験は大規模な新規生成で行われている。
- inline と managed のずれ（managed ノードにある `provides` / `non_goals` / `assumptions` が inline に無かった）は 2026-11-14 に解消済み。inline の各ノードに Non-goals / Assumptions / Provides 行を追加し、Why→`intent`、Done when→`done_when` の対応を明記した（E3 参照）。
- inline で承認した計画を managed で作り直す二重計画も 2026-11-14 に解消済み。承認済みグラフは同じフィールドのタスクJSONとして書き、`ego work run --task` / `ego work loop --task` にそのまま渡す（E8 参照）。`loop --task` の再計画は承認済み計画からの逸脱なので、使う前にユーザーの同意を取る。
- 入力側 I/O 契約（`requires`）は、`provides` の取り違えによる失敗が実際に観測されてから検討する。

---

## 4. ego 独自の追加要素（論文との差分）

上記論文に無い、または論文の要素を ego が具体化したもの。新しい要素を足したら、ここに1行追加する（導入コミットと根拠を書く）。

| ID | 要素 | 論文との差分 | 導入 |
|---|---|---|---|
| E1 | ノードごとの 5W1H（Who/What/When/Where/Why/How） | ⑦ の What/Where/How に Who（担当）・When（待つノード）・Why（目的）を追加し、要件と計画を同じノードに持たせる | `c17c930`（2026-11-13）`requirement` フィールド、`work-plan` Skill |
| E2 | 逐語実装可能な How ステップ | 各ステップがファイル・関数/節・正確な変更を名指しし、「検討する」「適宜」を禁止。③ の計画逸脱への直接対策 | `c17c930` `steps`（1〜10件）、planner プロンプト |
| E3 | ノードの `intent` / `done_when` / `non_goals` / `assumptions` / `provides` | 目的・受入条件・やらないこと・前提・後続への提供物をノード単位で明示。前提が偽ならワーカーは停止 | managed: `intent`/`provides` は rename 前から、他は `f065420`（2026-11-11）。inline `work-plan`: 2026-11-14 に同じ項目へ揃えた（v0.0.13〜0.0.14 の未改変 Skill は、この変更を含む ego の `ego apply` で自動更新。`install.py` `LEGACY_SKILL_HASHES`。リリース済み 0.0.14 の `ego apply` は旧テンプレートに戻すので、リリースまではこのリポジトリで `uv run ego apply` を使う） |
| E4 | インテーク質問 + 計画承認ゲート | 計画前に1問ずつ推奨案つきで確認し、承認まで編集しない | `c17c930` `work-plan` Skill |
| E5 | `write_allowlist` と wave の原子的マージ | ⑤ の並列実行に、ノードごとの書込範囲と同一パス衝突時の `MERGE_CONFLICT` 停止を追加 | `work.py`（rename `ef3928c` 以前から） |
| E6 | 検証済み・未適用の分離 | PASSED でも apply は明示承認が必要（`--apply --approve`）、`STALE_BASE` は迂回しない | `work.py` / `managed.md` |
| E7 | 有界な再計画入力 | 再計画は transcript ではなく、失敗ノード・切り詰めたチェック出力・変更パスだけを渡す（① の圧縮を再計画に適用） | `loop.py` |
| E8 | 承認済み計画をそのまま実行エンジンへ渡す | inline で承認したノードグラフを同じフィールドのタスクJSONに書き、`ego work plan` で作り直さずに `ego work run --task` / `ego work loop --task` で実行する。計画（人の承認）と実行（決定論的な基盤）を分けたまま一致させる（③⑤） | 2026-11-14（未コミット）。`loop.py` `run_loop(task=...)`: 承認済みタスクを1回目の計画として使い、2回目以降だけ planner を呼ぶ。台帳作成前に `validate_task` とコマンドポリシー（`check_command`）で検証し、壊れた計画が黙って再計画で置き換わるのを防ぐ。`--plan-first` との併用は `INVALID_LOOP` で拒否。台帳に `seeded_from_task` を記録。`cli.py`: `--task` 追加、`--goal` はタスクの goal が既定。手順は `work-plan` / `managed.md` 手順1 / `loop.md` |

---

## 5. 検証状況（2026-11-14）

- 実行済み: `uv run pytest` 223 passed（exit 0）、`uv run ego author lint` 問題なし（exit 0）。`uv run ego apply --check` の差分は `.opencode/plugins/ego.ts` の ego 実行パス（`.venv/bin/ego`）だけで、Skill / references はテンプレートと一致。
- 未実行: モデルを使う `ego author` ゲート、実ホストでの `ego work loop --task` 実行。E3 / E8 の効果（計画逸脱の減少など）は未計測。
