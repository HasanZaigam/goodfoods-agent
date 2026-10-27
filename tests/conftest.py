import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> Path:
    from goodfoods.data.seed import seed

    path = tmp_path_factory.mktemp("db") / "test.db"
    seed(path, reset=True)
    return path


@pytest.fixture(scope="session")
def toolbox(db_path):
    from goodfoods.agent.mcp_bridge import MCPToolbox

    env = {**os.environ, "GOODFOODS_DB": str(db_path), "PYTHONPATH": str(ROOT)}
    tb = MCPToolbox(env=env)
    yield tb
    tb.close()
