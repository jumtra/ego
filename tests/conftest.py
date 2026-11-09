from pathlib import Path
import pytest
from ego.config import Config
from ego.install import apply_install

@pytest.fixture
def project(tmp_path):
    root=tmp_path/"project"
    root.mkdir()
    apply_install(root,Config())
    return root
