# 参考文献 (References)

論文ごとに、ego が導入したもの / しないものと理由だけを記す。
inline = `work-plan` Skill の会話内計画、managed = `ego work plan` / `ego work run`。

## ① You don't need long context ([2611.05306](https://arxiv.org/abs/2611.05306))
- 導入: チェックポイント単位の文脈引き継ぎ（`context.py` / `checkpoint.md`）、選択的参照（`codeintel.py`）
  - 理由: 全履歴を持つより、圧縮と引き継ぎの方が低コストで同等の精度になるため

## ② Dive into Claude Code ([2604.14228](https://arxiv.org/abs/2604.14228))
- 導入: ガードレール（`policy.py` / `ownership.json`）、状態管理（`session.py` / `memory.py`）
  - 理由: エージェントの大半は決定論的な運用基盤であり、そこを堅くするのが効くため

## ③ From Plan to Action ([2604.12147](https://arxiv.org/abs/2604.12147))
- 導入: 計画（`work-plan` / `planner.py`）と実行（`work.py`）の分離、`evals.py` による完了検証、ループごとの状態再注入
  - 理由: 線形計画ではテスト工程のスキップや計画逸脱が起きるため

## ④ Graph Engineering ([2608.21156](https://arxiv.org/abs/2608.21156))
- 導入: タスク依存グラフ（`depends_on`）、共有状態（`memory.py` / run record）
  - 理由: 単一ループより依存と状態を明示した方が長いタスクで安定するため
- 導入（managed のみ）: 証拠必須の `judge` ノードによる局所リトライ（詳細は「ego 独自の追加要素」）
- 一部導入: 検証・リトライの循環
  - managed は `max_attempts` と `loop.py` の再計画で対応
  - inline 単体は「計画が誤りなら停止して承認」のみ。承認済み計画を `--task` で managed に渡せば同じ循環に乗る

## ⑤ Scheduler-Theoretic Framework ([2604.11378](https://arxiv.org/abs/2604.11378))
- 導入: 計画・実行・復旧の3層分離（`planner.py` / `work.py` / `loop.py`）
  - 理由: 責務を分けると障害箇所を特定しやすいため
- 導入（managed のみ）: 前提完了ノードだけのスケジュールと並列実行、ノード単位の障害追跡（`BLOCKED_DEPENDENCY`）
  - 理由: 並列化と正確な障害追跡に必要。inline はスケジューラを持たないため対象外
  - 並列は同一ツリーを共有せず、ノードごとの私的コピーで実行し、wave 単位で原子的にマージする（衝突は `MERGE_CONFLICT`）。動的な並列 DAG スケジューラの追加はしない

## ⑥ Atomic Task Graph ([2607.01942](https://arxiv.org/abs/2607.01942))
- 導入: 局所文脈（ワーカーは fresh session・`worker_context = "lean"`）
  - 理由: 参照範囲を絞るとハルシネーションが減るため
- 導入: I/O 契約 `inputs`（読むファイル、最大6件）と `outputs`（変更対象、`write_allowlist` 内に限定）。`provides` と併用
  - 理由: 入出力を固定するとノード間の実装のズレが減り、ワーカーが読む範囲も絞れる
- 一部導入（managed のみ）: 局所修復は「検証済み変更を引き継いで残作業だけ再計画」
  - 理由: 部分グラフ差し替えまでは不要
- 未導入: 部分グラフへの再帰コンパイル
  - 理由: 「最少ノード」方針と衝突し、計画コストが増える。対象が既存リポジトリの数ノード変更で、効果が見合わない

## ⑦ RPG: Repository Planning Graph ([2509.16198](https://arxiv.org/abs/2509.16198))
- 導入: What / Where / How を1ノードに統合（5W1H）、トポロジカル順の実装
  - 理由: 仕様・対象ファイル・手順を同じ場所に置くと整合が取れるため
- 一部導入: データフローは `provides` で代替
  - 理由: RPG はゼロからの大規模生成向け。ファイル間データフローのモデル化は ego の対象に対して過剰

## ego 独自の追加要素
- 5W1H ノード（`requirement` フィールド）
  - 理由: 要件と計画を同じノードに持たせるため
- 逐語実装可能な How ステップ（`steps` 1〜10件、「検討する」「適宜」を禁止）
  - 理由: ③ の計画逸脱を防ぐため
- `intent` / `done_when` / `non_goals` / `assumptions` / `provides`（managed と inline で同じ項目）
  - 理由: 受入条件と前提を明示し、前提が偽ならワーカーを止めるため。inline との差を無くすため
- インテーク質問と計画承認ゲート（`work-plan`）
  - 理由: 承認前の編集を防ぎ、誤った計画で実装しないため
- `write_allowlist` と wave の原子的マージ（衝突時 `MERGE_CONFLICT` 停止）
  - 理由: 並列ノードの書込衝突を黙って上書きしないため
- 検証済み・未適用の分離（`--apply --approve`、`STALE_BASE` は迂回しない）
  - 理由: PASSED でも適用は明示承認が必要なため
- `role`（`coder` / `tester` / `judge`）による書込権限の強制分離とモデル振り分け（`policy.py` / `task_routing.py` の `defaults.roles`）
  - 理由: 実装役がテストを書き換えて通す不正と、同一文脈での自己レビューを仕組みで防ぐため。`tester` は `tests/` のみ、`judge` は読み取り専用
- 役割別の host/model/effort の永続設定（`config.toml` の `roles`、`config.py` / `task_routing.py` の `default_roles`、`ego apply --roles`）
  - 優先順位: ノード > タスク `defaults.roles` > config `roles` > タスク `defaults` > config `models`。省略項目は `models` に従う
  - 理由: 実装は安価なモデル、judge は別ホストの強いモデルと、役割ごとにコストと独立性を調整するため（agy は表示名でなく `agy models` の ID を指定）
- 証拠必須の単発 Judge ゲート（`judge.py`）
  - `gate`/`code` ノードに依存させ、決定論的チェック通過後にだけ実行する。`path:line` のない指摘は破棄し、証拠なしの REJECT は APPROVE 扱い
  - REJECT 時は対象の `coder` ノードだけを最大 `max_retries`（既定1、上限2）回やり直す。`loop.py` の全体再計画は使わない
  - 理由: 自明な失敗を LLM に見せず、仕様の抜け漏れだけを低コストで監査するため
- 有界な再計画入力（失敗ノード・切り詰めたチェック出力・変更パスのみ）
  - 理由: transcript を渡さず、① の圧縮を再計画に適用するため
- 承認済み計画をそのまま実行（`ego work run --task` / `ego work loop --task`）
  - 理由: inline で承認した計画を managed で作り直す二重計画をやめ、承認内容と実行を一致させるため
  - `loop --task` の再計画は承認済み計画からの逸脱なので、使う前にユーザーの同意を取る

## 導入しないと判断したもの
- リポジトリ全体の統合グラフの事前構築（⑦ のフル実装）: 計画だけで大量のトークンと時間を使うため、`codeintel.py` で必要な箇所だけ取得する
- 部分グラフへの再帰コンパイル（⑥）: ノード数が増えて状態追跡が複雑になるため、フラットな3〜7ノードに保つ
- Reviewer と Critic に分けた多段の合議: API 呼び出しが3〜4倍になるため、単一の `judge` に「証拠なき指摘は破棄」を組み込む
- 同一ツリーでの動的な並列 DAG スケジューラ: 競合を避けるため、既存の私的コピーと原子的マージのままにする

## 検証状況（2026-11-14）
- 実行済み: `uv run pytest` 235 passed、`uv run ego author lint` 問題なし（`inputs`/`outputs`、`role`、judge ゲートのテストは `tests/test_judge_gate.py`）
- 未実行: モデルを使う `ego author` ゲート、実ホストでの `ego work loop --task`
- 未計測: 計画逸脱の減少など、導入効果
