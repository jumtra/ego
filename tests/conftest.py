from pathlib import Path
import pytest
from ego.config import Config
from ego.install import apply_install

@pytest.fixture(autouse=True)
def _isolated_ego_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("EGO_HOME", str(tmp_path_factory.mktemp("ego-home")))

@pytest.fixture
def project(tmp_path):
    root=tmp_path/"project"
    root.mkdir()
    apply_install(root,Config())
    return root
