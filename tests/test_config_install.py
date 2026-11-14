from pathlib import Path
import tomllib
import pytest
from ego.config import Config,load_config,save_config,OptConfig,load_opt,save_opt
from ego.install import apply_install,installation_plan,rollback,legacy_plan,remove_legacy
from ego.util import EgoError,filehash
from ego.author import lint

def test_initial_setup(project):
    assert load_config(project).profile=="balanced"
    assert load_config(project).timeout_seconds==3600
    assert (project/".claude/skills/work/SKILL.md").exists()
    assert (project/".agents/skills/work/SKILL.md").exists()
    assert len(list((project/".claude/agents").glob("*.md")))==4
    assert lint(project)["status"]=="PASS"
    assert all(x["action"]=="unchanged" for x in installation_plan(project,load_config(project))["changes"])

def test_non_destructive_managed_block(tmp_path):
    root=tmp_path
    (root/"CLAUDE.md").write_text("# My notes\n",encoding="utf8")
    result=apply_install(root,Config())
    assert (root/"CLAUDE.md").read_text().startswith("# My notes")
    rollback(root,result["snapshot_id"])
    assert (root/"CLAUDE.md").read_text()=="# My notes\n"
    assert not (root/".agents/skills/work/SKILL.md").exists()

def test_owned_edit_conflict(project):
    path=project/".claude"/"skills"/"work"/"SKILL.md"
    path.write_text(path.read_text()+"\nCUSTOM",encoding="utf8")
    with pytest.raises(EgoError) as exc:
        apply_install(project,load_config(project))
    assert exc.value.code == "OWNERSHIP_CONFLICT"

def test_config_policy_mode(project):
    cfg = Config(policy_mode="relaxed")
    save_config(project, cfg)
    loaded = load_config(project)
    assert loaded.policy_mode == "relaxed"
    content = (project / ".ego/config.toml").read_text()
    assert 'policy_mode = "relaxed"' in content
    with pytest.raises(EgoError):
        save_config(project, Config(policy_mode="invalid"))

def test_config_models_support(project):
    cfg = Config(models={"claude": "sonnet-3.5", "codex": "gpt-5-codex", "opencode": "auto"})
    save_config(project, cfg)
    loaded = load_config(project)
    assert loaded.models == {"claude": "sonnet-3.5", "codex": "gpt-5-codex", "opencode": "auto"}
    content = (project / ".ego/config.toml").read_text()
    assert 'models = {claude = "sonnet-3.5", codex = "gpt-5-codex", opencode = "auto"}' in content
    assert "# モデル設定の例 (models):" in content

    # Validation failure on invalid host or model type
    with pytest.raises(EgoError):
        save_config(project, Config(models={"invalid_host": "sonnet"}))
    with pytest.raises(EgoError):
        save_config(project, Config(models={"claude": ""}))

def test_config_validate(project):
    bad=Config(max_parallel=99)
    with pytest.raises(EgoError):save_config(project,bad)
    path=project/".ego/config.toml"
    path.write_text("bogus = true\n")
    with pytest.raises(EgoError):load_config(project)

def test_opt_default_and_save(project):
    assert load_opt(project).mode=="off"
    save_opt(project,OptConfig(mode="shadow"))
    assert load_opt(project).mode=="shadow"
    with pytest.raises(EgoError):OptConfig(require_human_approval=False).validate()

def test_migration_requires_hash(project):
    path=project/".claude/skills/brainstorm/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("legacy")
    plan=legacy_plan(project)
    assert len(plan)==1
    with pytest.raises(EgoError):remove_legacy(project,{plan[0]["path"]:"wrong"})
    applied=remove_legacy(project,{plan[0]["path"]:plan[0]["sha256"]})
    assert path.exists() is False
    assert Path(applied["backup"]).exists()


def test_recommended_defaults_and_sync_fallback(tmp_path):
    from ego.cli import main
    root = tmp_path / "fresh"
    root.mkdir()
    apply_install(root, Config())
    loaded = load_config(root)
    assert loaded.usage_switch_percent == 85
    assert loaded.run_yolo is True
    assert set(loaded.models) == {"claude", "codex", "opencode", "agy"}
    for role in ("coder", "tester", "judge"):
        assert set(loaded.roles[role]) == {"claude", "codex", "agy", "opencode"}

    # Existing config.toml without those keys inherits the recommended defaults on sync
    partial = tmp_path / "partial"
    partial.mkdir()
    apply_install(partial, Config())
    (partial / ".ego" / "config.toml").write_text('schema_version = 1\nprofile = "balanced"\n', encoding="utf-8")
    assert main(["sync", "--root", str(partial), "--apply"]) == 0
    synced = load_config(partial)
    assert synced.usage_switch_percent == 85
    assert synced.run_yolo is True
    assert "agy" in synced.models
    assert "agy" in synced.roles["coder"]
