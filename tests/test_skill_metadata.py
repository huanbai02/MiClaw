import json

import pytest

from miclaw.core import skill_loader
from miclaw.core.skill_loader import (
    SkillMetadataSeverity,
    validate_skill_metadata,
)


def _write_skill(tmp_path, content: str):
    skill_dir = tmp_path / "example"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


def _issue_pairs(result):
    return [(issue.code, issue.severity) for issue in result.issues]


def test_valid_metadata_returns_structured_result(tmp_path):
    skill_file = _write_skill(
        tmp_path,
        "name: example\ndescription: Example skill\n",
    )

    result = validate_skill_metadata("example", str(skill_file))

    assert result.valid is True
    assert result.status == "OK"
    assert result.issues == ()
    assert result.metadata.to_dict() == {
        "name": "example",
        "description": "Example skill",
    }
    json.dumps(result.to_dict(), ensure_ascii=False)


def test_spaced_colon_matches_runtime_parser_semantics(tmp_path):
    skill_file = _write_skill(
        tmp_path,
        "name : invalid\ndescription : invalid\n",
    )

    result = validate_skill_metadata("example", str(skill_file))

    assert result.valid is False
    assert result.metadata.name is None
    assert result.metadata.description is None
    assert _issue_pairs(result) == [
        ("missing_name", SkillMetadataSeverity.ERROR),
        ("missing_description", SkillMetadataSeverity.WARNING),
    ]


@pytest.mark.parametrize(
    ("content", "code", "severity", "field"),
    [
        ("description: text\n", "missing_name", "ERROR", "name"),
        ('name: ""\ndescription: text\n', "empty_name", "ERROR", "name"),
        ("name: example\n", "missing_description", "WARNING", "description"),
        ("name: example\ndescription:\n", "empty_description", "WARNING", "description"),
    ],
)
def test_metadata_field_issues_are_stable(tmp_path, content, code, severity, field):
    skill_file = _write_skill(tmp_path, content)

    result = validate_skill_metadata("example", str(skill_file))
    issue = result.issues[0]

    assert issue.code == code
    assert issue.severity.value == severity
    assert issue.field == field
    assert result.valid is (severity == "WARNING")


def test_structural_issue_codes_are_stable(tmp_path):
    missing = validate_skill_metadata("missing", str(tmp_path / "missing" / "SKILL.md"))
    not_regular_path = tmp_path / "not-regular" / "SKILL.md"
    not_regular_path.mkdir(parents=True)
    not_regular = validate_skill_metadata("not-regular", str(not_regular_path))
    invalid_path = tmp_path / "invalid" / "SKILL.md"
    invalid_path.parent.mkdir()
    invalid_path.write_bytes(b"name: invalid\n\xffPRIVATE_BODY\n")
    invalid = validate_skill_metadata("invalid", str(invalid_path))

    assert [result.issues[0].code for result in (missing, not_regular, invalid)] == [
        "missing_skill_md",
        "invalid_skill_md",
        "invalid_encoding",
    ]
    assert all(result.issues[0].severity.value == "ERROR" for result in (missing, not_regular, invalid))
    assert not_regular.issues[0].message == "SKILL.md 不是 regular file"


def test_unreadable_metadata_uses_safe_issue(tmp_path, monkeypatch):
    skill_file = _write_skill(tmp_path, "name: example\ndescription: Example\n")

    def raise_permission_error(_path):
        raise PermissionError("/home/user/private/SKILL.md")

    monkeypatch.setattr(skill_loader, "_read_metadata_prefix", raise_permission_error)

    result = validate_skill_metadata("example", str(skill_file))
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.issues[0].code == "unreadable_metadata"
    assert "/home/user" not in serialized
    assert "PermissionError" not in serialized


def test_validation_result_is_safe_and_deterministic(tmp_path):
    skill_file = _write_skill(
        tmp_path,
        "description : PRIVATE_SKILL_BODY\n",
    )

    first = validate_skill_metadata(str(tmp_path / "safe-folder"), str(skill_file))
    second = validate_skill_metadata(str(tmp_path / "safe-folder"), str(skill_file))
    serialized = json.dumps(first.to_dict(), ensure_ascii=False)

    assert first == second
    assert [issue.code for issue in first.issues] == ["missing_name", "missing_description"]
    assert str(tmp_path) not in serialized
    assert "PRIVATE_SKILL_BODY" not in serialized
    assert "safe-folder" in serialized
