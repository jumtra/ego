"""Strict configuration, with a tiny TOML writer for the supported primitives."""
from __future__ import annotations
from dataclasses import dataclass, fields
import copy
import json
from pathlib import Path
import tomllib
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from ..shared.util import EgoError, atomic_text
from .defaults import package_defaults, tuning_values
from .session_schema import SessionSchemaLimits
from .task_schema import PlanSchemaLimits

HOSTS = ("claude", "codex", "opencode", "agy")
PROVIDERS = ("codegraph", "graphify", "none")
KNOWLEDGE = ("file", "alter")  # where project knowledge (terms, glossary, decisions) lives
VALID_EFFORTS = {"auto", "low", "medium", "high", "xhigh", "max"}
EFFORT_ALIASES = {"middle": "medium"}

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
    cap = package_defaults()['tuning']['max_role_candidates']
    if len(out) > cap:
        raise bad(f"at most {cap} candidates")
    return out

NonEmptyModelStr = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
HostLiteral = Literal["claude", "codex", "opencode", "agy"]
RoleLiteral = Literal["coder", "tester", "reviewer"]
EffortLiteral = Literal["auto", "low", "medium", "high", "xhigh", "max"]


class AlterAnalyzerEntrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: NonEmptyModelStr


def _normalize_effort(v: Any) -> Any:
    return EFFORT_ALIASES.get(v, v) if isinstance(v, str) else v


class ModelTableSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: NonEmptyModelStr
    effort: EffortLiteral | None = None

    @field_validator("effort", mode="before")
    @classmethod
    def _normalize_effort_field(cls, v: Any) -> Any:
        return _normalize_effort(v)


class RoleConfigSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: HostLiteral | None = None
    model: NonEmptyModelStr | None = None
    effort: EffortLiteral | None = None
    claude: Any = None
    codex: Any = None
    opencode: Any = None
    agy: Any = None

    @field_validator("effort", mode="before")
    @classmethod
    def _normalize_effort_field(cls, v: Any) -> Any:
        return _normalize_effort(v)



def validate_max_run_tokens(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (value == 0 or value >= 1000):
        raise ValueError("max_run_tokens must be 0 (off) or >= 1000")
    return value


def resolve_max_run_tokens(cfg: Config, override: int | None) -> int:
    value = cfg.max_run_tokens if override is None else override
    try:
        return validate_max_run_tokens(value)
    except ValueError as exc:
        raise EgoError("INVALID_CONFIG", str(exc), 2) from exc


class ConfigSpec(BaseModel):
    """Pydantic schema for validating Config attributes mechanically."""
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    profile: Literal["minimal", "balanced", "full"]
    policy_mode: Literal["strict", "relaxed"]
    hosts: Annotated[list[HostLiteral], Field(min_length=1)]
    code_intelligence: Literal["codegraph", "graphify", "none"]
    knowledge: Literal["file", "alter"]
    runtime_mode: Literal["native", "managed"]
    max_parallel: Annotated[int, Field(ge=1, le=8)]
    max_attempts: Annotated[int, Field(ge=1, le=5)]
    max_rounds: Annotated[int, Field(ge=0, le=5)]
    max_replans: Annotated[int, Field(ge=0, le=5)]
    max_run_tokens: int
    timeout_seconds: Annotated[int, Field(ge=5, le=7200)]
    max_turns: Annotated[int, Field(ge=1, le=200)]
    worker_context: Literal["lean", "inherit"]
    worker_segment_turns: Annotated[int, Field(ge=1, le=200)]
    clear_suggest_tokens: int
    auto_resume: bool
    usage_switch_percent: int
    run_yolo: bool
    alter_analysis: bool
    alter_analyzer: dict[HostLiteral, AlterAnalyzerEntrySpec]
    alter_analysis_timeout_seconds: Annotated[int, Field(gt=0)]
    models: dict[HostLiteral, NonEmptyModelStr | ModelTableSpec]
    roles: dict[RoleLiteral, RoleConfigSpec]
    isolation: Literal["copy", "worktree", "auto"]
    cleanup_worktree: bool
    tests: list[Annotated[list[str], Field(min_length=1)]]
    allow_commands: list[str]

    @field_validator("hosts")
    @classmethod
    def _validate_unique_hosts(cls, v: list[HostLiteral]) -> list[HostLiteral]:
        if len(set(v)) != len(v):
            raise ValueError("Invalid/duplicate hosts")
        return v

    @field_validator("max_run_tokens")
    @classmethod
    def _validate_max_run_tokens(cls, v: int) -> int:
        return validate_max_run_tokens(v)

    @field_validator("clear_suggest_tokens")
    @classmethod
    def _validate_clear_suggest_tokens(cls, v: int) -> int:
        if not (v == 0 or 20000 <= v <= 2000000):
            raise ValueError("clear_suggest_tokens must be 0 (off) or between 20000 and 2000000")
        return v

    @field_validator("usage_switch_percent")
    @classmethod
    def _validate_usage_switch_percent(cls, v: int) -> int:
        if not (v == 0 or 50 <= v <= 99):
            raise ValueError("usage_switch_percent must be 0 (off) or between 50 and 99")
        return v

    @field_validator("roles")
    @classmethod
    def _validate_roles(cls, v: dict[RoleLiteral, RoleConfigSpec]) -> dict[RoleLiteral, RoleConfigSpec]:
        for r, c in v.items():
            for h in HOSTS:
                if h in c.model_fields_set:
                    parse_role_spec(getattr(c, h), f"roles.{r}.{h}")
        return v


def _format_config_error(err: ValidationError) -> str:
    first = err.errors()[0]
    loc = first.get("loc", ())
    err_type = first.get("type", "")
    msg = first.get("msg", "Invalid configuration")
    if loc == ("alter_analysis",):
        return "alter_analysis must be a boolean"
    if loc == ("alter_analysis_timeout_seconds",):
        return "alter_analysis_timeout_seconds must be an integer > 0"
    if loc == ("alter_analyzer",):
        return "alter_analyzer must be a dictionary"
    if len(loc) >= 2 and loc[0] == "alter_analyzer":
        host = loc[1]
        if len(loc) == 3 and loc[2] == "[key]":
            return f"Unsupported host '{host}' in alter_analyzer"
        if len(loc) == 3 and loc[2] == "model" and err_type != "missing":
            return f"alter_analyzer.{host}.model must be a non-empty string"
        return f"alter_analyzer.{host} must be a table with only 'model'"
    if len(loc) >= 2 and loc[0] == "models" and len(loc) == 3 and loc[2] == "[key]":
        return f"Unsupported host '{loc[1]}' in models"
    if len(loc) >= 2 and loc[0] == "roles" and len(loc) == 3 and loc[2] == "[key]":
        return f"Unsupported role '{loc[1]}' in roles"
    path = ".".join(str(p) for p in loc)
    return f"{path}: {msg}" if path else msg


@dataclass(frozen=True)
class Tuning:
    max_feedback_chars: int
    max_summary_chars: int
    max_record_chars: int
    max_context_hits: int
    max_save_turns: int
    max_handoff_chars: int
    max_role_candidates: int
    plan_schema: PlanSchemaLimits
    session_schema: SessionSchemaLimits


def load_tuning(root: Path | None) -> Tuning:
    t = tuning_values(root)
    return Tuning(
        max_feedback_chars=t["max_feedback_chars"],
        max_summary_chars=t["max_summary_chars"],
        max_record_chars=t["max_record_chars"],
        max_context_hits=t["max_context_hits"],
        max_save_turns=t["max_save_turns"],
        max_handoff_chars=t["max_handoff_chars"],
        max_role_candidates=t["max_role_candidates"],
        plan_schema=PlanSchemaLimits(**t["plan_schema"]),
        session_schema=SessionSchemaLimits(**t["session_schema"]),
    )


@dataclass
class Config:
    schema_version: int
    profile: str
    policy_mode: str
    hosts: list[str]
    code_intelligence: str
    knowledge: str  # "file" (.ego/knowledge index + topic files) or "alter" (alter MCP); process memory (`ego memory`) is always local
    runtime_mode: str
    max_parallel: int
    max_attempts: int
    max_rounds: int  # reviewer rejections allowed per node
    max_replans: int  # plan revisions allowed per task
    max_run_tokens: int  # per-invocation billable token cap (resets on resume; cache_read excluded; 0 = off)
    timeout_seconds: int
    max_turns: int  # agentic turns per managed worker invocation
    worker_context: str  # "lean" (no inherited MCP servers) or "inherit"
    worker_segment_turns: int  # managed Claude worker: turns per segment; context is measured between segments
    clear_suggest_tokens: int  # Claude Code/Codex/OpenCode/AGY: at this measured context size the Stop hook forces a checkpoint and suggests /clear; 0 = off
    auto_resume: bool  # Claude Code/Codex/AGY: SessionStart(clear) hook injects the saved checkpoint after /clear (core; independent of clear_suggest_tokens)
    usage_switch_percent: int  # Claude Code/Codex: at this % of the usage limit, checkpoint and propose another CLI; 0 = off
    run_yolo: bool  # `ego run` starts the CLI with its skip-permissions switch (--yolo / --no-yolo overrides per run)
    alter_analysis: bool  # opt-in: after a session ends, run the analyzer host headless to attach analysis to the recorded conversation (knowledge=alter only)
    alter_analyzer: dict[str, Any]  # host -> {model} used for analysis; overrides the session host
    alter_analysis_timeout_seconds: int  # per-analysis wall-clock limit in seconds; must be > 0
    models: dict[str, Any]
    roles: dict[str, Any]  # role (coder/tester/reviewer) -> {host, model, effort}; same vocabulary as hosts/models
    isolation: str  # "auto" (worktree only when writer nodes can run in parallel), "copy" (staging directory) or "worktree" (git worktree)
    cleanup_worktree: bool  # whether to remove the worktree after run
    tests: list[list[str]]
    allow_commands: list[str]
    tuning: Tuning

    def plan_schema_limits(self) -> PlanSchemaLimits:
        return self.tuning.plan_schema

    def session_schema_limits(self) -> SessionSchemaLimits:
        return self.tuning.session_schema

    def validate(self) -> None:
        try:
            ConfigSpec.model_validate(_owned(self))
        except ValidationError as err:
            raise EgoError("INVALID_CONFIG", _format_config_error(err), 2) from err


def _owned(cfg: Config) -> dict[str, Any]:
    return {k: v for k, v in vars(cfg).items() if k != "tuning"}


def default_config(root: Path | None = None) -> Config:
    return Config(**copy.deepcopy(package_defaults()["config"]), tuning=load_tuning(root))


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
        "#   roles = {reviewer = {host = \"agy\", model = \"gemini-2.5-pro\", effort = \"high\"}, coder = {host = \"claude\", model = \"sonnet\"}}\n"
        "#   ホスト別・複数指定: coder = {host = \"claude\", claude = [{model = \"haiku\", effort = [\"low\", \"middle\"]}, {model = \"sonnet\"}], codex = {model = \"gpt-5-codex\", effort = \"medium\"}}\n"
        "#     (リストは先頭から順に候補化。2つ以上なら UNAVAILABLE/TIMEOUT/RATE_LIMIT で次の候補へ fallback。middle は medium)\n"
        "#   優先順位: ノード > タスク defaults.roles > config roles > タスク defaults > config models\n"
        "#\n"
        "# 実行環境の隔離 (isolation, cleanup_worktree):\n"
        "#   isolation = \"auto\"      # 既定。同時に書き込むノードが 2 つ以上ある run だけ worktree、それ以外は copy\n"
        "#   isolation = \"copy\"      # ディレクトリコピーによるステージング\n"
        "#   isolation = \"worktree\"  # 常に Git worktree によるブランチ作業分離\n"
        "#   cleanup_worktree = true  # タスク終了時に worktree を自動削除するかどうか\n"
        "#\n"
        "# Managed worker のコンテキスト (max_turns, worker_context):\n"
        "#   max_turns = 30             # worker 1 回あたりの最大ターン数\n"
        "#   worker_context = \"lean\"   # \"lean\": ユーザーの MCP サーバーを継承しない / \"inherit\": 継承する\n"
        "#   worker_segment_turns = 10  # Claude worker をこのターン数ごとに区切り、全ホストで clear_suggest_tokens 超過時はチェックポイントから新セッションで再開\n"
        "#\n"
        "# 1回の実行（run / resume）あたりのトークン上限 (max_run_tokens。既定 0 = 無制限。設定時は 1000 以上。cache_read は除外。resume ごとにリセット):\n"
        "#   max_run_tokens = 0\n"
        "#\n"
        "# /clear 後の自動再開 (auto_resume, Claude Code / Codex / AGY / OpenCode。既定 true。SessionStart hook が保存済みチェックポイントを注入):\n"
        "#   auto_resume = true\n"
        "#\n"
        "# セッションクリアのしきい値 (clear_suggest_tokens, Claude Code / Codex / OpenCode / AGY。Stop hook が強制):\n"
        "#   clear_suggest_tokens = 150000  # 実測コンテキストがこの値に達したらチェックポイントを自動保存させ /clear を提案。0 で無効\n"
        "#\n"
        "# 使用量上限が近いときの自動保存と切り替え提案 (usage_switch_percent, Claude Code / Codex。使用量を報告しない OpenCode / AGY は対象外):\n"
        "#   usage_switch_percent = 99  # 5時間/7日枠の使用率がこの%以上でチェックポイント保存と別CLIへの切り替えを提案。0 で無効\n"
        "#\n"
        "# ego run の権限確認スキップ (run_yolo):\n"
        "#   run_yolo = true  # ego run が各CLIを権限確認なしで起動 (claude/agy: --dangerously-skip-permissions, codex: --dangerously-bypass-approvals-and-sandbox, opencode: --auto)。--yolo / --no-yolo で起動ごとに上書き\n"
        "#\n"
        "# セッション終了後の分析 (alter_analysis, knowledge=alter のみ。既定 false = オプトイン。モデル費用が発生):\n"
        "#   alter_analysis = false\n"
        "#   alter_analyzer = {opencode = {model = \"opencode/glm-5.3-flash-free\"}}  # ホスト別の分析モデル。未指定はセッションのホストを使用\n"
        "#   alter_analysis_timeout_seconds = 600  # 1回あたりの制限時間 (秒, > 0)\n"
        "#\n"
        "# 細かいパラメーターは .ego/defaults.toml の [tuning] で上書き\n"
    )
    return header + "\n".join(f"{k} = {_tomlval(v)}" for k, v in _owned(cfg).items()) + "\n"

V2_SECTIONS = {"runtime", "sdk", "authoring", "evaluation", "evolution"}
HOST_ALIASES = {"antigravity": "agy"}
PROFILE_ALIASES = {"standard": "balanced"}
KNOWLEDGE_ALIASES = {"files": "file", "alter-mcp": "alter"}

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
            data["memory"] = data["memory"].get("provider", "file")  # read as the legacy `memory` key below
        if isinstance(data.get("hosts"), dict):
            data["hosts"] = data["hosts"].get("enabled", ["claude", "codex", "opencode", "agy"])
    if isinstance(data.get("profile"), str):
        data["profile"] = PROFILE_ALIASES.get(data["profile"], data["profile"])
    if "memory" in data:  # legacy key: `memory` named the provider that is now `knowledge`
        legacy = data.pop("memory")
        data.setdefault("knowledge", legacy)
    if isinstance(data.get("knowledge"), str):
        data["knowledge"] = KNOWLEDGE_ALIASES.get(data["knowledge"], data["knowledge"])
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
            return default_config(root)
    try: raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc: raise EgoError("INVALID_CONFIG", str(exc), 2) from exc
    data = _normalize_loaded_data(raw)
    for key in ("plan_schema", "session_schema"):
        if key in data: raise EgoError("INVALID_CONFIG", f"{key} moved to .ego/defaults.toml [tuning.{key}]", 2)
    unknown = set(data) - ({f.name for f in fields(Config)} - {"tuning"})
    if unknown: raise EgoError("INVALID_CONFIG", f"Unknown keys: {sorted(unknown)}", 2)
    cfg = Config(**{**package_defaults()["config"], **data}, tuning=load_tuning(root))
    cfg.validate()
    return cfg

def save_config(root: Path, cfg: Config):
    atomic_text(root / ".ego" / "config.toml", dump_config(cfg))
