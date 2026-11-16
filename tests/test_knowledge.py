"""Knowledge: where project knowledge lives per mode (file index + topic files, or alter) and how it is read and written."""
from __future__ import annotations
import io
import json
from pathlib import Path
import sys

import pytest

from ego.adapters.knowledge import KnowledgeStore, record_checkpoint_decisions
from ego.cli import main
from ego.domain.config import Config, load_config, save_config
from ego.domain.templates import generated_files, instruction_block
from ego.usecases.install import apply_install, installation_plan


# -- the store: index first, merge on write ---------------------------------------------------------

def test_first_fact_creates_a_topic_file_and_an_index_line(tmp_path: Path):
    result = KnowledgeStore(tmp_path).add("Retention keeps the 30 newest runs", keywords=["retention"])
    assert result["action"] == "created" and result["path"] == ".ego/knowledge/topics/retention.md"
    index = (tmp_path / ".ego" / "knowledge" / "index.md").read_text()
    assert "- [retention](topics/retention.md) — Retention keeps the 30 newest runs | keywords: retention" in index
    assert "- Retention keeps the 30 newest runs" in (tmp_path / result["path"]).read_text()


def test_a_related_fact_is_appended_to_the_existing_topic_not_a_new_file(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    store.add("Retention keeps the 30 newest runs", keywords=["retention", "state"])
    result = store.add("The event journal keeps its last 5000 lines", keywords=["retention"])
    assert result["action"] == "appended" and result["topic"] == "retention"
    assert sorted(p.name for p in (tmp_path / ".ego" / "knowledge" / "topics").iterdir()) == ["retention.md"]
    assert len(store.entries()) == 1  # still one index line; keywords merged
    text = store.get("retention")["text"]
    assert "30 newest runs" in text and "5000 lines" in text


def test_matching_without_keywords_uses_the_words_of_the_fact(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    store.add("Sync migrates memory file to alter when the alter MCP server is configured", keywords=["sync", "alter"])
    assert store.add("Sync keeps file knowledge when the alter MCP server is missing")["topic"] == "sync"


def test_an_unrelated_fact_creates_its_own_topic(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    store.add("Retention keeps the 30 newest runs", keywords=["retention"])
    result = store.add("A staging worktree is removed after a verified run", keywords=["worktree"])
    assert result["action"] == "created" and result["topic"] == "worktree"
    assert {e["slug"] for e in store.entries()} == {"retention", "worktree"}


def test_duplicates_are_skipped_and_a_richer_line_replaces_the_shorter_one(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    store.add("Runs are pruned", topic="retention")
    again = store.add("runs   are PRUNED", topic="retention")
    assert again["action"] == "duplicate" and again["skipped"] == 1
    richer = store.add("Runs are pruned to the 30 newest on every start", topic="retention")
    assert richer["action"] == "appended" and richer["merged"] == 1 and richer["added"] == 0
    lines = [ln for ln in store.get("retention")["text"].splitlines() if ln.startswith("- ")]
    assert lines == ["- Runs are pruned to the 30 newest on every start"]


def test_explicit_topic_wins_and_japanese_text_is_matched(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    store.add("オーナーとはこのプロジェクトの意思決定者を指す", topic="glossary", keywords=["用語"])
    result = store.add("ゴールとはオーナーの意図を指す", keywords=["用語"])
    assert result["topic"] == "glossary" and result["action"] == "appended"
    assert [h["slug"] for h in store.search("オーナー 意図")] == ["glossary"]


def test_search_get_and_index_text(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    assert store.index_text().startswith("# Knowledge index")  # readable before anything is written
    store.add("Hooks never call a model", keywords=["hooks"])
    store.add("Staging worktrees are removed", keywords=["worktree"])
    assert [h["slug"] for h in store.search("hooks model")] == ["hooks"]
    assert store.get("worktree")["text"].startswith("# Staging worktrees are removed")
    with pytest.raises(Exception, match="No topic"):
        store.get("missing")


def test_topic_names_cannot_escape_the_knowledge_directory(tmp_path: Path):
    result = KnowledgeStore(tmp_path).add("x marks the spot", topic="../../outside")
    assert result["path"].startswith(".ego/knowledge/topics/") and "outside" in result["topic"]
    assert not (tmp_path / "outside.md").exists()


def test_the_legacy_domain_file_is_still_listed_and_readable(tmp_path: Path):
    legacy = tmp_path / ".ego" / "knowledge" / "domain.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# Project knowledge\n- Terms live here\n")
    store = KnowledgeStore(tmp_path)
    assert [e["slug"] for e in store.entries()] == ["domain"]
    assert "Terms live here" in store.get("domain")["text"]


# -- CLI: file mode works, alter mode points at alter ------------------------------------------------

def test_cli_add_search_get_index_in_file_mode(project: Path, capsys):
    assert main(["knowledge", "add", "Hooks never call a model", "--keyword", "hooks", "--root", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "created"
    assert main(["knowledge", "search", "hooks", "--root", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["slug"] == "hooks"
    assert main(["knowledge", "get", "hooks", "--root", str(project)]) == 0
    assert "Hooks never call a model" in json.loads(capsys.readouterr().out)[0]["text"]
    assert main(["knowledge", "index", "--root", str(project)]) == 0
    assert "[hooks](topics/hooks.md)" in capsys.readouterr().out


def test_cli_knowledge_is_an_error_in_alter_mode(tmp_path: Path, capsys):
    save_config(tmp_path, Config(knowledge="alter"))
    assert main(["knowledge", "add", "x fact here", "--root", str(tmp_path)]) == 2
    assert "KNOWLEDGE_IS_ALTER" in capsys.readouterr().err
    assert not (tmp_path / ".ego" / "knowledge").exists()


# -- automatic recording -----------------------------------------------------------------------------

def _checkpoint(root: Path, *decisions: str) -> int:
    argv = ["session", "checkpoint", "--root", str(root), "--task-id", "t1", "--goal", "Tune retention limits", "--completed", "done"]
    for d in decisions:
        argv += ["--decision", d]
    return main(argv)


def test_file_mode_checkpoint_records_decisions_as_knowledge(project: Path, capsys):
    assert _checkpoint(project, "Keep 30 runs, not 10", "Prune before each run") == 0
    store = KnowledgeStore(project)
    assert len(store.entries()) >= 1
    bullets = lambda: [ln for e in store.entries() for ln in store.get(e["slug"])["text"].splitlines() if ln.startswith("- ")]
    assert "- Keep 30 runs, not 10" in bullets() and "- Prune before each run" in bullets()
    before = bullets()
    assert _checkpoint(project, "Keep 30 runs, not 10") == 0  # the same decision again adds nothing
    assert bullets() == before


def test_auto_record_can_be_switched_off_and_skips_checkpoints_without_decisions(project: Path, monkeypatch):
    monkeypatch.setenv("EGO_KNOWLEDGE_AUTO_RECORD", "0")
    assert _checkpoint(project, "A decision worth keeping") == 0
    assert KnowledgeStore(project).entries() == []
    monkeypatch.delenv("EGO_KNOWLEDGE_AUTO_RECORD")
    assert record_checkpoint_decisions(project, {"goal": "x", "decisions": []})["status"] == "skipped"


def test_alter_mode_checkpoint_does_not_write_the_file_store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EGO_ALTER_AUTO_REVIEW", "0")
    save_config(tmp_path, Config(knowledge="alter"))
    assert _checkpoint(tmp_path, "A decision for alter") == 0
    assert not (tmp_path / ".ego" / "knowledge").exists()


# -- config and generated instructions ---------------------------------------------------------------

def test_legacy_memory_key_is_read_as_knowledge(tmp_path: Path):
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego" / "config.toml").write_text('schema_version = 1\nmemory = "file"\n')
    assert load_config(tmp_path).knowledge == "file"
    (tmp_path / ".ego" / "config.toml").write_text('schema_version = 1\nmemory = "alter-mcp"\nknowledge = "file"\n')
    assert load_config(tmp_path).knowledge == "file"  # the new key wins
    save_config(tmp_path, Config(knowledge="file"))
    saved = (tmp_path / ".ego" / "config.toml").read_text()
    assert 'knowledge = "file"' in saved and "\nmemory =" not in saved


def test_instruction_block_names_the_source_of_knowledge_and_rules():
    alter = instruction_block(Config(knowledge="alter"))
    assert "alter project AI usage rules" in alter and "`alter_bootstrap`" in alter and "index.md" not in alter
    file = instruction_block(Config(knowledge="file"))
    assert ".ego/knowledge/index.md" in file and "ego knowledge add" in file and "alter_bootstrap" not in file


def test_no_domain_file_is_generated_any_more():
    assert ".ego/knowledge/domain.md" not in generated_files(Config(knowledge="file"))
    assert not any(path.startswith(".ego/knowledge") for path in generated_files(Config(knowledge="alter")))


def test_an_edited_legacy_domain_file_does_not_block_a_sync(tmp_path: Path):
    root = tmp_path / "p"
    root.mkdir()
    cfg = Config(knowledge="file", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0)
    apply_install(root, cfg)
    legacy = root / ".ego" / "knowledge" / "domain.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("# Project knowledge\n- my own terms\n")
    ownership = root / ".ego" / "ownership.json"
    data = json.loads(ownership.read_text())
    data["files"][".ego/knowledge/domain.md"] = "0" * 64  # an older install owned the stub with a different digest
    ownership.write_text(json.dumps(data))
    plan = installation_plan(root, cfg)
    assert plan["conflicts"] == []
    apply_install(root, cfg)
    assert "my own terms" in legacy.read_text()


# -- alter unreachable is an error, not silence -------------------------------------------------------

def _hook(monkeypatch, capsys, root: Path, event: str, payload: dict) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert main(["hook", event, "--root", str(root)]) == 0
    return json.loads(capsys.readouterr().out)


def test_user_prompt_hook_reports_an_unreachable_alter(tmp_path: Path, monkeypatch, capsys):
    from ego.adapters import memory
    save_config(tmp_path, Config(knowledge="alter"))
    monkeypatch.delenv("EGO_ALTER_PROMPT_CONTEXT", raising=False)
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: None)
    out = _hook(monkeypatch, capsys, tmp_path, "user-prompt", {"prompt": "過去の決定を探したい", "cwd": str(tmp_path)})
    assert "ego ERROR: alter に接続できません" in out["hookSpecificOutput"]["additionalContext"]
    assert "ego ERROR:" in out["systemMessage"]


def test_user_prompt_hook_is_silent_in_file_mode(project: Path, monkeypatch, capsys):
    out = _hook(monkeypatch, capsys, project, "user-prompt", {"prompt": "過去の決定を探したい", "cwd": str(project)})
    assert out == {}
