"""Static guard: limit numbers come from src/ego/defaults.toml, never inline in code."""
import ast
import tomllib
from pathlib import Path

from ego.domain.config import ConfigSpec

ROOT = Path(__file__).resolve().parents[1]
SCANNED = [
    "src/ego/usecases/work.py",
    "src/ego/usecases/replan.py",
    "src/ego/usecases/planner.py",
    "src/ego/usecases/worker.py",
    "src/ego/domain/review.py",
    "src/ego/domain/task_routing.py",
    "src/ego/domain/task_schema.py",
    "src/ego/domain/config.py",
]
LIMIT_KEYWORDS = {"timeout", "max_turns", "limit"}
LIMIT_CLASSES = {"PlanSchemaLimits", "SessionSchemaLimits", "Config"}


def _is_int_literal(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) is int


def find_inline_limits(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "clip_output" and len(node.args) >= 2 and _is_int_literal(node.args[1]):
                found.append(f"{path}:{node.lineno}: clip_output limit literal")
            for kw in node.keywords:
                if kw.arg in LIMIT_KEYWORDS and _is_int_literal(kw.value):
                    found.append(f"{path}:{kw.value.lineno}: {kw.arg}= literal")
    for node in tree.body:
        if isinstance(node, ast.Assign) and _is_int_literal(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("MAX_"):
                    found.append(f"{path}:{node.lineno}: module-level {target.id} literal")
        if isinstance(node, ast.ClassDef) and node.name in LIMIT_CLASSES:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and _is_int_literal(stmt.value):
                    found.append(f"{path}:{stmt.lineno}: {node.name}.{stmt.target.id} int default")
    return found


def test_scanned_sources_have_no_inline_limits():
    # Arrange
    paths = [ROOT / p for p in SCANNED]

    # Act
    violations = [v for p in paths for v in find_inline_limits(p)]

    # Assert
    assert violations == []


def test_defaults_config_keys_match_config_spec():
    # Arrange
    data = tomllib.loads((ROOT / "src/ego/defaults.toml").read_text(encoding="utf-8"))

    # Act
    keys = set(data["config"])

    # Assert
    assert keys == set(ConfigSpec.model_fields)


def test_checker_flags_injected_clip_output_literal(tmp_path):
    # Arrange
    injected = tmp_path / "injected.py"
    injected.write_text("clip_output(x, 10)\n", encoding="utf-8")

    # Act
    found = find_inline_limits(injected)

    # Assert
    assert len(found) == 1 and "clip_output" in found[0]
