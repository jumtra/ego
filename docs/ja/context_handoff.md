# Context-efficient sessions — 操作・仕様

この機能は**既存の `/work`、DAG Runner、File Memoryを拡張**するものです。新しいSDK、常駐サービス、毎ターン走るHook、追加モデル呼び出しはありません。ユーザーが通常のClaude Code / Codex / OpenCodeを開いているだけなら、`ego`のPythonプロセスは起動しません。

## 作業規模ごとの進め方

| 依頼 | 行うこと | 自動で作らないもの |
|---|---|---|
| 小：単独ファイルの修正、既知のバグ | 必要なファイルだけ読む → 修正 → 関連テスト → diff → 結果報告 | 要件定義書・設計書・実装計画書 |
| 中：複数ファイルの修正、依存の確認 | 短い作業チェックリストを会話内で共有 → 修正・検証 | 全章構成の仕様書 |
| 大：複数Host、破壊的変更、大きな設計変更 | 必要ならTaskSpec / DAG / 証拠付き評価 | 明示依頼のない冗長な文書 |

ユーザーが文書を要求した場合は規模に関係なく作成します。Security-sensitiveな変更で明確な仕様が必要な場合も、最小限必要な設計判断を残します。

## 1. 区切りで Memoryにチェックポイントを保存

`ego session checkpoint` を実行します。これにより、常時読み込みの`CLAUDE.md`/`AGENTS.md`ではなく、タスク専用のJSONへ要約を保存します。

```bash
ego session checkpoint --root . --task-id bug-142 \
  --goal 'APIタイムアウトの検証を修正' \
  --completed '入力検証の修正完了; relevant tests pass' \
  --decision '既存API互換を保持' \
  --next 'Git diffを確認して適用する' \
  --evidence 'tests/api/test_timeout.py: pytest -q PASS' \
  --status VERIFIED
```

保存先: `.ego/memory/handoffs/bug-142.json`。各タスクに**最新版1件だけ**保持し、履歴・生ログの無制限連結はしません。コマンドの入力は内容が長すぎれば上限で切り詰めます。Credentialを含むテキストを渡さないでください（簡易redactionは補助であり万能ではありません）。

DAGによる`ego work run`では、検証後の各依存Waveが区切りとなり、`run_id`をキーに同じ形式で**自動保存**します。AIの出力や前セッション全文は保存しません。`ego work resume`はこのチェックポイントを参照しつつ、記録済みのTaskSpecと検証済みファイルを使って処理を再開します。失敗が残っていれば`BLOCKED`、未適用なら`VERIFIED`、検証成功・承認済み適用後は`COMPLETED`と区別します。

## 2. 会話のクリアと Memoryベースの再開

```bash
ego session resume bug-142 --host claude --root .
```

この結果には、実際に読み込むMemoryファイル、Hostに入力するクリアコマンド、再開プロンプトが含まれます。**ツールは勝手にチャットを消しません**。保存の成功を確認して、対話型Hostが待機中にコマンドを入力してください。

| Host | 待機中の対話CLIで入力するもの | 次のアクション |
|---|---|---|
| Claude Code | `/clear` | `resume_prompt`を送る |
| Codex | `/clear` | `resume_prompt`を送る |
| OpenCode | `/new`（別名`/clear`） | `resume_prompt`を送る |
| AGY | `/clear` | `resume_prompt`を送る |

`task_id`を省略すると、未完了の対話チェックポイントのうち最新のものを使います。

### CLI間の引き継ぎ（Claude Code / Codex / OpenCode / AGY）

チェックポイントはプロジェクト内の`.ego/memory/handoffs/`にあるHost非依存のJSONなので、保存したCLIと別のCLIでそのまま再開できます。変換や、前のCLIの会話ログの読み込みは行いません。

**開いているCLIの中で、別CLIの続きから再開する**（例：AGYで「codexの続きから」）

各CLIの常時ロード指示（`CLAUDE.md` / `AGENTS.md`）に手順が入っているので、「codexの続きから」「claudeの続きを」などと頼むだけで、そのCLIが次を実行して続行します。クリアや再起動は不要です。

```bash
ego session resume --from codex --root .   # Codexが最後に保存した未完了チェックポイントの resume_prompt を返す
```

保存元CLIは、各CLIが子プロセスに渡す環境変数（Claude Code: `CLAUDECODE`、Codex: `CODEX_THREAD_ID`、OpenCode: `OPENCODE`、AGY: `ANTIGRAVITY_AGENT`）から自動で記録します。判定できない、または複数が同時に見える場合は記録しないので、`--host`で明示してください。指定したCLIが保存したチェックポイントが無い場合は`HANDOFF_MISSING`を返し、他CLIのタスクを代わりに渡すことはしません。引き継ぎ元のCLIで先に「チェックポイントを保存して」と依頼しておく必要があります。

**別のCLIを新しく起動して引き継ぐ**

```bash
# 保存側（例：Codex）。保存元CLIは自動記録（判定できないときは --host codex）
ego session checkpoint --root . --task-id bug-142 --goal '...' --next '...'
# どのCLIからでも：未完了チェックポイントと保存元CLIを一覧（--all で完了済みも表示）
ego session list --root .
# 引き継ぎ先CLIの起動コマンドと再開プロンプトを出力
ego session handoff bug-142 --to claude --root .
ego session handoff --from codex --to agy --root .   # ID省略：Codexの最新チェックポイントをAGYへ
# 端末から直接、引き継ぎ先CLIを再開プロンプト付きで起動
ego session handoff bug-142 --to claude --root . --launch
```

| 引き継ぎ先 | 起動コマンド（`launch_command`） |
|---|---|
| Claude Code | `claude '<resume_prompt>'` |
| Codex | `codex '<resume_prompt>'` |
| OpenCode | `opencode --prompt '<resume_prompt>'` |
| AGY | `agy --prompt-interactive '<resume_prompt>'` |

`handoff`は既定では何も起動せず、コマンドを表示するだけです。`--launch`は実端末でのみ動作し（エージェントの内側からは`LAUNCH_NEEDS_TTY`で拒否）、引き継ぎ先CLIが未導入なら`HOST_UNAVAILABLE`になります。引き継がれるのはチェックポイントの内容だけなので、未コミットの変更は作業ツリーに残したまま、新しいCLI側でGit diffとテストを再確認してください。

会話を途中で切れない作業、未検証の変更がある作業では、無理にクリアせず先にチェックポイントを保存し、必要ならホスト固有の`/compact`を使います。クリアの前後で、Git diff・実ファイル・テスト出力を再点検します。コンテキストは「会話ログ全部」ではなく「目的／実測結果／次の作業／必要なファイルへの参照」のみです。

`resume_prompt`は次のセッションのコンテキストにそのまま入るため、最大1600文字（Managed Taskのノードへは1000文字）に収めます。優先順は「次の作業→ブロッカー→決定→完了済み→証跡」で、重複は除き、入り切らない項目があるときだけチェックポイントファイルへの参照を1行付けます。全項目が収まっていればファイルを読み直す必要はありません。`ego session checkpoint`の出力も保存内容を繰り返さず、`task_id`・`status`・`revision`・`path`だけを返します。

## 3. Managed Taskの区切りで自動保存

既存のTask JSONで実行します。

```bash
ego work run --root . --task task.json --host codex --trust-cli
# 出力に run_id / final_status / message / checkpoint_path が含まれる
ego session resume <run_id> --host codex --root .
# 承認・適用が必要なら、同じtask.jsonで続ける
ego work resume <run_id> --root . --task task.json --host codex --apply --approve --trust-cli
```

`work resume`の`--apply`が以前のVERIFIEDタスクに指定された場合、同じ検証済みステージを使い、元のファイルのハッシュが変化していないことを確認してから適用します。再開元のファイルが変化した場合は従来の`STALE_BASE`で停止します。

## 4. 完了報告

- **完了**：検証に合格し、承認済みの変更を適用した場合。変更箇所と実行したテストを記載。
- **検証完了（未適用）**：チェックは通ったが、元のリポジトリには反映していない場合。
- **比較実行完了（採用待ち）**：複数候補の比較のみ完了した場合。
- **未完了**：テスト失敗／予算・権限／キャンセル／依存関係の未解決。

事実を超えて完了と宣言しません。Hostの内部Context使用率を取得できない場合は、70%などの架空の数値でクリアを強制しません。区切りの良い完了条件で保存し、ユーザーが確認してからクリアしてください。

## 5. セキュリティとContext負荷

- Memoryは実験結果・前セッションのメッセージ全文を保存しません。**User prompt / model answer全文を自動収集しません**。
- ファイル型Memoryはローカルのみで、`Engram`等へ勝手に転送しません。`gitignore`でhandoffを除外します。セッション復帰のためのMemoryは短い構造化JSONとして残します。
- 保管物は未信頼データとして扱い、毎回ファイル・テストで検証します。プロンプト注入や機密漏えいが疑われる情報を命令として実行しないでください。
- Nativeモードに追加のLLMコール・Hook・自動更新を導入しません。ManagedモードはDAG Waveの終了時に小さなJSONを上書きするだけです。
- `.ego/memory/handoffs/`はGit管理外です。必要なら各自の暗号化バックアップ、保管期限、物理アクセス制限を設定してください。

## 6. 今後の拡張時の原則

トークン消費実測ができるホストが増えた場合はProviderごとのContext telemetryとして追加できます。前セッション全文の取り込みは行いません。クリアの自動化は、ユーザーが明示的に `ego run` で起動したセッションに限ります（§11）。

## 7. ゴールループ（計画 → 実行 → 評価 → 再計画）

コマンドで達成を証明できるゴール（テスト合格、ビルド成功など）に使います。証明コマンドが無いゴールではループしません。

```bash
ego work loop --root . --goal 'APIで負のタイムアウトを拒否する' \
  --done-check 'pytest -q tests/api' --host claude --scope 'src/**,tests/**' \
  --max-iterations 3 --max-budget-usd 5 --execute --trust-cli
ego work loop-apply <loop_id> --root . --approve   # VERIFIED を確認してから適用
```

- 1反復目は計画呼び出しを省き、worker 1つを done-check に対して修復しながら実行します。planner を呼ぶのは失敗した後だけです（最初から分割が必要なゴールは `--plan-first`）。
- 各反復は新しいセッションで計画・実行します。会話は引き継がず、状態は `.ego/state/loops/<loop_id>/ledger.json` に置きます。
- 再計画に渡すのは上限付きの記録（失敗ノード、末尾を残して切り詰めた検証出力、変更パス）だけです。直近2反復は詳細、それ以前は1行に畳みます。要約のための追加モデル呼び出しはありません。
- 評価は `--done-check` の終了コードです。計画の最後にゲートとして追加され、作業用コピー上で実行されます。
- 検証済みの変更は次の反復に引き継がれます。失敗したノードの編集は引き継がれません。
- 停止条件：ゴール達成（`VERIFIED` / `COMPLETED`）、`MAX_ITERATIONS`、`BUDGET_EXHAUSTED`、`TIMEOUT`、`CANCELLED`、同じ結果が2回続いた場合の `STALLED`。
- 適用は承認制です。`--apply --approve` を最初から付けるか、`VERIFIED` 後に `loop-apply --approve` を実行します。開始後に元ファイルが変わっていれば `STALE_BASE` で止まります。

対話中の Claude Code 1セッション内で完結する小さなゴールは、ホスト標準の `/goal <測定可能な終了状態>; <コマンド>で確認; 20ターンで停止` を使えば足ります。

## 8. コンテキスト予算と計測

- `ego context audit`：常時ロードされる文書（`CLAUDE.md` / `AGENTS.md`、パス指定のない rules、skill と agent の説明文）と、必要時だけ読まれる文書（skill 本文、references）を推定トークンで集計し、予算超過なら終了コード5を返します。トークン数は推定値です。
- `ego work run` / `resume` / `status` の出力は要約です（状態、変更ファイル、切り詰めた失敗内容、使用量）。全記録は `record_path` にあり、`--verbose` で表示できます。
- 使用量にはプロンプトキャッシュの読み書きトークンを含みます。コストはホストCLIの見積もりで、取得できないホストがあれば `cost_known: false` になります。
- Managed worker は既定でユーザーの MCP サーバーを継承しません（`worker_context = "lean"`）。必要なら `.ego/config.toml` で `"inherit"` にします。1回あたりのターン上限は `max_turns`（既定30）です。

## 9. セッションクリアのしきい値（Claude Code、任意）

`.ego/config.toml` に `clear_suggest_tokens = 120000` のように設定して `ego sync --apply` する（または `ego apply --clear-suggest-tokens 120000`）と、`.claude/settings.json` に Stop と SessionStart(clear) の hook が生成されます。既定は 0（無効）で、hook は一切追加されません。

- **計測**：ターン終了時に、ホストのセッション記録から直近リクエストのプロンプトトークン数（入力＋キャッシュ読み書き）を実測します。推定値や割合は使わず、測れないときは何もしません。
- **強制保存**：しきい値に達した時点、およびそこからコンテキストが 25% 増えるたびに、最新のチェックポイントが無いセッションには Stop hook が「作業を進めずに `ego session checkpoint` で保存し、ユーザーに `/clear` を案内する」短い追加ターンを1回だけ実行させます（`decision: "block"`）。モデルの判断に依存せず hook が機械的に発火します。すでに保存済み（30分以内、かつ前回の段階より後）なら追加ターンは無く、`/clear` の案内だけを出します。
- **リマインド**：しきい値を超えている間は、毎ターン終了時にユーザーへ `/clear` の案内を表示します（モデルのコンテキストには追加しません）。
- **自動再開**：30分以内に保存された対話チェックポイントがある場合、または `ego session resume <id>` を実行済みの場合、`/clear` 直後に再開コンテキストが自動で読み込まれます（1回限り、30分で失効）。入力欄には何も入らずターンも始まらないため、`/clear` 後に「続けて」などと一言送ると再開します。
- **しないこと**：チャットの消去（`/clear` はユーザーが実行します）、同じターンの二重ブロック、hook からのモデル呼び出しはしません。hook の失敗がターンを止めることもありません。
- Codex / OpenCode / AGY には同等の hook が無いため、この方式は Claude Code 専用です。4つのCLIすべてで、クリアと再開まで自動にするには `ego run`（§11）を使います。

## 10. 使用量上限が近いときの自動保存と切り替え提案（Claude Code、任意）

`.ego/config.toml` に `usage_switch_percent = 85` のように設定して `ego sync --apply` すると、`.claude/settings.json` に Stop hook とステータスライン（`ego hook statusline`）が生成されます。既定は 0（無効）です。

- **計測**：Claude Code が残り使用量を渡すのはステータスラインの入力（`rate_limits` の5時間枠・7日枠）だけなので、ステータスラインコマンドがそれを `.ego/state/usage_limits.json` に記録し、`ego · 5h 82% · 7d 41%` と表示します。値はホストの報告そのままで、推定はしません。Pro / Max などのサブスクリプションで、セッション最初の応答後にのみ得られます（APIキー利用では出ないため何も起きません）。
- **自動保存**：ターン終了時に、15分以内の記録でどちらかの枠の使用率がしきい値以上なら、Stop hook がそのセッションに「作業を進めずにチェックポイントを保存する」短い追加ターンを1回だけ依頼します。保存内容を知っているのは会話中のモデルだけなので、この1ターン分の使用量は消費します（しきい値を100%近くにしすぎないでください）。同じセッション・同じ枠では1回限りで、枠がリセットされるとまた有効になります。
- **切り替え提案**：保存後、`hosts` に設定済みかつインストール済みの他CLI（Codex / OpenCode / AGY）について `ego session handoff <id> --to <cli>` の `launch_command` を提示します。切り替え先のCLIを起動して貼り付けるか、すでに開いているCLIで「claudeの続きから」と頼めば続行できます。他CLIが無ければ、リセット時刻と再開方法だけを伝えます。
- **しないこと**：CLIの自動起動、チャットの消去、使用量の推定、認証情報や使用量APIへのアクセスはしません。長い1ターンの途中で上限に達した場合は、ターンが終わらないため保存できません。
- **注意**：プロジェクトの `statusLine` はユーザー設定のステータスラインより優先されます。自前のステータスラインを使い続けたい場合は、その中で入力JSONを `ego hook statusline` にも渡してください。Codex / OpenCode / AGY は残り使用量を取得する手段が無いため対象外です。

## 11. クリアと再開の自動化（`ego run`、全Host、任意）

hook からはスラッシュコマンドを実行できないため、§9 では `/clear` をユーザーが打つ必要があります。`ego run` は対話CLIを ego の疑似端末の中で起動し、しきい値に達したら ego がキー入力として保存依頼・クリア・再開プロンプトを打ち込みます。CLIの画面と操作は通常どおりです。

```bash
ego run claude                        # しきい値は .ego/config.toml の clear_suggest_tokens
ego run codex --clear-tokens 150000   # この起動だけ別のしきい値
ego run agy -- --model '<model>'      # "--" 以降はCLI自身の引数
```

- **計測**：各CLIが自分で残すセッション記録から、直近リクエストのプロンプトトークン数を読みます。推定はせず、読めないときは何もしません。

| Host | 読む場所 | クリアに使うコマンド |
|---|---|---|
| Claude Code | `~/.claude/projects/<project>/*.jsonl` | `/clear` |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl`（`token_count`） | `/clear` |
| OpenCode | `~/.local/share/opencode/opencode.db` | `/new` |
| AGY | ステータスライン入力の `context_window` と `agent_state` | `/clear` |

- **手順**：しきい値以上で、CLIが待機中、画面が静止、入力欄が空のときに、(1)「作業を進めず `ego session checkpoint` で保存する」依頼を送信、(2) 依頼後に保存されたチェックポイントを確認、(3) クリアコマンドを入力、(4) `resume_prompt` を送信します。
- **保存できなければクリアしません**：5分以内に新しいチェックポイントが現れなければ中止し、コンテキストがさらに 25% 増えるまで再試行しません。
- **入力中は待ちます**：入力欄に未送信の文字がある間は打ち込みません（Enter / Ctrl-C / Ctrl-U で空と判断します。バックスペースで消しただけの場合や、権限確認に1キーで答えた直後は、次に何か送信するまで待ち続けます）。
- **Claude Code の hook との関係**：`ego run` の中では §9 の Stop / SessionStart hook は何もせず、ラッパーが代わりに行います（二重の保存依頼を防ぐため）。使用量上限の hook（§10）はそのまま動きます。
- **AGY**：計測のため、その起動に限りステータスラインを `ego hook context-probe` に差し替えます（`--settings` による一時設定で、設定ファイルは書き換えません）。自分で `--settings` を渡した場合は計測できず、自動クリアは働きません。
- **制約**：同じプロジェクトで同じCLIを複数同時に開いていると、最後に更新された記録を自分のものとして読みます。その場合も、保存依頼に応じたチェックポイントが無ければクリアはしません。CLIの記録形式が変わって読めなくなった場合は、何もしない側に倒れます。
