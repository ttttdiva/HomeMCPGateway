import json

import pytest

from home_mcp_gateway.job_environment import (
    build_job_environment,
    is_secret_like_environment_name,
)


def test_minimal_allowlist_keeps_required_platform_and_tool_environment():
    child, metadata = build_job_environment(
        {
            "Path": "parent-path",
            "ProgramFiles(x86)": "program-files-x86",
            "CommonProgramFilesW6432": "common-program-files",
            "HOME_MCP_AOITALK_URL": "http://127.0.0.1:3000",
            "NODE_EXTRA_CA_CERTS": "certificate-path",
            "NODE_OPTIONS": "--no-warnings",
            "CODEX_HOME": "codex-home",
            "MSYS2_PATH_TYPE": "inherit",
            "PYTHONPATH": "python-path",
            "HTTPS_PROXY": "proxy-value",
            "UNLISTED_PARENT_VALUE": "not-inherited",
        }
    )

    assert child == {
        "Path": "parent-path",
        "ProgramFiles(x86)": "program-files-x86",
        "CommonProgramFilesW6432": "common-program-files",
        "HOME_MCP_AOITALK_URL": "http://127.0.0.1:3000",
        "NODE_EXTRA_CA_CERTS": "certificate-path",
        "NODE_OPTIONS": "--no-warnings",
        "CODEX_HOME": "codex-home",
        "MSYS2_PATH_TYPE": "inherit",
        "PYTHONPATH": "python-path",
        "HTTPS_PROXY": "proxy-value",
    }
    assert metadata["env_policy"] == "allowlist-v1"
    assert metadata["env_inherited_count"] == len(child)
    assert metadata["env_dropped_count"] == 1
    assert metadata["env_dropped_secret_like_names"] == []


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
        "GROK_API_KEY",
        "MISTRAL_API_KEY",
        "COHERE_API_KEY",
        "SERVICE_TOKEN",
        "SERVICE_SECRET",
        "DB_PASSWORD_FILE",
        "CUSTOM_CREDENTIAL_PATH",
        "GIT_ASKPASS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_STORAGE_KEY",
        "AZURE_CLIENT_SECRET",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "HF_TOKEN",
        "NPM_CONFIG__AUTH",
    ],
)
def test_secret_like_names_are_rejected(name):
    child, metadata = build_job_environment(
        {name: "unique-secret-sentinel", "PATH": "allowed-path"}
    )

    assert name not in child
    assert child["PATH"] == "allowed-path"
    assert name in metadata["env_dropped_secret_like_names"]
    assert "unique-secret-sentinel" not in json.dumps(metadata)
    assert is_secret_like_environment_name(name)


def test_custom_allowlist_supports_exact_and_prefix_rules_but_cannot_allow_secrets():
    child, metadata = build_job_environment(
        {
            "HOME_MCP_JOB_ENV_ALLOW": "build_tool, MY_TOOL_*, OPENAI_*, bad*middle",
            "BUILD_TOOL": "exact-value",
            "MY_TOOL_ENDPOINT": "prefix-value",
            "MY_TOOL_API_KEY": "unique-secret-sentinel",
            "OPENAI_API_KEY": "unique-secret-sentinel",
            "UNLISTED": "not-inherited",
            "BAD_ENDMIDDLE": "not-inherited",
        }
    )

    assert child["BUILD_TOOL"] == "exact-value"
    assert child["MY_TOOL_ENDPOINT"] == "prefix-value"
    assert "MY_TOOL_API_KEY" not in child
    assert "OPENAI_API_KEY" not in child
    assert "UNLISTED" not in child
    assert "BAD_ENDMIDDLE" not in child
    assert metadata["env_dropped_secret_like_names"] == [
        "MY_TOOL_API_KEY",
        "OPENAI_API_KEY",
    ]
    assert "unique-secret-sentinel" not in json.dumps(metadata)


def test_name_matching_is_case_insensitive_and_allows_parenthesized_windows_names():
    child, metadata = build_job_environment(
        {
            "pAtH": "path-value",
            "programfiles(X86)": "program-files-value",
            "openai_api_key": "unique-secret-sentinel",
        }
    )

    assert child["pAtH"] == "path-value"
    assert child["programfiles(X86)"] == "program-files-value"
    assert "openai_api_key" not in child
    assert metadata["env_dropped_secret_like_names"] == ["openai_api_key"]


def test_explicit_env_is_added_after_filtering_even_for_secret_like_name():
    child, metadata = build_job_environment(
        {"PATH": "parent-path", "OPENAI_API_KEY": "parent-secret"},
        {"OPENAI_API_KEY": "explicit-secret", "CUSTOM_VALUE": "explicit-value"},
    )

    assert child == {
        "PATH": "parent-path",
        "OPENAI_API_KEY": "explicit-secret",
        "CUSTOM_VALUE": "explicit-value",
    }
    assert metadata["env_dropped_secret_like_names"] == ["OPENAI_API_KEY"]
    serialized = json.dumps(metadata)
    assert "parent-secret" not in serialized
    assert "explicit-secret" not in serialized
    assert "explicit-value" not in serialized


def test_explicit_env_overrides_parent_name_case_insensitively():
    child, metadata = build_job_environment(
        {"PATH": "parent-path"},
        {"pAtH": "explicit-path"},
    )

    assert child == {"pAtH": "explicit-path"}
    assert metadata["env_inherited_count"] == 0
    assert metadata["env_dropped_count"] == 1
