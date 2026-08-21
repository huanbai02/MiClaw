from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from entry.cli import app
from miclaw.core import skill_loader
from miclaw.core.skill_loader import _match_metadata_field


runner = CliRunner()


def _reset_loader() -> None:
    loader = skill_loader._lazy_loader
    cache_clear = getattr(loader._load_skill_content, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()
    loader._skill_registry = None
    loader._last_scan_time = 0


@pytest.fixture
def isolated_skills_dir(tmp_path, monkeypatch):
    skills_dir = tmp_path / "office" / "skills"
    monkeypatch.setattr(skill_loader, "SKILLS_DIR", str(skills_dir))
    _reset_loader()
    yield skills_dir
    _reset_loader()


def _write_skill(skills_dir: Path, folder: str, content: str) -> Path:
    skill_dir = skills_dir / folder
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


def test_list_skill_metadata_discovers_multiple_skills(isolated_skills_dir):
    _write_skill(
        isolated_skills_dir,
        "research",
        "name: research\ndescription: Research workflow\n",
    )
    _write_skill(
        isolated_skills_dir,
        "github-review",
        "name: github-review\ndescription: Review GitHub changes\n",
    )

    skills = skill_loader.list_skill_metadata()

    assert skills == [
        {"name": "github-review", "description": "Review GitHub changes"},
        {"name": "research", "description": "Research workflow"},
    ]


def test_skills_list_uses_metadata_only_and_does_not_fill_content_cache(isolated_skills_dir, monkeypatch):
    body_marker = "FULL_SKILL_BODY_MUST_NOT_LOAD"
    _write_skill(
        isolated_skills_dir,
        "lazy-skill",
        "name: lazy-skill\ndescription: Metadata only\n"
        + "\n".join(f"metadata padding {index}" for index in range(60))
        + f"\n{body_marker}\n",
    )
    full_content_loader = Mock(side_effect=AssertionError("full Skill content was loaded"))
    monkeypatch.setattr(skill_loader._lazy_loader, "_load_skill_content", full_content_loader)

    result = runner.invoke(app, ["skills", "list"])

    assert result.exit_code == 0
    assert "lazy-skill" in result.output
    assert "Metadata only" in result.output
    assert body_marker not in result.output
    full_content_loader.assert_not_called()


def test_skills_list_renders_name_and_description_without_absolute_path(isolated_skills_dir):
    _write_skill(
        isolated_skills_dir,
        "planning",
        "name: planning\ndescription: File-based [planning] workflow\n",
    )

    result = runner.invoke(app, ["skills", "list"])

    assert result.exit_code == 0
    assert "Available skills: 1" in result.output
    assert "NAME" in result.output
    assert "DESCRIPTION" in result.output
    assert "planning" in result.output
    assert "File-based [planning] workflow" in result.output
    assert str(isolated_skills_dir) not in result.output


def test_skills_list_handles_empty_directory(isolated_skills_dir):
    isolated_skills_dir.mkdir(parents=True)

    result = runner.invoke(app, ["skills", "list"])

    assert result.exit_code == 0
    assert "No skills found." in result.output


def test_skills_list_handles_missing_directory(isolated_skills_dir):
    assert not isolated_skills_dir.exists()

    result = runner.invoke(app, ["skills", "list"])

    assert result.exit_code == 0
    assert "No skills found." in result.output


def test_skills_list_tolerates_incomplete_metadata(isolated_skills_dir):
    _write_skill(isolated_skills_dir, "fallback-name", "description: Description only\n")
    _write_skill(isolated_skills_dir, "fallback-description", "name: fallback-description\n")

    result = runner.invoke(app, ["skills", "list"])

    assert result.exit_code == 0
    assert "fallback-name" in result.output
    assert "Description only" in result.output
    assert "fallback-description" in result.output
    assert "提供 fallback-description 相关功能" in result.output


def test_skills_list_does_not_leak_path_for_invalid_utf8_metadata(isolated_skills_dir):
    skill_dir = isolated_skills_dir / "broken"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_bytes(b"name: broken\n\xffPRIVATE_SKILL_BODY\n")

    result = runner.invoke(app, ["skills", "list"])

    assert result.exit_code == 0
    assert result.exception is None
    assert "No skills found." in result.output
    assert "提取 Skill metadata 失败: broken (UnicodeDecodeError)" in result.output
    assert "Traceback" not in result.output
    assert str(skill_file) not in result.output
    assert str(isolated_skills_dir.parent.parent) not in result.output
    assert "PRIVATE_SKILL_BODY" not in result.output


def test_list_skill_metadata_reuses_cache_and_force_rescan(isolated_skills_dir):
    skill_file = _write_skill(
        isolated_skills_dir,
        "cached",
        "name: cached\ndescription: Before refresh\n",
    )

    first = skill_loader.list_skill_metadata()
    skill_file.write_text("name: cached\ndescription: After refresh\n", encoding="utf-8")
    cached = skill_loader.list_skill_metadata()
    refreshed = skill_loader.list_skill_metadata(force_rescan=True)

    assert first == cached
    assert refreshed == [{"name": "cached", "description": "After refresh"}]


def test_skills_lint_reports_valid_skill(isolated_skills_dir):
    _write_skill(
        isolated_skills_dir,
        "valid",
        "name: valid\ndescription: Valid Skill\n",
    )

    result = runner.invoke(app, ["skills", "lint"])

    assert result.exit_code == 0
    assert "valid                    OK" in result.output
    assert "1 valid, 0 warning, 0 error" in result.output


@pytest.mark.parametrize(
    ("field", "line", "expected"),
    [
        ("name", "name: foo", "foo"),
        ("name", "name:   foo", "foo"),
        ("description", "description: text", "text"),
        ("name", "name : foo", None),
        ("description", "description : text", None),
    ],
)
def test_metadata_matcher_uses_runtime_syntax(field, line, expected):
    match = _match_metadata_field(line, field)

    assert (match.group(1).strip() if match else None) == expected


def test_skills_lint_matches_runtime_fallback_for_invalid_colon_spacing(isolated_skills_dir):
    _write_skill(
        isolated_skills_dir,
        "folder-fallback",
        "name : declared-name\ndescription : Declared description\n",
    )

    metadata = skill_loader.list_skill_metadata(force_rescan=True)
    result = runner.invoke(app, ["skills", "lint"])

    assert metadata == [{
        "name": "folder-fallback",
        "description": "提供 folder-fallback 相关功能",
    }]
    assert result.exit_code == 1
    assert "folder-fallback          ERROR   missing_name, missing_description" in result.output
    assert "0 valid, 0 warning, 1 error" in result.output


def test_skills_lint_and_runtime_accept_valid_metadata_syntax(isolated_skills_dir):
    _write_skill(
        isolated_skills_dir,
        "folder-fallback",
        "name: declared-name\ndescription: Declared description\n",
    )

    metadata = skill_loader.list_skill_metadata(force_rescan=True)
    result = runner.invoke(app, ["skills", "lint"])

    assert metadata == [{"name": "declared-name", "description": "Declared description"}]
    assert result.exit_code == 0
    assert "folder-fallback          OK" in result.output


@pytest.mark.parametrize(
    ("folder", "setup", "issue"),
    [
        ("missing-file", lambda path: path.mkdir(parents=True), "missing_skill_md"),
        (
            "not-regular",
            lambda path: (path / "SKILL.md").mkdir(parents=True),
            "invalid_skill_md",
        ),
    ],
)
def test_skills_lint_reports_structure_errors(isolated_skills_dir, folder, setup, issue):
    skill_dir = isolated_skills_dir / folder
    setup(skill_dir)

    result = runner.invoke(app, ["skills", "lint"])

    assert result.exit_code == 1
    assert folder in result.output
    assert issue in result.output
    assert "0 valid, 0 warning, 1 error" in result.output


def test_skills_lint_reports_invalid_encoding_without_leaking_path(isolated_skills_dir):
    skill_dir = isolated_skills_dir / "broken"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_bytes(b"name: broken\n\xffPRIVATE_SKILL_BODY\n")

    result = runner.invoke(app, ["skills", "lint"])

    assert result.exit_code == 1
    assert "broken" in result.output
    assert "invalid_encoding" in result.output
    assert "Traceback" not in result.output
    assert "UnicodeDecodeError" not in result.output
    assert str(skill_file) not in result.output
    assert str(isolated_skills_dir.parent.parent) not in result.output
    assert "PRIVATE_SKILL_BODY" not in result.output


def test_skills_lint_reports_metadata_severity_and_summary(isolated_skills_dir):
    _write_skill(isolated_skills_dir, "valid", "name: valid\ndescription: Valid\n")
    _write_skill(isolated_skills_dir, "missing-name", "description: Missing name\n")
    _write_skill(isolated_skills_dir, "empty-name", 'name: ""\ndescription: Empty name\n')
    _write_skill(isolated_skills_dir, "missing-description", "name: missing-description\n")
    _write_skill(
        isolated_skills_dir,
        "empty-description",
        "name: empty-description\ndescription: ''\n",
    )

    result = runner.invoke(app, ["skills", "lint"])

    assert result.exit_code == 1
    assert "missing-name             ERROR   missing_name" in result.output
    assert "empty-name               ERROR   empty_name" in result.output
    assert "missing-description      WARNING missing_description" in result.output
    assert "empty-description        WARNING empty_description" in result.output
    assert "1 valid, 2 warning, 2 error" in result.output


def test_skills_lint_warning_only_exits_zero(isolated_skills_dir):
    _write_skill(isolated_skills_dir, "warning", "name: warning\n")

    result = runner.invoke(app, ["skills", "lint"])

    assert result.exit_code == 0
    assert "WARNING missing_description" in result.output
    assert "0 valid, 1 warning, 0 error" in result.output


def test_skills_lint_handles_missing_directory(isolated_skills_dir):
    assert not isolated_skills_dir.exists()

    result = runner.invoke(app, ["skills", "lint"])

    assert result.exit_code == 0
    assert "No skills found." in result.output


def test_skills_lint_does_not_load_full_content_or_pollute_cache(isolated_skills_dir, monkeypatch):
    body_marker = "FULL_SKILL_BODY_MUST_NOT_LOAD"
    _write_skill(
        isolated_skills_dir,
        "lazy-lint",
        "name: lazy-lint\ndescription: Metadata only\n"
        + "\n".join(f"metadata padding {index}" for index in range(60))
        + f"\n{body_marker}\n",
    )
    full_content_loader = Mock(side_effect=AssertionError("full Skill content was loaded"))
    monkeypatch.setattr(skill_loader._lazy_loader, "_load_skill_content", full_content_loader)

    result = runner.invoke(app, ["skills", "lint"])

    assert result.exit_code == 0
    assert body_marker not in result.output
    assert skill_loader._lazy_loader._skill_registry is None
    full_content_loader.assert_not_called()


def test_skills_help_commands_are_available():
    group_help = runner.invoke(app, ["skills", "--help"])
    list_help = runner.invoke(app, ["skills", "list", "--help"])
    lint_help = runner.invoke(app, ["skills", "lint", "--help"])

    assert group_help.exit_code == 0
    assert "list" in group_help.output
    assert "lint" in group_help.output
    assert list_help.exit_code == 0
    assert "列出当前 workspace" in list_help.output
    assert lint_help.exit_code == 0
    assert "静态检查当前 workspace" in lint_help.output
