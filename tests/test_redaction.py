import json

import pytest

from miclaw.core.redaction import (
    COLLECTION_ITEMS_OMITTED,
    CONTENT_OMITTED,
    DEPTH_LIMIT_REACHED,
    LARGE_INTEGER_OMITTED,
    REDACTED,
    REDACTION_FAILED,
    UNSUPPORTED_VALUE,
    is_sensitive_key,
    redact_mapping,
    sanitize_value,
    summarize_content,
    summarize_tool_args,
)


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "API_KEY",
        "Authorization",
        "password",
        "Client_Secret",
        "privateKey",
        "ACCESS-key",
        "cReDeNtIaL",
        "access_token",
        "refresh_token",
        "openai_api_key",
        "aws_access_key",
        "service_private_key",
        "github_token",
        "database_password",
        "oauth_client_secret",
    ],
)
def test_sensitive_key_detection_is_case_insensitive(key):
    assert is_sensitive_key(key) is True


def test_sensitive_keys_are_redacted_without_value_prefix_or_suffix():
    result = redact_mapping(
        {
            "token": "SECRET_TOKEN",
            "API_KEY": "key-value",
            "Authorization": "Bearer hidden",
            "password": "password-value",
        }
    )
    serialized = json.dumps(result)

    assert set(result.values()) == {REDACTED}
    assert "SECRET_TOKEN" not in serialized
    assert "Bearer" not in serialized
    assert "password-value" not in serialized


def test_dangerous_tool_content_fields_use_presence_and_length_only():
    command = 'curl -H "Authorization: Bearer SECRET_TOKEN" https://example.com'
    file_content = "private file content"
    result = summarize_tool_args(
        {
            "command": command,
            "content": file_content,
            "stdout": "secret stdout",
            "stderr": "secret stderr",
        }
    )
    serialized = json.dumps(result)

    assert result["command_present"] is True
    assert result["command_length"] == len(command)
    assert result["content_present"] is True
    assert result["content_length"] == len(file_content)
    assert result["stdout_length"] == len("secret stdout")
    assert result["stderr_length"] == len("secret stderr")
    assert "SECRET_TOKEN" not in serialized
    assert "Authorization" not in serialized
    assert "Bearer" not in serialized
    assert file_content not in serialized


@pytest.mark.parametrize(
    "content",
    [
        "Authorization: Bearer SECRET_TOKEN",
        "token=SECRET_TOKEN",
        '"client_secret": "SECRET_TOKEN"',
        "API_KEY: SECRET_TOKEN",
    ],
)
def test_obvious_secret_like_strings_are_fully_redacted(content):
    result = sanitize_value(content)

    assert result == REDACTED
    assert "SECRET_TOKEN" not in result


@pytest.mark.parametrize(
    "content",
    [
        "Bearer SECRET_TOKEN",
        "bEaReR abc_DEF123",
        "prefix Bearer jwt.token.value suffix",
        "Bearer abcdefghijklmnop",
        "bEaReR AbCdEfGhIjKlMnOp",
        "prefix Bearer abcdefghijklmnop suffix",
        "Bearer abcdefgh",
        "bearer abcdefghij",
        "BEARER abcdefghijk",
        "Bearer abcdefghijkl",
    ],
)
def test_bare_bearer_credentials_are_fully_redacted(content):
    result = sanitize_value(content)

    assert result == REDACTED
    assert "SECRET_TOKEN" not in result
    assert "abc_DEF123" not in result
    assert "jwt.token.value" not in result


def test_nested_bare_bearer_credential_boundaries_are_redacted():
    result = sanitize_value(
        {
            "metadata": [
                "safe",
                "Bearer abcdefgh",
                {"value": "bearer abcdefghij"},
                "BEARER abcdefghijk",
                "Bearer abcdefghijkl",
            ]
        }
    )
    serialized = json.dumps(result)

    assert result["metadata"][1] == REDACTED
    assert result["metadata"][2]["value"] == REDACTED
    assert result["metadata"][3:] == [REDACTED, REDACTED]
    assert not any(token in serialized for token in ("abcdefgh", "abcdefghij", "abcdefghijk", "abcdefghijkl"))


def test_seven_character_alphabetic_bearer_value_defines_minimum_boundary():
    value = "Bearer abcdefg"

    assert sanitize_value(value) == value


def test_prefixed_credential_keys_are_redacted_recursively_and_json_serializable():
    secrets = {
        "openai_api_key": "OPENAI_SECRET_VALUE",
        "aws_access_key": "AWS_SECRET_VALUE",
        "service_private_key": "PRIVATE_SECRET_VALUE",
        "github_token": "GITHUB_SECRET_VALUE",
        "database_password": "DATABASE_SECRET_VALUE",
        "nested": {"oauth_client_secret": "OAUTH_SECRET_VALUE"},
    }

    result = redact_mapping(secrets)
    serialized = json.dumps(result)

    assert result["nested"]["oauth_client_secret"] == REDACTED
    assert all(result[key] == REDACTED for key in secrets if key != "nested")
    assert not any(secret in serialized for secret in secrets.values() if isinstance(secret, str))
    assert "OAUTH_SECRET_VALUE" not in serialized


@pytest.mark.parametrize(
    "content",
    [
        "The bearer carried the document.",
        "A bearer of good news arrived.",
        "The word bearer is ordinary here.",
    ],
)
def test_ordinary_bearer_text_is_not_over_redacted(content):
    assert sanitize_value(content) == content


def test_nested_mappings_and_lists_redact_secrets_recursively():
    result = redact_mapping(
        {
            "status": "ok",
            "metadata": {
                "client_secret": "nested-secret",
                "items": [
                    {"token": "list-token", "retries": 2},
                    "Authorization: Bearer LIST_SECRET",
                ],
            },
        }
    )
    serialized = json.dumps(result)

    assert result["status"] == "ok"
    assert result["metadata"]["client_secret"] == REDACTED
    assert result["metadata"]["items"][0]["token"] == REDACTED
    assert result["metadata"]["items"][0]["retries"] == 2
    assert result["metadata"]["items"][1] == REDACTED
    assert "nested-secret" not in serialized
    assert "list-token" not in serialized
    assert "LIST_SECRET" not in serialized


def test_deep_nesting_hits_safe_depth_limit():
    nested = {"level1": {"level2": {"level3": {"level4": {"token": "too-deep"}}}}}

    result = redact_mapping(nested, max_depth=3)
    serialized = json.dumps(result)

    assert DEPTH_LIMIT_REACHED in serialized
    assert "too-deep" not in serialized


def test_long_strings_are_bounded():
    result = sanitize_value("x" * 500, max_string_length=32)

    assert result == f"{'x' * 32}...[truncated]"
    assert len(result) < 60


def test_huge_integer_is_omitted_and_json_serializable():
    huge_integer = 10**10_000
    result = sanitize_value(huge_integer)

    assert result == LARGE_INTEGER_OMITTED
    assert len(result) < 40
    json.dumps(result)


def test_small_integer_remains_readable():
    assert sanitize_value(42) == 42


def test_large_collections_are_bounded():
    mapping_result = redact_mapping({f"item_{index}": index for index in range(20)}, max_collection_items=3)
    list_result = sanitize_value(list(range(20)), max_collection_items=3)

    assert list(mapping_result) == ["item_0", "item_1", "item_2", "_redaction_omitted_items"]
    assert mapping_result["_redaction_omitted_items"] == 17
    assert list_result == [0, 1, 2, COLLECTION_ITEMS_OMITTED]


def test_callers_cannot_expand_hard_output_limits():
    string_result = sanitize_value("x" * 500, max_string_length=10_000)
    collection_result = sanitize_value(list(range(100)), max_collection_items=10_000)

    assert string_result == f"{'x' * 200}...[truncated]"
    assert len(collection_result) == 21
    assert collection_result[-1] == COLLECTION_ITEMS_OMITTED


def test_content_summary_never_returns_raw_content():
    content = "Authorization: Bearer SECRET_TOKEN"
    result = summarize_content(content)

    assert result.startswith(CONTENT_OMITTED)
    assert f"length={len(content)}" in result
    assert "SECRET_TOKEN" not in result


def test_unusual_object_does_not_use_raw_string_or_repr_fallback():
    class DangerousObject:
        def __str__(self):
            return "SECRET_FROM_STR"

        def __repr__(self):
            return "SECRET_FROM_REPR"

    result = sanitize_value(DangerousObject())

    assert result == UNSUPPORTED_VALUE
    assert "SECRET" not in result


def test_unreadable_or_truncated_keys_do_not_expose_values():
    class DangerousKey:
        def __str__(self):
            return "token"

    result = redact_mapping(
        {
            DangerousKey(): "SECRET_FROM_OBJECT_KEY",
            f"{'x' * 100}_token": "SECRET_AFTER_KEY_LIMIT",
        }
    )
    serialized = json.dumps(result)

    assert set(result.values()) == {REDACTED}
    assert "SECRET_FROM_OBJECT_KEY" not in serialized
    assert "SECRET_AFTER_KEY_LIMIT" not in serialized


def test_mapping_failure_fails_closed_without_returning_raw_data():
    class BrokenMapping(dict):
        def items(self):
            raise RuntimeError("SECRET_FAILURE")

    result = redact_mapping(BrokenMapping({"token": "SECRET_TOKEN"}))
    serialized = json.dumps(result)

    assert result == {"_redaction_error": REDACTION_FAILED}
    assert "SECRET_FAILURE" not in serialized
    assert "SECRET_TOKEN" not in serialized


def test_invalid_limits_fail_closed():
    assert sanitize_value("raw value", max_depth=0) == REDACTION_FAILED
    assert redact_mapping({"safe": "raw value"}, max_collection_items=0) == {
        "_redaction_error": REDACTION_FAILED
    }


def test_low_risk_scalars_remain_readable_and_output_is_json_serializable():
    result = redact_mapping(
        {
            "status": "ok",
            "attempt": 2,
            "enabled": True,
            "ratio": 0.5,
            "optional": None,
            "tags": ("safe", "metadata"),
        }
    )

    assert result == {
        "status": "ok",
        "attempt": 2,
        "enabled": True,
        "ratio": 0.5,
        "optional": None,
        "tags": ["safe", "metadata"],
    }
    json.dumps(result)


def test_observability_metrics_are_not_over_redacted_or_summarized():
    metadata = {
        "token_count": 123,
        "output_tokens": 45,
        "output_token_count": 46,
        "author": "MiClaw",
        "auth_status": "not_configured",
        "result_count": 4,
        "latency_ms": 87,
        "model": "test-model",
        "tool_name": "read_office_file",
        "risk_level": "low",
        "step_id": 3,
    }

    result = redact_mapping(metadata)

    assert result == metadata
    json.dumps(result)
