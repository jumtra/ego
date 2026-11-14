"""Strict configuration, with a tiny TOML writer for the supported primitives."""
from __future__ import annotations
from dataclasses import dataclass, field
import copy
import json
from pathlib import Path
import tomllib
from typing import Any
from .util import EgoError, atomic_text

HOSTS = ("claude", "codex", "opencode", "agy")
PROVIDERS = ("codegraph", "graphify", "none")
MEMORY = ("file", "engram")
POLICIES = ("strict", "relaxed")
ROLE_NAMES = ("coder", "tester", "judge")
VALID_EFFORTS = {"auto", "low", "medium", "high", "xhigh", "max"}
EFFORT_ALIASES = {"middle": "medium"}
MAX_ROLE_CANDIDATES = 8
# Host-neutral starting point: no `host` key, so the run's host decides which entry applies.
DEFAULT_ROLES: dict[str, Any] = {
    "coder": {"claude": [{"model": "haiku", "effort": ["low", "medium"]}, {"model": "sonnet"}], "codex": {"model": "auto", "effort": "medium"}},
    "tester": {"claude": {"model": "haiku", "effort": "low"}, "codex": {"model": "auto", "effort": "low"}},
    "judge": {"claude": {"model": "sonnet", "effort": "high"}, "codex": {"model": "auto", "effort": "high"}},
}


def parse_role_spec(value: Any, where: str = "role spec") -> list[tuple[str | None, str | None]]:
    """Expand a per-host role spec into ordered (model, effort) candidates; None means host default / auto.

    Accepts a {model, effort} table or a list of them; effort may be a list (["low", "middle"]) and fans out
    into one candidate each, in order.
    """
    def bad(msg: str) -> EgoError:
        return EgoError("INVALID_CONFIG", f"{where}: {msg}", 2)

    def model_of(raw: Any) -> str | None:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 128 or any(c in raw for c in " |"):
            raise bad("model must be a non-empty string without spaces or '|'")
        return None if raw.strip() in ("auto", "native-default") else raw.strip()

    def effort_of(raw: Any) -> str | None:
        raw = EFFORT_ALIASES.get(raw, raw)
        if not isinstance(raw, str) or raw not in VALID_EFFORTS:
            raise bad(f"effort must be one of {sorted(VALID_EFFORTS)} (or 'middle')")
        return None if raw == "auto" else raw

    out: list[tuple[str | None, str | None]] = []
    if isinstance(value, dict):
        if set(value) - {"model", "effort"}:
            raise bad("table takes only model/effort")
        m = model_of(value.get("model", "auto"))
        efforts = value.get("effort", "auto")
        for e in efforts if isinstance(efforts, list) else [efforts]:
            out.append((m, effort_of(e)))
    elif isinstance(value, list) and value:
        for item in value:
            out.extend(parse_role_spec(item, where))
    else:
        raise bad("must be a {model, effort} table or a non-empty list of them")
    if len(out) > MAX_ROLE_CANDIDATES:
        raise bad(f"at most {MAX_ROLE_CANDIDATES} candidates")
    return out

@dataclass
class Config:
    schema_version: int = 1
    profile: str = "balanced"
    policy_mode: str = "relaxed"
    hosts: list[str] = field(default_factory=lambda: ["claude", "codex", "opencode", "agy"])
    code_intelligence: str = "none"
    memory: str = "file"
    runtime_mode: str = "native"
    max_parallel: int = 2
    max_attempts: int = 3
    timeout_seconds: int = 3600
    max_turns: int = 30  # agentic turns per managed worker invocation
    worker_context: str = "lean"  # "lean" (no inherited MCP servers) or "inherit"
    clear_suggest_tokens: int = 200000  # Claude Code/Codex/OpenCode/AGY: at this measured context size the Stop hook forces a checkpoint and suggests /clear; 0 = off
    auto_resume: bool = True  # Claude Code/Codex/AGY: SessionStart(clear) hook injects the saved checkpoint after /clear (core; independent of clear_suggest_tokens)
    usage_switch_percent: int = 0  # Claude Code/Codex: at this % of the usage limit, checkpoint and propose another CLI; 0 = off
    run_yolo: bool = False  # `ego run` starts the CLI with its skip-permissions switch (--yolo / --no-yolo overrides per run)
    models: dict[str, Any] = field(default_factory=dict)
    roles: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULT_ROLES))  # role (coder/tester/judge) -> {host, model, effort}; same vocabulary as hosts/models
    isolation: str = "copy"  # "copy" (staging directory) or "worktree" (git worktree)
    cleanup_worktree: bool = True  # whether to remove the worktree after run
    tests: list[list[str]] = field(default_factory=list)
    allow_commands: list[str] = field(default_factory=lambda: ["python", "python3", "pytest", "git", "ruff", "npm", "pnpm", "yarn", "bun", "node", "deno", "tsc", "cargo", "rustc", "go", "dotnet", "swift", "swiftc", "cmake", "ctest", "mvn", "gradle", "gradlew", "java", "javac", "ruby", "bundle", "php", "composer"])

    def validate(self) -> None:
        if self.schema_version != 1 or self.profile not in {"minimal", "balanced", "full"}:
            raise EgoError("INVALID_CONFIG", "Unknown schema/profile", 2)
        if self.policy_mode not in POLICIES:
            raise EgoError("INVALID_CONFIG", f"policy_mode must be one of {POLICIES}", 2)
        if not self.hosts or any(h not in HOSTS for h in self.hosts) or len(set(self.hosts)) != len(self.hosts):
            raise EgoError("INVALID_CONFIG", "Invalid/duplicate hosts", 2)
        if self.code_intelligence not in PROVIDERS or self.memory not in MEMORY or self.runtime_mode not in ("native", "managed"):
            raise EgoError("INVALID_CONFIG", "Invalid provider/runtime", 2)
        if self.isolation not in ("copy", "worktree"):
            raise EgoError("INVALID_CONFIG", "isolation must be 'copy' or 'worktree'", 2)
        if not isinstance(self.cleanup_worktree, bool):
            raise EgoError("INVALID_CONFIG", "cleanup_worktree must be a boolean", 2)
        if not 1 <= self.max_parallel <= 8 or not 1 <= self.max_attempts <= 5 or not 5 <= self.timeout_seconds <= 7200:
            raise EgoError("INVALID_CONFIG", "Invalid runtime limits", 2)
        if not isinstance(self.max_turns, int) or not 1 <= self.max_turns <= 200:
            raise EgoError("INVALID_CONFIG", "max_turns must be between 1 and 200", 2)
        if not isinstance(self.clear_suggest_tokens, int) or not (self.clear_suggest_tokens == 0 or 20000 <= self.clear_suggest_tokens <= 2000000):
            raise EgoError("INVALID_CONFIG", "clear_suggest_tokens must be 0 (off) or between 20000 and 2000000", 2)
        if not isinstance(self.auto_resume, bool):
            raise EgoError("INVALID_CONFIG", "auto_resume must be a boolean", 2)
        if not isinstance(self.usage_switch_percent, int) or not (self.usage_switch_percent == 0 or 50 <= self.usage_switch_percent <= 99):
            raise EgoError("INVALID_CONFIG", "usage_switch_percent must be 0 (off) or between 50 and 99", 2)
        if not isinstance(self.run_yolo, bool):
            raise EgoError("INVALID_CONFIG", "run_yolo must be a boolean", 2)
        if self.worker_context not in ("lean", "inherit"):
            raise EgoError("INVALID_CONFIG", "worker_context must be 'lean' or 'inherit'", 2)
        if not isinstance(self.models, dict):
            raise EgoError("INVALID_CONFIG", "models must be a dictionary", 2)
        for h, m in self.models.items():
            if h not in HOSTS:
                raise EgoError("INVALID_CONFIG", f"Unsupported host '{h}' in models", 2)
            if isinstance(m, str):
                if not m.strip() or len(m) > 128:
                    raise EgoError("INVALID_CONFIG", f"Invalid model ID for host '{h}'", 2)
            elif isinstance(m, dict):
                model_name = m.get("model")
                if not isinstance(model_name, str) or not model_name.strip() or len(model_name) > 128:
                    raise EgoError("INVALID_CONFIG", f"Host '{h}' table config requires a valid 'model' string", 2)
                effort = m.get("effort")
                if effort is not None and (not isinstance(effort, str) or effort not in VALID_EFFORTS):
                    raise EgoError("INVALID_CONFIG", f"Host '{h}' effort must be one of {sorted(VALID_EFFORTS)}", 2)
                extra_keys = set(m.keys()) - {"model", "effort"}
                if extra_keys:
                    raise EgoError("INVALID_CONFIG", f"Unknown keys in models.{h}: {sorted(extra_keys)}", 2)
            else:
                raise EgoError("INVALID_CONFIG", f"Model config for host '{h}' must be a string or table", 2)
        if not isinstance(self.roles, dict):
            raise EgoError("INVALID_CONFIG", "roles must be a dictionary", 2)
        for r, c in self.roles.items():
            if r not in ROLE_NAMES:
                raise EgoError("INVALID_CONFIG", f"Unsupported role '{r}' in roles (use {ROLE_NAMES})", 2)
            if not isinstance(c, dict) or set(c) - {"host", "model", "effort", *HOSTS}:
                raise EgoError("INVALID_CONFIG", f"roles.{r} must be a table with only host/model/effort or per-host specs ({', '.join(HOSTS)})", 2)
            for h in HOSTS:
                if h in c:
                    parse_role_spec(c[h], f"roles.{r}.{h}")
            if "host" in c and c["host"] not in HOSTS:
                raise EgoError("INVALID_CONFIG", f"roles.{r}.host must be one of {HOSTS}", 2)
            if "model" in c and (not isinstance(c["model"], str) or not c["model"].strip() or len(c["model"]) > 128):
                raise EgoError("INVALID_CONFIG", f"roles.{r}.model must be a non-empty string", 2)
            if "effort" in c and c["effort"] not in VALID_EFFORTS:
                raise EgoError("INVALID_CONFIG", f"roles.{r}.effort must be one of {sorted(VALID_EFFORTS)}", 2)
        if any(not isinstance(cmd, list) or not cmd or any(not isinstance(x, str) for x in cmd) for cmd in self.tests):
            raise EgoError("INVALID_CONFIG", "Tests must be argv arrays", 2)

def _tomlval(value: Any) -> str:
    if isinstance(value, bool): return "true" if value else "false"
    if isinstance(value, str): return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)): return str(value)
    if isinstance(value, list): return "[" + ", ".join(_tomlval(x) for x in value) + "]"
    if isinstance(value, dict): return "{" + ", ".join(f"{k} = {_tomlval(v)}" for k, v in value.items()) + "}"
    raise TypeError(type(value))

def dump_config(cfg: Config) -> str:
    cfg.validate()
    header = (
        "# Generated by ego; edit only owned values.\n"
        "#\n"
        "# ポリシーモード (policy_mode):\n"
        "#   \"strict\":  安全重視（git push や設定ファイル直接編集を制限）\n"
        "#   \"relaxed\": ゆるいモード（個人開発向け。ユーザー依頼に基づく git push や設定編集を自律実行可能）\n"
        "#\n"
        "# モデル設定の例 (models):\n"
        "#   claude:   \"auto\", \"sonnet\", \"opus\", \"haiku\", etc.\n"
        "#             またはテーブル形式: { model = \"sonnet\", effort = \"medium\" }\n"
        "#             effort の選択肢: \"auto\", \"low\", \"medium\", \"high\", \"xhigh\", \"max\"\n"
        "#   codex:    \"auto\", \"gpt-5-codex\", etc.\n"
        "#   opencode: \"auto\", \"anthropic/claude-3-5-sonnet\", etc.\n"
        "#   agy:      \"auto\", \"gemini-2.5-pro\", etc.\n"
        "#\n"
        "# 役割別の host/model/effort (roles。models と同じ語彙。未指定の項目は models/hosts の設定に従う):\n"
        "#   roles = {judge = {host = \"agy\", model = \"gemini-2.5-pro\", effort = \"high\"}, coder = {host = \"claude\", model = \"sonnet\"}}\n"
        "#   ホスト別・複数指定: coder = {host = \"claude\", claude = [{model = \"haiku\", effort = [\"low\", \"middle\"]}, {model = \"sonnet\"}], codex = {model = \"gpt-5-codex\", effort = \"medium\"}}\n"
        "#     (リストは先頭から順に候補化。2つ以上なら UNAVAILABLE/TIMEOUT/RATE_LIMIT で次の候補へ fallback。middle は medium)\n"
        "#   優先順位: ノード > タスク defaults.roles > config roles > タスク defaults > config models\n"
        "#\n"
        "# 実行環境の隔離 (isolation, cleanup_worktree):\n"
        "#   isolation = \"copy\"      # ディレクトリコピーによるステージング\n"
        "#   isolation = \"worktree\"  # Git worktree によるブランチ作業分離\n"
        "#   cleanup_worktree = true  # タスク終了時に worktree を自動削除するかどうか\n"
        "#\n"
        "# Managed worker のコンテキスト (max_turns, worker_context):\n"
        "#   max_turns = 30             # worker 1 回あたりの最大ターン数\n"
        "#   worker_context = \"lean\"   # \"lean\": ユーザーの MCP サーバーを継承しない / \"inherit\": 継承する\n"
        "#\n"
        "# /clear 後の自動再開 (auto_resume, Claude Code / Codex / AGY / OpenCode。既定 true。SessionStart hook が保存済みチェックポイントを注入):\n"
        "#   auto_resume = true\n"
        "#\n"
        "# セッションクリアのしきい値 (clear_suggest_tokens, Claude Code / Codex / OpenCode / AGY。Stop hook が強制):\n"
        "#   clear_suggest_tokens = 200000  # 実測コンテキストがこの値に達したらチェックポイントを自動保存させ /clear を提案。0 で無効\n"
        "#\n"
        "# 使用量上限が近いときの自動保存と切り替え提案 (usage_switch_percent, Claude Code / Codex。使用量を報告しない OpenCode / AGY は対象外):\n"
        "#   usage_switch_percent = 85  # 5時間/7日枠の使用率がこの%以上でチェックポイント保存と別CLIへの切り替えを提案。0 で無効\n"
        "#\n"
        "# ego run の権限確認スキップ (run_yolo):\n"
        "#   run_yolo = true  # ego run が各CLIを権限確認なしで起動 (claude/agy: --dangerously-skip-permissions, codex: --dangerously-bypass-approvals-and-sandbox, opencode: --auto)。--yolo / --no-yolo で起動ごとに上書き\n"
    )
    return header + "\n".join(f"{k} = {_tomlval(v)}" for k, v in vars(cfg).items()) + "\n"

V2_SECTIONS = {"runtime", "sdk", "authoring", "evaluation", "evolution"}
HOST_ALIASES = {"antigravity": "agy"}
PROFILE_ALIASES = {"standard": "balanced"}
MEMORY_ALIASES = {"files": "file"}

def _normalize_loaded_data(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw)
    if str(data.get("schema_version")) in ("2", "2.0") or V2_SECTIONS & set(data):
        data["schema_version"] = 1
        for sec in V2_SECTIONS:
            val = data.pop(sec, None)
            if sec == "runtime" and isinstance(val, dict):
                if "default_mode" in val and "runtime_mode" not in data:
                    data["runtime_mode"] = val["default_mode"]
                if "max_concurrency" in val and "max_parallel" not in data:
                    data["max_parallel"] = val["max_concurrency"]
                if "max_fix_iterations" in val and "max_attempts" not in data:
                    data["max_attempts"] = val["max_fix_iterations"]
                if "timeout_sec" in val and "timeout_seconds" not in data:
                    data["timeout_seconds"] = val["timeout_sec"]
        if isinstance(data.get("code_intelligence"), dict):
            data["code_intelligence"] = data["code_intelligence"].get("provider", "none")
        if isinstance(data.get("memory"), dict):
            data["memory"] = data["memory"].get("provider", "file")
        if isinstance(data.get("hosts"), dict):
            data["hosts"] = data["hosts"].get("enabled", ["claude", "codex", "opencode", "agy"])
    if isinstance(data.get("profile"), str):
        data["profile"] = PROFILE_ALIASES.get(data["profile"], data["profile"])
    if isinstance(data.get("memory"), str):
        data["memory"] = MEMORY_ALIASES.get(data["memory"], data["memory"])
    if isinstance(data.get("hosts"), list):
        data["hosts"] = [HOST_ALIASES.get(h, h) if isinstance(h, str) else h for h in data["hosts"]]
    return data

def load_config(root: Path) -> Config:
    path = root / ".ego" / "config.toml"
    if not path.exists():
        legacy = root / ".harness" / "config.toml"
        if legacy.exists():
            path = legacy
        else:
            return Config()
    try: raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc: raise EgoError("INVALID_CONFIG", str(exc), 2) from exc
    data = _normalize_loaded_data(raw)
    unknown = set(data) - set(vars(Config()))
    if unknown: raise EgoError("INVALID_CONFIG", f"Unknown keys: {sorted(unknown)}", 2)
    cfg = Config(**data)
    cfg.validate()
    return cfg

def save_config(root: Path, cfg: Config):
    atomic_text(root / ".ego" / "config.toml", dump_config(cfg))

@dataclass
class OptConfig:
    schema_version: int = 1
    mode: str = "off"  # off, analyze, shadow, assisted
    profile: str = "lite"  # lite, full
    max_candidates: int = 1
    max_rounds: int = 1
    max_probe_runs: int = 2
    max_wall_seconds: int = 900
    max_candidate_bytes: int = 8192
    cost_policy: str = "manual_consent"
    require_human_approval: bool = True

    def validate(self) -> None:
        if self.schema_version != 1 or self.mode not in ("off", "analyze", "shadow", "assisted") or self.profile not in ("lite", "full"):
            raise EgoError("INVALID_OPT_CONFIG", "Bad mode/profile", 2)
        if self.max_candidates not in range(1, 9) or not 1 <= self.max_rounds <= 5 or not 0 <= self.max_probe_runs <= 40 or not 30 <= self.max_wall_seconds <= 7200 or not 100 <= self.max_candidate_bytes <= 32768:
            raise EgoError("INVALID_OPT_CONFIG", "Bad optimization limits", 2)
        if self.cost_policy not in ("manual_consent", "require_metered") or not self.require_human_approval:
            raise EgoError("INVALID_OPT_CONFIG", "Budget and approvals cannot be bypassed", 2)

def load_opt(root: Path) -> OptConfig:
    path = root / ".ego" / "optimization.toml"
    if not path.exists(): return OptConfig()
    try: data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc: raise EgoError("INVALID_OPT_CONFIG", str(exc), 2) from exc
    unknown = set(data) - set(vars(OptConfig()))
    if unknown: raise EgoError("INVALID_OPT_CONFIG", f"Unknown: {sorted(unknown)}", 2)
    result = OptConfig(**data); result.validate(); return result

def save_opt(root: Path, opt: OptConfig) -> None:
    opt.validate()
    atomic_text(root / ".ego" / "optimization.toml", "# Loaded only by ego optimize.\n" + "\n".join(f"{k} = {_tomlval(v)}" for k,v in vars(opt).items()) + "\n")
