# v0.0.1 初回スタンドアロンリリース

- **スタンドアロンCLI実装**: 外部SDKに依存しないポータブルな `seh` / `harness` CLI。
- **マルチホスト対応**: Claude / Codex / OpenCode Hostとモデル指定、Fallback/Compare戦略に対応。
- **タスク・DAG実行**: タスク仕様に基づいたDAGノード実行、変更ファイルの検証、安全なステージング適用。
- **CLIバージョン・互換性検知**: ベンダーCLI引数のキャッシュ、バイナリ変更検出、引数変更への追従。
- **ホスト・モデル管理**: `harness hosts inspect|refresh`、`harness models list|refresh`。
- **検証機能付き自己更新**: GitHub Release APIのSHA-256ダイジェストとWheelメタデータによる完全性検証（`harness update`）。
- **オフラインデモ & E2Eテスト**: オフラインで動作確認可能なデモプロジェクトおよびテストスイート同梱。
