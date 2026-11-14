<div align="center">

# ego

**ひとつの依頼を、検証できる変更へ。**

Claude Code・Codex・OpenCode・Antigravity に、計画・タスク分担・テスト・記憶を。

![Version](https://img.shields.io/badge/version-0.0.14-2563eb)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-475569)

[English](../../README.md) · **日本語**

</div>

## 概要

`ego` は AI コーディング CLI（Claude Code、Codex、OpenCode、Antigravity / `agy`）のためのハーネスです。各 CLI 向けに共通の `/work` skill、agent、指示を生成し、検証付きの複数ホストによるタスク実行、メモリ、セッションの引き継ぎのためのコマンドを提供します。Python 以外の実行時依存はありません。

<picture>
  <source media="(max-width: 600px)" srcset="assets/workflow-ja-mobile.svg">
  <img src="assets/workflow-ja.svg" alt="依頼 → 計画 → タスク定義に従って画面とAPIを分担 → テスト → 承認して適用" width="1120">
</picture>

| | やりたいこと | ego がすること |
|---|---|---|
| ⚡ | いつもの AI CLI ですぐ直す | `/work` が調査・実装・テストを行う。小さな修正に計画書は不要 |
| 🧩 | 複数の AI CLI に分担する | ノードごとにホストとモデルを指定。依存関係を守り、独立したノードは並列実行 |
| ✅ | 確かめてから反映する | ステージング用コピーか Git worktree で変更とテストを行い、承認後にのみ適用 |
| 🔁 | コマンドが通るまで作業する | ゴールループ: 計画 → 実行 → 評価 → 再計画。イテレーション、予算、時間で上限を設定 |
| 💾 | 後で、または別の CLI で続ける | Claude Code / Codex / OpenCode / AGY の間でメモリ、チェックポイント、引き継ぎ |
| ♻️ | 長いセッションを小さく保つ | `ego run <cli>` がコンテキストのしきい値でチェックポイント、クリア、再開を行う |

---

## クイックスタート

### 1 · インストール

macOS / Linux で Python **3.11 以上**が必要です。

```bash
uv tool install git+https://github.com/jumtra/ego    # または: pipx install git+https://github.com/jumtra/ego
ego --version
```

リリース wheel、開発用インストール、更新: [セットアップ](setup.md)

### 2 · プロジェクトへ適用

```bash
cd /path/to/project
ego apply          # 対話ウィザード。変更一覧を確認して y で確定
```

スクリプトから指定する場合:

```bash
ego apply --hosts claude,codex --profile balanced --policy strict
```

これにより `.ego/config.toml` と、選択した CLI 向けの skill、agent、指示ブロックが書き込まれます。すべてのオプションと生成ファイル: [セットアップ](setup.md)。すべての設定項目: [設定](configuration.md)

### 3 · 作業する

プロジェクトで AI CLI を起動し、次のように依頼します。

```text
/work Add timeout input validation and run the related tests
```

`/work` が日常的な入口です。AI は依頼に合う最も軽いワークフローを選び、管理された実行が必要なときは `ego` コマンドを自分で実行します。コンテキストの上限でセッションを ego に自動ローテーションさせたい場合は、ego を通じて CLI を起動します。

```bash
ego run claude     # codex、opencode、agy も可
```

### 4 · 保守する

| コマンド | 用途 |
|---|---|
| `ego doctor` | ホスト CLI、プロバイダ、設定を確認 |
| `ego status` | `.ego/config.toml` と生成ファイルのズレを表示 |
| `ego sync --apply` | `.ego/config.toml` を編集した後、ファイルを再生成 |
| `ego update --yes --sync-all` | ego を更新し、登録済みの全プロジェクトを再生成 |
| `ego rollback` | 直前の apply を取り消す |
| `ego uninstall` | プロジェクトから ego を削除（`--cli` で CLI のシンボリックリンクを削除） |

---

## ドキュメント

| トピック | English | 日本語 |
|---|---|---|
| インストール、適用、更新、アンインストール | [Setup](../en/setup.md) | [セットアップ](setup.md) |
| `.ego/config.toml`、環境変数 | [Configuration](../en/configuration.md) | [設定](configuration.md) |
| `/work`、管理実行、タスク JSON、ゴールループ | [Workflows](../en/workflows.md) | [ワークフロー](workflows.md) |
| チェックポイント、CLI 間の引き継ぎ、`ego run` | [Sessions](../en/sessions.md) | [セッション](sessions.md) |
| 全コマンドとフラグ | [Command reference](../en/commands.md) | [コマンドリファレンス](commands.md) |
| 信頼の境界と制限 | [Security](../en/security.md) | [セキュリティ](security.md) |

ライセンス: [MIT](../../LICENSE)
