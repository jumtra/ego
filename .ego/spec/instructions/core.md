# custom-harness 共通指示

## 原則

- YAGNI / KISS / DRY。コメントは WHY のみ
- 言語・テストの詳細規約は path-scoped rule（`.ego/spec/rules/`）に従う

## CI

- CI_CHECK_CMD: `make test`
- CI_FIX_CMD: `未検出`
- 変更後は CI を実行し、出力を示してから完了と言う（証拠なき完了宣言は禁止）

## 変更禁止の設定ファイル

`- （検出なし）`。変更が必要ならユーザーに相談する。

## 作業の入口

- コード変更は `/work`（各ホストの Skill）
- 複数ユニットの作業は `seh work run --mode managed`

## Authoring（Prompt / Skill / Rule / Agent / Hook）

- 変更は `seh author` 経由。重要な変更は計画の承認を得てから編集する
- Eval 結果なしに完了と言わない。評価できなければ BLOCKED_NO_EVAL と報告する
- 正本は `.ego/spec`。生成物（`CLAUDE.md` / `AGENTS.md` / `.claude/` / `.agents/`）を直接編集しない

## 承認が必要な操作

- 高リスク操作（R2）・保護パスの変更・push / PR 作成は自分で実行せず、ユーザーに依頼する

## 知識と記憶

- ドメイン知識は `.ego/knowledge/domain.md`。常時読み込まず、必要な節だけ読む
- 記憶は `seh` の Memory（`memory.provider` に従う）を使う
