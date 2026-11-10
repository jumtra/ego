---
description: このリポジトリに ego を導入・再設定するときに使う。`seh init` の計画を確認して適用手順を案内する
claude:
  disable-model-invocation: true
---

# harness-init

## Goal

`seh init` の計画をユーザーが理解し、自分で適用して導入を完了できるようにする。

## Inputs

対象ホスト（claude / codex / antigravity / opencode）、プロファイル、`seh init --plan` の出力。

## 手順

1. `seh --version` を実行する。無ければ導入コマンド `uv tool install git+https://github.com/jumtra/ego` を案内して止まる。
2. `seh init --plan --hosts <hosts> --profile <profile>` を実行し、追加・変更・削除の内容と承認が必要な項目を要約する。
3. apply はユーザーのターミナルで実行してもらう。`seh init --apply --hosts <hosts> --profile <profile>` を案内する。
4. 適用後、`seh doctor` の結果で導入状態を確認する。

## Boundaries

- apply は承認が必要で、対話ターミナルの y/N でのみ受け付けられる。エージェントの Bash は非対話なので `seh init --apply` を実行しない。
- ホーム設定の変更や外部ツールのインストールを勝手に行わない。
- v1 資産の削除は `seh` の移行計画に従い、手で消さない。

## Output

計画の要約、ユーザーが実行するコマンド、適用後の `seh doctor` 結果。

## Feedback

`seh init --plan` と `seh doctor` の診断で判断する。ERROR が残るときは内容を報告し、推測で設定を直さない。
