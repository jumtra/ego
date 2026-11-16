from pathlib import Path
import pytest
from ego.domain.config import Config
from ego.usecases.install import apply_install

@pytest.fixture(autouse=True)
def _isolated_ego_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("EGO_HOME", str(tmp_path_factory.mktemp("ego-home")))
    monkeypatch.setenv("EGO_ALTER_AUTO_REGISTER", "0")
    monkeypatch.setenv("EGO_ALTER_SESSION_RULES", "0")  # never spawn the user's real alter MCP from a hook test
    monkeypatch.setenv("EGO_ALTER_AUTO_INSTALL_SKILLS", "0")  # never run real `npx --yes github:...` (network, ~7s per apply)
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))  # never resolve the user's real MCP servers (~/.claude.json)
    monkeypatch.delenv("EGO_RUN", raising=False)  # never inherit an outer `ego run` (its hooks stand down)
    monkeypatch.delenv("EGO_RUN_ID", raising=False)
    monkeypatch.delenv("EGO_RUN_SESSION", raising=False)
    monkeypatch.delenv("EGO_RUN_STATE", raising=False)

@pytest.fixture
def project(tmp_path):
    root=tmp_path/"project"
    root.mkdir()
    apply_install(root,Config(clear_suggest_tokens=0,auto_resume=False,usage_switch_percent=0,memory="file"))  # no hooks unless a test asks for them
    return root
