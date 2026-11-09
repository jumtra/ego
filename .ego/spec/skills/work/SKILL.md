---
description: コードの変更・機能追加・バグ修正・リファクタリング・Skill/Prompt の作成や修正を依頼されたときに使う。質問だけの依頼には使わない
---

# work

## Goal

依頼を検証可能な形で完了させる。完了宣言は証拠の提示が前提（CI 出力・テスト結果・Gate 判定）。

## Inputs

依頼文、リポジトリの現状、`seh` コマンドの出力。

## 手順

1. 読み取り専用で探索する（関連ファイル、既存テスト、プロジェクトの CI コマンド）。
2. 依頼を分類する。
   - 小さな単一変更: そのまま TDD（Red → Green → Refactor）で進める。
   - 複数ファイル・依存のある作業: `seh work run --task <file> --mode managed` を提案する。タスクファイルは `references/managed-task.md` に従って作る。
   - Skill/Prompt/Rule/Agent/Hook の変更: `seh author plan` → `seh author lint` → `seh author eval` の順に実行し、承認が必要なものはユーザーに依頼する。
3. 変更する。
4. プロジェクトの CI コマンドを実行し、出力を示す。
5. 完了報告する。

## Boundaries

- 承認が必要な操作は実行しない。R2 操作・保護パスの変更・外部への push/PR はユーザーに依頼する。
- `.ego/spec` が正本。生成物を直接編集しない。
- 評価できないときは「未検証」と報告し、成功と言わない。
- native ではタスク DAG の厳密な外部制御は保証されない。保証が必要なら managed を使う。

## Output

変更概要、検証の証拠（コマンドと出力）、残リスク。

## Feedback

CI・`seh author lint`・`seh doctor` の結果で判断する。失敗は最大3回まで修正し、超えたら原因と試行を報告して止まる。
