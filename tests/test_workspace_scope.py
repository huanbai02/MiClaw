from pathlib import Path

import miclaw.core.tools.sandbox_tools as sandbox_tools
from miclaw.core.workspace import WorkspaceRoot, WorkspaceScope


def test_default_active_workspace_root_resolves_to_office(tmp_path, monkeypatch):
    office = tmp_path / "workspace" / "office"
    office.mkdir(parents=True)
    monkeypatch.setattr(sandbox_tools, "OFFICE_DIR", str(office))

    root = sandbox_tools._get_active_workspace_root()

    assert root == WorkspaceRoot(path=office, scope=WorkspaceScope.OFFICE)
    assert root.path == office.resolve()
    assert root.scope is WorkspaceScope.OFFICE
    assert root.to_dict() == {"path": str(office.resolve()), "scope": "office"}
    assert sandbox_tools._tool_metadata("test_tool", "read", ".")["workspace_scope"] == "office"


def test_generic_candidate_resolution_uses_supplied_workspace_root(tmp_path):
    office = tmp_path / "office"
    office.mkdir()
    root = WorkspaceRoot(path=office, scope=WorkspaceScope.OFFICE)

    resolved, base = sandbox_tools._resolve_candidate("nested/file.txt", root)

    assert resolved == (office / "nested" / "file.txt").resolve()
    assert base == office.resolve()


def test_root_aware_write_resolution_validates_parent_before_side_effects(tmp_path):
    office = tmp_path / "office"
    office.mkdir()
    root = WorkspaceRoot(path=office, scope=WorkspaceScope.OFFICE)

    resolved = sandbox_tools._resolve_new_workspace_path("nested/file.txt", root)

    assert resolved == (office / "nested" / "file.txt").resolve()
    assert not (office / "nested").exists()


def test_project_and_external_scopes_are_not_activatable_through_public_api(tmp_path, monkeypatch):
    office = tmp_path / "office"
    office.mkdir()
    monkeypatch.setattr(sandbox_tools, "OFFICE_DIR", str(office))

    assert WorkspaceScope.PROJECT.value == "project"
    assert WorkspaceScope.EXTERNAL.value == "external"
    assert sandbox_tools._get_active_workspace_root().scope is WorkspaceScope.OFFICE
    assert not hasattr(sandbox_tools, "set_active_workspace_root")
