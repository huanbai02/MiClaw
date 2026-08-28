"""锁定 ExecutionStore 路径始终由现有 workspace config 派生。"""

import json
import os
from pathlib import Path
import subprocess
import sys


def _config_in_subprocess(env: dict[str, str]) -> dict[str, str]:
    """以独立 interpreter 避免 import-time config state 污染测试。"""
    code = (
        "import json; from miclaw.core.runtime import config; "
        "print(json.dumps({'workspace': config.WORKSPACE_DIR, 'execution': config.EXECUTION_DB_PATH}))"
    )
    output = subprocess.check_output([sys.executable, "-c", code], text=True, env=env)
    return json.loads(output.splitlines()[-1])


def test_execution_db_default_and_workspace_override_paths_are_stable(tmp_path):
    """默认与 MICLAW_WORKSPACE override 都只在对应 workspace 创建 execution.sqlite3。"""
    env = os.environ.copy()
    env.pop("MICLAW_WORKSPACE", None)
    result = _config_in_subprocess(env)
    repository_root = Path(__file__).resolve().parents[1]
    assert Path(result["workspace"]) == repository_root / "workspace"
    assert Path(result["execution"]) == repository_root / "workspace" / "execution.sqlite3"

    override = tmp_path / "custom-workspace"
    env["MICLAW_WORKSPACE"] = str(override)
    overridden = _config_in_subprocess(env)
    assert Path(overridden["workspace"]) == override
    assert Path(overridden["execution"]) == override / "execution.sqlite3"
