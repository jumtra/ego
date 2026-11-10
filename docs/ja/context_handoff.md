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
| AGY | 自動実行・クリア未保証 | UIで新しいセッションを開き、`resume_prompt`を送る |

会話を途中で切れない作業、未検証の変更がある作業では、無理にクリアせず先にチェックポイントを保存し、必要ならホスト固有の`/compact`を使います。クリアの前後で、Git diff・実ファイル・テスト出力を再点検します。コンテキストは「会話ログ全部」ではなく「目的／実測結果／次の作業／必要なファイルへの参照」のみです。

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

トークン消費実測ができるホストが増えた場合はProviderごとのContext telemetryとして追加できます。ただし、未検証の`/clear`自動投入や前セッション全文の取り込みは行わず、ホスト実装に応じた安全なセッション開始APIが提供されてから自動化を検討します。

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

## 9. セッションクリアの自動提案（Claude Code、任意）

`.ego/config.toml` に `clear_suggest_tokens = 120000` のように設定して `ego sync --apply` すると、`.claude/settings.json` に Stop と SessionStart(clear) の hook が生成されます。既定は 0（無効）で、hook は一切追加されません。

- **提案**：ターン終了時に、ホストのセッション記録から直近リクエストのプロンプトトークン数（入力＋キャッシュ読み書き）を実測し、しきい値を超えていればユーザーにだけ `/clear` を提案します。推定値や割合は使わず、測れないときは何も出しません。同じセッションでは、前回の提案から 25% 以上増えるまで再提案しません。
- **自動再開**：30分以内に保存された対話チェックポイントがある場合、または `ego session resume <id>` を実行済みの場合、`/clear` 直後に再開コンテキストが自動で読み込まれます（1回限り、30分で失効）。入力欄には何も入らずターンも始まらないため、`/clear` 後に「続けて」などと一言送ると再開します。
- **しないこと**：チャットの消去、ターンのブロック、モデル呼び出し、モデルのコンテキストへの追記（提案時）はしません。hook の失敗がターンを止めることもありません。
- Codex / OpenCode には同等の仕組みが無いため、従来どおり手動です。

