"""TTY-only opt-in setup wizard. Never used on the non-interactive fast path.

All wizard text is written to stderr so the CLI's JSON stdout contract stays
stable for scripts. The wizard has no file, network or model side effects.
"""
from __future__ import annotations

import sys
from pathlib import Path
from .config import Config, HOSTS, PROVIDERS, MEMORY


class WizardCancelled(Exception):
    """User declined or input ended. The project has not been changed."""


def should_prompt(args) -> bool:
    """Interactive in a terminal unless the invocation clearly requests automation."""
    if args.interactive:
        return True
    if args.non_interactive or args.dry_run or args.check:
        return False
    # Explicit selections are a complete non-interactive invocation by default.
    if any(getattr(args, key, None) is not None for key in
           ("profile", "hosts", "graph", "memory", "runtime")) or args.detect_tests:
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def _write(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def _read(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt as exc:
        raise WizardCancelled from exc
    if not line or line.strip().lower() in {"q", "quit", "cancel"}:
        raise WizardCancelled
    return line.strip()


def _choose(label: str, choices: tuple[str, ...], current: str) -> str:
    _write("\n" + label)
    for index, choice in enumerate(choices, 1):
        selected = " (current)" if choice == current else ""
        _write(f"  {index}. {choice}{selected}")
    current_index = choices.index(current) + 1
    while True:
        answer = _read(f"番号または名前 [Enter: {current_index}; q: 中止]: ")
        if not answer:
            return current
        if answer.isdecimal() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        if answer in choices:
            return answer
        _write("無効な選択です。上記の番号または名前を入力してください。")


def _multiple(label: str, choices: tuple[str, ...], selected: list[str]) -> list[str]:
    _write("\n" + label)
    for i, choice in enumerate(choices, 1):
        _write(f"  [{'x' if choice in selected else ' '}] {i}. {choice}")
    default = ",".join(str(i) for i, choice in enumerate(choices, 1) if choice in selected)
    while True:
        answer = _read(f"番号をカンマ区切り（例 1,2）、all、またはEnterで維持 [現状: {default}; q: 中止]: ")
        if not answer:
            return list(selected)
        if answer.lower() == "all":
            return list(choices)
        parts = [part.strip() for part in answer.split(",")]
        if parts and all(part.isdecimal() and 1 <= int(part) <= len(choices) for part in parts):
            wanted = {choices[int(part) - 1] for part in parts}
            return [choice for choice in choices if choice in wanted]
        if parts and all(part in choices for part in parts):
            wanted = set(parts)
            return [choice for choice in choices if choice in wanted]
        _write("少なくとも1つのHostを選択してください。番号をカンマ区切りで入力できます。")


def _yes_no(label: str, *, default: bool = False) -> bool:
    shown = "Y/n" if default else "y/N"
    while True:
        answer = _read(f"{label} [{shown}; q: 中止]: ").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        _write("y または n を入力してください。")


def select_options(cfg: Config, args, root: Path) -> bool:
    """Mutate *in-memory* config; return whether test auto-detection was requested."""
    _write(f"\n=== self-evolve-harness 適用設定: {root} ===")
    _write("既存設定がある場合は初期値として使用します。Enterで維持、qで中止。")
    _write("ここで選ぶHostは設定ファイルの生成先です。TaskごとのHost/Modelは別途変更できます。")
    if args.profile is None:
        cfg.profile = _choose("プロファイル（minimal=軽量、balanced=標準、full=追加Hook）", ("minimal", "balanced", "full"), cfg.profile)
    if args.hosts is None:
        cfg.hosts = _multiple("設定を生成するHost（複数選択可）", HOSTS, cfg.hosts)
    if args.graph is None:
        cfg.code_intelligence = _choose("コード解析Provider（外部Providerは別途セットアップが必要）", PROVIDERS, cfg.code_intelligence)
    if args.memory is None:
        cfg.memory = _choose("Memory（Engramは外部接続が必要）", MEMORY, cfg.memory)
    if args.runtime is None:
        cfg.runtime_mode = _choose("既定の実行方式（managedはRunnerと適切な実行環境が必要）", ("native", "managed"), cfg.runtime_mode)
    detect_tests = False
    if not cfg.tests and not args.detect_tests:
        detect_tests = _yes_no("テストコマンド候補を検出して設定に追加しますか？（実行はしません）")
    cfg.validate()
    return detect_tests


def confirm_apply(cfg: Config, changes: list[dict], root: Path) -> bool:
    """Only called when there is a real diff, no conflicts and no dry-run."""
    by_action: dict[str, int] = {}
    for change in changes:
        by_action[change["action"]] = by_action.get(change["action"], 0) + 1
    _write("\n=== 変更内容の確認 ===")
    _write(f"対象: {root}")
    _write(f"設定: profile={cfg.profile}; policy={cfg.policy_mode}; hosts={','.join(cfg.hosts)}; graph={cfg.code_intelligence}; memory={cfg.memory}; runtime={cfg.runtime_mode}")
    _write("変更件数: " + ", ".join(f"{name}={count}" for name, count in sorted(by_action.items())))
    for change in changes:
        _write(f"  {change['action']}: {change['path']}")
    _write("ユーザー所有ファイルの衝突時は自動上書きせずに停止します。適用前スナップショットを作成します。")
    return _yes_no("この設定を適用しますか？", default=False)
