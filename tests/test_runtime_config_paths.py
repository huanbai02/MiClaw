"""锁定 runtime/config.py relocation 后的默认路径与 env override 语义。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


_CONFIG_SNAPSHOT_SCRIPT = """
import json
import sys
import types

dotenv = types.ModuleType('dotenv')
dotenv.load_dotenv = lambda: None
sys.modules['dotenv'] = dotenv

from miclaw.core.runtime import config

print(json.dumps({
    'project_root': config.PROJECT_ROOT,
    'workspace_dir': config.WORKSPACE_DIR,
    'db_path': config.DB_PATH,
    'memory_dir': config.MEMORY_DIR,
    'personas_dir': config.PERSONAS_DIR,
    'scripts_dir': config.SCRIPTS_DIR,
    'office_dir': config.OFFICE_DIR,
    'skills_dir': config.SKILLS_DIR,
    'tasks_file': config.TASKS_FILE,
    'log_file': str(config.get_log_file_path()),
}))
"""


def _isolated_config_snapshot(workspace: Path | None) -> dict[str, str]:
    """在独立解释器中禁用 dotenv 并导入 config，避免当前进程的 import-time state。"""
    environment = os.environ.copy()
    if workspace is None:
        environment.pop("MICLAW_WORKSPACE", None)
    else:
        environment["MICLAW_WORKSPACE"] = str(workspace)
    result = subprocess.run(
        [sys.executable, "-c", _CONFIG_SNAPSHOT_SCRIPT],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_default_runtime_config_paths_remain_root_relative_after_relocation():
    """无 env override 时，所有默认路径仍以 repository root 而非 source package 为基准。"""
    repository_root = Path(__file__).resolve().parents[1]
    workspace = repository_root / "workspace"

    snapshot = _isolated_config_snapshot(None)

    assert snapshot == {
        "project_root": str(repository_root),
        "workspace_dir": str(workspace),
        "db_path": str(workspace / "state.sqlite3"),
        "memory_dir": str(workspace / "memory"),
        "personas_dir": str(workspace / "personas"),
        "scripts_dir": str(workspace / "scripts"),
        "office_dir": str(workspace / "office"),
        "skills_dir": str(workspace / "office" / "skills"),
        "tasks_file": str(workspace / "tasks.json"),
        "log_file": str(workspace / "logs" / "miclaw.jsonl"),
    }
    assert Path(snapshot["workspace_dir"]) != repository_root / "miclaw" / "workspace"


def test_workspace_env_override_keeps_precedence_for_all_derived_paths(tmp_path):
    """MICLAW_WORKSPACE 仍优先于默认 root，并驱动全部派生路径。"""
    workspace = tmp_path / "override-workspace"

    snapshot = _isolated_config_snapshot(workspace)

    assert snapshot["workspace_dir"] == str(workspace)
    assert snapshot["db_path"] == str(workspace / "state.sqlite3")
    assert snapshot["memory_dir"] == str(workspace / "memory")
    assert snapshot["personas_dir"] == str(workspace / "personas")
    assert snapshot["scripts_dir"] == str(workspace / "scripts")
    assert snapshot["office_dir"] == str(workspace / "office")
    assert snapshot["skills_dir"] == str(workspace / "office" / "skills")
    assert snapshot["tasks_file"] == str(workspace / "tasks.json")
    assert snapshot["log_file"] == str(workspace / "logs" / "miclaw.jsonl")
