from pathlib import Path
import pytest
from seh.config import Config
from seh.install import apply_install

@pytest.fixture
def project(tmp_path):
    root=tmp_path/"project"
    root.mkdir()
    apply_install(root,Config())
    return root
