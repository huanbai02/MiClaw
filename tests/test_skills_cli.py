from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from entry.cli import app
from miclaw.core import skill_loader


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


def test_skills_help_commands_are_available():
    group_help = runner.invoke(app, ["skills", "--help"])
    list_help = runner.invoke(app, ["skills", "list", "--help"])

    assert group_help.exit_code == 0
    assert "list" in group_help.output
    assert list_help.exit_code == 0
    assert "列出当前 workspace" in list_help.output
