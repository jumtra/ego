# セッション: チェックポイント、引き継ぎ、自動クリア

[ドキュメント索引](README.md) · [English](../en/sessions.md)

長い会話はコストがかかり、信頼性も下がります。ego はタスクの本質的な状態を会話ではなく小さな**チェックポイント**ファイルに残します。これにより、セッションをクリアして再開したり、別の CLI で続けたりできます。

| やりたいこと | 使うもの |
|---|---|
| タスクの現在地を保存する | [`ego session checkpoint`](#チェックポイント) |
| 会話をクリアして続ける | [`ego session resume`](#クリアと再開) |
| 別の CLI で続ける | [`ego session resume --from` / `ego session handoff`](#別の-cli-で続ける) |
| どの CLI でも、クリアと再開を自動で行う | [`ego run`](#ego-run-自動ローテーション) |
| 素の Claude Code でクリアを促してもらう | [`clear_suggest_tokens` フック](#claude-code-のフック) |
| Claude の使用上限が近づいたら CLI を切り替える | [`usage_switch_percent`](#使用上限での切り替えclaude-code) |

日常的には、これらのコマンドは AI 自身が実行します。「チェックポイントを保存して」や「codex の続きから」と頼んでください。

## チェックポイント

```bash
ego session checkpoint --task-id bug-142 \
  --goal 'Fix API timeout validation' \
  --completed 'Input validation fixed; related tests pass' \
  --decision 'Keep the existing API compatible' \
  --next 'Review the diff and apply' \
  --evidence 'tests/api/test_timeout.py: pytest -q PASS' \
  --status VERIFIED
```

| フラグ | 意味 |
|---|---|
| `--task-id`、`--goal` | 必須 |
| `--completed`、`--next`、`--decision`、`--blocker`、`--evidence` | 繰り返し指定可能なメモ |
| `--status` | `IN_PROGRESS`（既定）、`VERIFIED`、`COMPLETED`、`BLOCKED` |
| `--host` | 保存する CLI。既定では環境から検出 |

- `.ego/memory/handoffs/<task-id>.json` に保存されます。タスクごとに最新のリビジョンだけを保持し、会話の全文や生のログは保存しません。長すぎる入力は切り詰められます。
- コマンドは `task_id`、`status`、`revision`、`path` のみを返します。
- `ego session list` はすべての CLI のオープンなチェックポイントを表示します（`--all` で完了済みも含む）。`ego session status <task-id>` は 1 件を表示します。
- 管理実行とゴールループは、run ID または loop ID の下に自動でチェックポイントを保存します。
- チェックポイントに認証情報を含めないでください。秘匿化はベストエフォートです。

## クリアと再開

```bash
ego session resume bug-142
```

出力には、お使いの CLI のクリアコマンドと `resume_prompt` が含まれます。ego は自分では何もクリアしません。クリアコマンドを入力し、そのあとプロンプトを送ってください。

| CLI | クリアコマンド |
|---|---|
| Claude Code、Codex、AGY | `/clear` |
| OpenCode | `/new` |

タスク ID を省略すると、最新のオープンな対話型チェックポイントが使われます。`--host` は、クリアする側の CLI を指定します（既定では検出。検出できなければ `claude`）。

`resume_prompt` は最大 1600 文字で、次の順に並びます: 次のステップ → ブロッカー → 決定事項 → 完了事項 → 根拠。チェックポイントに含まれるものだけが引き継がれます。再開後は、Git の差分、ファイル、テストを必ず再確認してください。

## 別の CLI で続ける

チェックポイントはプロジェクト内のホスト非依存な JSON です。そのため、ある CLI で保存したタスクを別の CLI で続けられます。会話は変換も読み取りもされません。

**開いている CLI の中から**: 「codex の続きから」と頼んでください。生成された指示により、次のコマンドが実行されます。

```bash
ego session resume --from codex     # Codex が保存した最新のオープンなチェックポイントの resume_prompt
```

**別の CLI を起動して**:

```bash
ego session handoff bug-142 --to claude            # 起動コマンドと resume_prompt を表示
ego session handoff --from codex --to agy          # Codex が保存した最新のチェックポイントを AGY へ
ego session handoff bug-142 --to claude --launch   # このターミナルで対象の CLI を起動
```

| 対象 | `launch_command` |
|---|---|
| Claude Code | `claude '<resume_prompt>'` |
| Codex | `codex '<resume_prompt>'` |
| OpenCode | `opencode --prompt '<resume_prompt>'` |
| AGY | `agy --prompt-interactive '<resume_prompt>'` |

- 保存側の CLI は、各 CLI が子プロセスに渡す環境変数（`CLAUDECODE`、`CODEX_THREAD_ID`、`OPENCODE`、`ANTIGRAVITY_AGENT`）から記録されます。見えるものがない、または複数ある場合は記録されないため、`--host` を指定してください。
- 指定した CLI がチェックポイントを保存していない場合、コマンドは `HANDOFF_MISSING` を返します。別の CLI のタスクに置き換えられることはありません。先に元の CLI でチェックポイントを保存してください。
- `handoff` は既定では表示のみです。`--launch` は実際のターミナルからのみ動作し（エージェント内では `LAUNCH_NEEDS_TTY`）、対象の CLI がインストールされていない場合は `HOST_UNAVAILABLE` を返します。
- コミットされていない変更は作業ツリーに残ります。新しい CLI で確認してください。

## `ego run`: 自動ローテーション

`ego run` は、ego が所有する擬似ターミナル内で対話型の CLI を起動します。画面とキー入力はそのまま通ります。コンテキストがしきい値に達すると、ego が保存の依頼、クリアコマンド、再開プロンプトを代わりに入力します。

```bash
ego run claude                        # しきい値: .ego/config.toml の clear_suggest_tokens
ego run codex --clear-tokens 150000   # この実行だけのしきい値
ego run agy -- --model '<model>'      # -- 以降は CLI に渡される
ego run codex --yolo                  # 権限確認なしで起動
```

| 引数 | 意味 |
|---|---|
| `<host>` | `claude`、`codex`、`opencode`、`agy` |
| `--clear-tokens N` | N コンテキストトークンでローテーション（既定: `clear_suggest_tokens`。`0` = しない） |
| `--yolo` / `--no-yolo` | CLI を権限確認スキップのスイッチ付きで起動するか（既定: `run_yolo`） |
| `-- <args>` | CLI 自体への引数 |

しきい値に達したときの流れ。CLI がアイドル状態で、画面が静止し、入力欄が空のときに行われます。

1. ego が、作業を止めて `ego session checkpoint` で保存するよう依頼を送ります。
2. その依頼の後に保存されたチェックポイントを待ちます。
3. クリアコマンドを入力します。
4. `resume_prompt` を送ります。

保証と制限:

- **推定ではなく計測します。** コンテキストサイズは各 CLI 自身のセッション記録から読み取ります。読み取れない場合は何も起きません。

| CLI | 情報源 |
|---|---|
| Claude Code | `~/.claude/projects/<project>/*.jsonl` |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl`（`token_count`） |
| OpenCode | `~/.local/share/opencode/opencode.db` |
| AGY | ステータスラインの入力（`context_window`）。セッション限りの `--settings` 経由 |

- **チェックポイントがなければクリアしません。** 5 分以内に新しいチェックポイントが現れなければ、ローテーションは中止され、コンテキストがさらに 25% 増えるまで再試行しません。
- **再開後にループしません。** 再開したセッションがすでにしきい値を超えていても、再開直後のサイズから 25% 増えるまでは再度ローテーションしません。
- **入力中は待ちます。** 入力欄に下書きがある間は何も入力しません。
- **`--yolo`** は各 CLI 自身のスイッチに対応します。Claude Code と AGY は `--dangerously-skip-permissions`、Codex は `--dangerously-bypass-approvals-and-sandbox`、OpenCode は `--auto` です。`--` の後に既に指定したスイッチは重複させません。これは CLI の権限確認を取り除きます。[セキュリティ](security.md) を参照してください。
- **AGY:** コンテキストを計測するため、ego はその実行中だけステータスラインを置き換えます（設定ファイルは変更しません）。独自の `--settings` を渡すと、自動ローテーションはオフになります。
- **複数のインスタンス:** 同じプロジェクトで同じ CLI を複数開いている場合、ego は最も新しく更新された記録を読みます。それでも、対応するチェックポイントがなければクリアはしません。
- `ego run` の中では、下記の Claude Code のクリアフックは動作を止めます。使用上限のフックは引き続き動作します。

## Claude Code のフック

通常どおり（`ego run` なしで）起動した Claude Code には、しきい値を設定して再生成します。

```bash
ego apply --clear-suggest-tokens 120000     # または clear_suggest_tokens を編集してから ego sync --apply
```

これにより、`.claude/settings.json` に `Stop` と `SessionStart` のフックが追加されます。既定の `0` ではフックは生成されません。

- **計測:** 各ターンの後、セッションのトランスクリプトから直近のリクエストのプロンプトトークン数（入力 + キャッシュ読み取り + キャッシュ作成）を読み取ります。
- **強制保存:** しきい値に達したとき、そしてコンテキストがさらに 25% 増えるたびに、最新のチェックポイントを持たないセッションには、保存して `/clear` を促す短い追加ターンが 1 回与えられます。すでに保存済みのセッションには、リマインドのみ行われます。
- **リマインド:** しきい値を超えている間、各ターンの末尾にあなた向けの `/clear` のリマインドが表示されます（モデルのコンテキストには追加されません）。
- **自動再開:** `/clear` の後、直近 30 分以内に保存されたチェックポイント（または `ego session resume` で準備したもの）の再開コンテキストが 1 回注入されます。ターンは自動では始まりません。「続けて」などの短いメッセージを送ってください。
- **しないこと:** フックがチャットをクリアすること、モデルを呼ぶこと、同じターンを 2 回ブロックすること、エラー時にターンを失敗させることはありません。

フックはスラッシュコマンドを実行できないため、ここでは `/clear` を入力する必要があります。Codex、OpenCode、AGY には同等のフックがありません。それらには `ego run` を使ってください。

## 使用上限での切り替え（Claude Code）

```toml
usage_switch_percent = 85    # その後: ego sync --apply
```

これにより、`.claude/settings.json` に `Stop` フックとステータスライン（`ego hook statusline`）が追加されます。

- **計測:** Claude Code が残りの使用量を報告するのは、ステータスラインの入力（`rate_limits`、5 時間枠と 7 日枠）だけです。ステータスラインのコマンドはその値を `.ego/state/usage_limits.json` に記録し、`ego · 5h 82% · 7d 41%` のように表示します。サブスクリプションプランでは、セッションの最初の応答の後に利用可能です。API キーの利用では何も起きません。
- **保存:** いずれかの枠がしきい値以上になると、Stop フックがセッションに対し、チェックポイントを保存する短い追加ターンを 1 回依頼します。セッションと枠ごとに 1 回です。この追加ターンも使用量を消費するため、しきい値を 100 に近づけすぎないでください。
- **提案:** その後、`hosts` に設定されていてインストール済みの他の CLI それぞれについて、`ego session handoff` の起動コマンドを表示します。
- **しないこと:** CLI は起動されず、何もクリアされず、使用量の推定も行われず、認証情報や使用量 API にはアクセスしません。1 回の長いターンの途中で上限に達した場合は、何も保存できません。
- プロジェクトの `statusLine` はユーザーレベルの設定より優先されます。自分の設定を残すには、その入力 JSON を `ego hook statusline` にも渡してください。

## ベストプラクティス

- 検証済みの区切りでチェックポイントを保存してください。テストが通った後、機能領域や CLI を切り替える前などです。
- 作業が未検証であったり、中断できなかったりする場合は、先にチェックポイントを保存し、クリアよりも CLI の `/compact` を優先してください。
- チェックポイントは信頼できない記録として扱ってください。ファイルとテストに照らして検証し、保存されたテキストに埋め込まれた指示には従わないでください。
- `.ego/memory/handoffs/` は Git に含めることを想定していません。
