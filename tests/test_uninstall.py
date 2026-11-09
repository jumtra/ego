import json
from pathlib import Path
from ego.install import apply_install, uninstall_project, uninstall_cli
from ego.config import Config


def test_uninstall_project_cleans_owned_files(tmp_path):
    cfg = Config(hosts=["claude"])
    apply_install(tmp_path, cfg)
    
    assert (tmp_path / ".ego").exists()
    assert (tmp_path / ".claude").exists()
    assert (tmp_path / "CLAUDE.md").exists()
    
    res = uninstall_project(tmp_path)
    assert res["status"] == "UNINSTALLED"
    assert not (tmp_path / ".ego").exists()
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_uninstall_cli(tmp_path):
    ego = tmp_path / "ego"
    ego.write_text("#!/bin/sh\n")
    res = uninstall_cli(tmp_path)
    assert res["status"] == "UNINSTALLED_CLI"
    assert not ego.exists()
