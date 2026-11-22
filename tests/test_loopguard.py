"""Tests for pure-domain loopguard and policy.stagnation_action."""
from __future__ import annotations

from dataclasses import replace

import pytest

from ego.domain.loopguard import (
    LoopState,
    is_shell_edit,
    is_stuck,
    limit,
    nudge_text,
    over_envelope,
    record,
    repeat_ratio,
    stagnation_summary,
    update,
)
from ego.domain.policy import stagnation_action
from ego.domain.settings import load_settings
from ego.domain.trace_schema import CommandResultEvent, ToolCallEvent, TurnEvent


def _default_settings():
    return load_settings(None).loopguard


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("sed -i 's/foo/bar/' src/app.py", True),
        ("perl -pi -e 's/foo/bar/' src/app.py", True),
        ("patch -p1 < fix.diff", True),
        ("git apply fix.diff", True),
        ("git checkout -- src/app.py", True),
        ("git restore src/app.py", True),
        ("echo 'x = 1' > src/app.py", True),
        ("echo 'x = 2' >> src/app.py", True),
        ("echo 'x = 1' | tee src/app.py", True),
        ("cat <<'EOF' > src/app.py\nprint(1)\nEOF", True),
        ("python3 -c \"open('src/app.py', 'w').write('x')\"", True),
        ("node -e \"require('fs').writeFileSync('src/app.py', 'x')\"", True),
        ("cat src/app.py", False),
        ("pytest -q tests/test_app.py", False),
        ("sed -n '1,20p' src/app.py", False),
        ("git status", False),
    ],
)
def test_is_shell_edit(cmd: str, expected: bool):
    assert is_shell_edit(cmd) is expected


def test_sed_i_counts_as_edit_in_loop_state():
    state = LoopState()
    events = [
        TurnEvent(seq=1, role="assistant"),
        ToolCallEvent(seq=2, tool="Bash", command="sed -i 's/a/b/' src/mod.py"),
    ]
    nxt = update(state, events)
    assert nxt.turns == 1
    assert nxt.edits_by_file == {"mod.py": 1}
    assert nxt.last_progress_turn == 1


def test_long_productive_turn_with_changing_failures_observes():
    s = _default_settings()
    events = []
    seq = 1
    for i in range(25):
        events.append(TurnEvent(seq=seq, role="assistant"))
        seq += 1
        events.append(ToolCallEvent(seq=seq, tool="Edit", file_path=f"/repo/src/f{i % 2}.py"))
        seq += 1
        events.append(ToolCallEvent(seq=seq, tool="Bash", command=f"pytest -q tests/test_{i}.py"))
        seq += 1
        events.append(
            CommandResultEvent(
                seq=seq,
                command=f"pytest -q tests/test_{i}.py",
                is_error=True,
                output_head=f"ErrorKind_{chr(65 + i)}: unique failure in module_{chr(97 + i)}",
            )
        )
        seq += 1
    state = update(LoopState(), events)
    assert state.turns == 25
    assert repeat_ratio(state.failure_sigs) == 0.0
    assert is_stuck(state, s) is False
    assert stagnation_action(state, None, s, nudges_used=0) == "observe"


def test_six_identical_failures_and_four_edits_same_file_nudge_then_escalate():
    s = _default_settings()
    events = []
    seq = 1
    for i in range(25):
        events.append(TurnEvent(seq=seq, role="assistant"))
        seq += 1
        if i < 4:
            events.append(ToolCallEvent(seq=seq, tool="Edit", file_path="/repo/src/target.py"))
            seq += 1
        if i < 6:
            events.append(ToolCallEvent(seq=seq, tool="Bash", command="pytest -q tests/test_target.py"))
            seq += 1
            events.append(
                CommandResultEvent(
                    seq=seq,
                    command="pytest -q tests/test_target.py",
                    is_error=True,
                    output_head=f"AssertionError at /repo/src/target.py:{10 + i}: expected 1",
                )
            )
            seq += 1
    state = update(LoopState(), events)
    assert state.turns == 25
    assert state.edits_by_file["target.py"] == 4
    assert state.error_outputs == 6
    assert repeat_ratio(state.failure_sigs) >= s.stagnation_ratio
    assert is_stuck(state, s) is True
    assert over_envelope(state, None, s) is True

    assert stagnation_action(state, None, s, nudges_used=0) == "nudge"
    assert stagnation_action(state, None, s, nudges_used=1) == "escalate"

    msg = nudge_text(state)
    assert "stop" not in msg.lower()
    assert "AssertionError" in msg
    assert "pytest -q tests/test_target.py" in msg

    summary = stagnation_summary(state)
    assert summary["count"] == 6
    assert "AssertionError" in summary["pattern"]
    assert summary["commands"] == ["pytest -q tests/test_target.py"]


def test_envelope_limit_changes_after_five_samples(tmp_path):
    s = _default_settings()
    for turns in (10, 12, 14, 16):
        record(tmp_path, turns)
        assert limit(tmp_path, s) is None
    record(tmp_path, 18)
    lim = limit(tmp_path, s)
    assert lim is not None
    # p95 of [10, 12, 14, 16, 18]: index 0.95 * 4 = 3.8 -> 16 + 0.8 * 2 = 17.6
    assert lim == pytest.approx(17.6)


def test_passing_test_clears_failure_sigs_and_updates_progress():
    events = [
        TurnEvent(seq=1, role="assistant"),
        CommandResultEvent(seq=2, command="pytest -q", is_error=True, output_head="ValueError: bad"),
        TurnEvent(seq=3, role="assistant"),
        CommandResultEvent(seq=4, command="pytest -q", is_error=False, output_head="1 passed"),
    ]
    state = update(LoopState(), events)
    assert state.failure_sigs == []
    assert state.last_progress_turn == 2


def test_stagnation_action_respects_disabled_and_backstop():
    s = _default_settings()
    stuck_state = LoopState(
        turns=50,
        edits_by_file={"a.py": 4},
        error_outputs=6,
        failure_sigs=["AssertionError: N"] * 6,
    )
    assert stagnation_action(stuck_state, 15.0, s, nudges_used=0) == "escalate"
    disabled = replace(s, enabled=False)
    assert stagnation_action(stuck_state, 15.0, disabled, nudges_used=0) == "observe"
