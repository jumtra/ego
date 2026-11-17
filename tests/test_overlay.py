import json
from pathlib import Path

from ego.domain.config import Config
from ego.domain.templates import MARK_BEGIN
from ego.usecases.overlay import STATE_DIR, begin_overlay, end_overlay

DEAD_PID = 2**31 - 2  # no process can have this pid


def _cfg() -> Config:
    return Config(hosts=["claude", "codex"])


def test_block_goes_on_line_one_and_the_original_comes_back_byte_for_byte(tmp_path: Path):
    original = "# My notes\n\nkeep this\n"
    (tmp_path / "CLAUDE.md").write_text(original, encoding="utf8")
    overlay = begin_overlay(tmp_path, "claude", _cfg())
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf8")
    assert text.startswith(MARK_BEGIN) and text.endswith(original)
    end_overlay(overlay)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf8") == original
    assert not list((tmp_path / STATE_DIR).glob("*.json"))


def test_agents_md_is_used_by_codex_and_removed_at_the_end_when_ego_created_it(tmp_path: Path):
    overlay = begin_overlay(tmp_path, "codex", _cfg())
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf8").startswith(MARK_BEGIN)
    end_overlay(overlay)
    assert not (tmp_path / "AGENTS.md").exists()


def test_text_written_during_the_session_is_kept_and_only_the_block_is_removed(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# notes\n", encoding="utf8")
    overlay = begin_overlay(tmp_path, "claude", _cfg())
    with open(tmp_path / "CLAUDE.md", "a", encoding="utf8") as f:
        f.write("added during session\n")
    end_overlay(overlay)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf8") == "# notes\nadded during session\n"


def test_overlapping_runs_share_one_block_until_the_last_one_ends(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# notes\n", encoding="utf8")
    first = begin_overlay(tmp_path, "claude", _cfg())
    second = begin_overlay(tmp_path, "claude", _cfg())
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf8")
    assert text.count(MARK_BEGIN) == 1
    assert second.state["owner"] is False
    end_overlay(first)
    assert MARK_BEGIN in (tmp_path / "CLAUDE.md").read_text(encoding="utf8")  # second run still needs it
    end_overlay(second)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf8") == "# notes\n"


def test_a_run_that_died_without_restoring_is_cleaned_up_by_the_next_run(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# notes\n", encoding="utf8")
    crashed = begin_overlay(tmp_path, "claude", _cfg())
    data = json.loads(crashed.state_file.read_text(encoding="utf8"))
    data["pid"] = DEAD_PID
    crashed.state_file.write_text(json.dumps(data), encoding="utf8")
    # the crashed run never calls end_overlay; the next run recovers the file before adding its own block
    nxt = begin_overlay(tmp_path, "claude", _cfg())
    assert not crashed.state_file.exists()
    end_overlay(nxt)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf8") == "# notes\n"
