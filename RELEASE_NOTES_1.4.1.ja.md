# v1.4.1 更新・互換性機能

- タスク・DAG node毎のClaude/Codex/OpenCode HostとModel指定、Fallback/Compare。
- CLIバージョン・引数情報のキャッシュ、バイナリ変更検出、既知引数への切替。
- `harness hosts inspect|refresh`、`harness models list|refresh`。
- 署名済みReleaseではなくGitHub Release APIが報告するSHA-256ダイジェストとWheelメタデータによる完全性検証を実施。独立した発行者署名の保証はありません。
- `harness update --check`、確認付き更新、`--sync`、検証済みローカルWheel更新。
- `--autonomy auto`既定。完全権限バイパスは外部Sandbox宣言と明示指定が必要。
- 外部ホストCLIの自動インストール・更新、未知のCLI引数の自動解釈、AGYのManaged実行は未実装。
- 実ホスト認証済みE2E、GitHub Releaseの実ネットワーク更新は未検証。
