"""Tests for deterministic EnvFacts extraction (port of TokenEconomizer observe tests)."""
from __future__ import annotations

import warnings

import pytest

from ego.domain.envfacts import Observation, classify, extract, normalize_command
from ego.domain.trace_schema import CommandResultEvent, ToolCallEvent, TurnEvent


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("conda run -n ml pytest -q", "pytest -q"),
        ("mamba run -n ml pytest", "pytest"),
        ("conda run -p ./env pytest", "pytest"),
        ("conda run --prefix /opt/envs/ml python -m pytest tests", "pytest tests"),
        ("conda run --no-capture-output --prefix=./env pytest", "pytest"),
        ("micromamba run -n ml pytest", "pytest"),
        ("micromamba run -p ./env python -m pytest", "pytest"),
    ],
)
def test_conda_run_normalization(cmd: str, expected: str) -> None:
    assert normalize_command(cmd) == expected
    assert classify(normalize_command(cmd)) == "test"


def test_conda_install_is_not_classified() -> None:
    assert normalize_command("conda install -n ml pytest") == "conda install -n ml pytest"
    assert classify(normalize_command("conda install -n ml pytest")) is None


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("coverage run -m pytest", "pytest"),
        ("coverage run -m pytest -q tests", "pytest -q tests"),
        ("coverage run --branch --source=pkg -m pytest", "pytest"),
        ("coverage run --rcfile .coveragerc -m unittest discover", "unittest discover"),
        ("python -m coverage run -m pytest", "pytest"),
        ("python3 -m coverage run -p -m pytest", "pytest"),
        (".venv/bin/coverage run -m pytest", "pytest"),
        ("uv run coverage run -m pytest", "pytest"),
    ],
)
def test_coverage_run_normalization(cmd: str, expected: str) -> None:
    assert normalize_command(cmd) == expected
    assert classify(normalize_command(cmd)) == "test"


@pytest.mark.parametrize(
    "cmd",
    [
        "coverage run repro.py",
        "coverage report -m",
        "coverage run manage.py migrate",
        "python -m coverage report",
    ],
)
def test_coverage_non_test_is_ignored(cmd: str) -> None:
    assert classify(normalize_command(cmd)) is None


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("uv run pytest -q", "pytest -q"),
        ("poetry run pytest", "pytest"),
        ("pipenv run pytest -q", "pytest -q"),
        ("uv run --frozen pytest -q", "pytest -q"),
        ("uv run --extra test pytest tests", "pytest tests"),
        ("uv run --group dev --frozen python -m pytest", "pytest"),
        ("uv run --with pytest python -m pytest", "pytest"),
        ("uv run --python 3.12 pytest", "pytest"),
        ("uv run --all-extras -- pytest", "pytest"),
        ("uv run -- pytest", "pytest"),
    ],
)
def test_python_launcher_normalization(cmd: str, expected: str) -> None:
    assert normalize_command(cmd) == expected
    assert classify(normalize_command(cmd)) == "test"


def test_segments_env_assignments_and_npx() -> None:
    assert normalize_command("cd repo && FOO=1 env BAR=2 npx --yes eslint .") == "eslint ."
    assert normalize_command("echo hi;   python3 -m   mypy   src/ ") == "mypy src/"
    assert normalize_command('python -c "import os; print(1)" && pytest -q') == "pytest -q"


@pytest.mark.parametrize(
    "cmd, expected_category",
    [
        ("pytest", "test"),
        ("tox -e py312", "test"),
        ("npm test", "test"),
        ("npm run test", "test"),
        ("node --test", "test"),
        ("go test ./...", "test"),
        ("cargo test", "test"),
        ("make test", "test"),
        ("unittest discover", "test"),
        ("tsc --noEmit", "build"),
        ("npm run build", "build"),
        ("cargo build", "build"),
        ("go build ./...", "build"),
        ("make", "build"),
        ("ruff check .", "lint"),
        ("biome check .", "lint"),
        ("eslint .", "lint"),
        ("mypy src/", "lint"),
        ("flake8", "lint"),
        ("npm run lint", "lint"),
    ],
)
def test_classify_categories(cmd: str, expected_category: str) -> None:
    assert classify(cmd) == expected_category


@pytest.mark.parametrize(
    "cmd",
    [
        "cat README.md",
        "grep -rn foo src/",
        "ls -la",
        "git status",
    ],
)
def test_readonly_commands_ignored(cmd: str) -> None:
    assert classify(normalize_command(cmd)) is None


def test_extract_uses_is_error_only_never_output_text() -> None:
    # Ported spirit of test_observe_label_failed: no DeprecationWarning and never infer from text.
    events = [
        TurnEvent(seq=1, role="assistant"),
        ToolCallEvent(seq=2, tool="Bash", command="pytest -q"),
        CommandResultEvent(
            seq=3,
            command="uv run pytest -q",
            is_error=False,
            output_head="Error: something looked scary; No module named 'tests'",
        ),
        CommandResultEvent(
            seq=4,
            command="npx eslint .",
            is_error=True,
            output_head="All good!",
        ),
        CommandResultEvent(
            seq=5,
            command="cat foo.txt",
            is_error=True,
            output_head="No such file",
        ),
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        obs = extract(events, session="sess-1")

    assert obs == [
        Observation(kind="works", key="pytest -q", category="test", session="sess-1"),
        Observation(kind="fails", key="eslint .", category="lint", session="sess-1"),
    ]
