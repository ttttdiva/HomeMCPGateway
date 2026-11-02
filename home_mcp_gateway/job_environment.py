"""Build a minimal environment for Gateway job command processes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_ALLOWED_EXACT_NAMES = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMPUTERNAME",
        "COMSPEC",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "COMMONPROGRAMFILESW6432",
        "COMMONPROGRAMW6432",
        "CYGWIN",
        "HOMEDRIVE",
        "HOMEPATH",
        "HOME",
        "LANG",
        "LOCALAPPDATA",
        "MSYS",
        "MSYSTEM",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATHEXT",
        "PATH",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "PUBLIC",
        "PYTHONHOME",
        "PYTHONIOENCODING",
        "PYTHONPATH",
        "PYTHONUTF8",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "VIRTUAL_ENV",
        "WINDIR",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
)

_ALLOWED_PREFIXES = (
    "AOITALK_",
    "CODEX_",
    "CYGWIN_",
    "GIT_",
    "HOME_MCP_",
    "LC_",
    "MSYS_",
    "MSYS2_",
    "NODE_",
    "NPM_CONFIG_",
    "PROCESSOR_",
    "WSL_",
)

_PROVIDER_PREFIXES = (
    "OPENAI_",
    "ANTHROPIC_",
    "OPENROUTER_",
    "GEMINI_",
    "DEEPSEEK_",
    "XAI_",
    "GROK_",
    "MISTRAL_",
    "COHERE_",
)

_DENIED_EXACT_NAMES = frozenset(
    {
        "API_KEY",
        "TOKEN",
        "SECRET",
        "GIT_ASKPASS",
        "GIT_ASKPASS_REQUIRE",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "HF_TOKEN",
    }
)


def is_secret_like_environment_name(name: str) -> bool:
    """Return whether an environment variable name is never inherited by default."""
    normalized = str(name).upper()
    if normalized in _DENIED_EXACT_NAMES:
        return True
    if normalized.endswith(("_API_KEY", "_TOKEN", "_SECRET")):
        return True
    if "PASSWORD" in normalized or "CREDENTIAL" in normalized:
        return True
    if normalized.startswith(_PROVIDER_PREFIXES):
        return True
    if normalized.startswith("AWS_") and any(
        marker in normalized for marker in ("ACCESS_KEY", "SECRET", "SESSION")
    ):
        return True
    if normalized.startswith("AZURE_") and any(
        marker in normalized for marker in ("KEY", "SECRET")
    ):
        return True
    if normalized.startswith("CLAUDE_") and any(
        marker in normalized for marker in ("KEY", "TOKEN", "SECRET", "OAUTH")
    ):
        return True
    if normalized.startswith("NPM_CONFIG_") and "AUTH" in normalized:
        return True
    return False


def _additional_allow_rules(value: str | None) -> tuple[set[str], tuple[str, ...]]:
    exact: set[str] = set()
    prefixes: list[str] = []
    for raw_item in str(value or "").split(","):
        item = raw_item.strip()
        if not item:
            continue
        if "*" in item:
            if item.endswith("*") and item.count("*") == 1 and len(item) > 1:
                prefixes.append(item[:-1].upper())
            continue
        exact.add(item.upper())
    return exact, tuple(prefixes)


def _allowed_by_name(name: str, extra_exact: set[str], extra_prefixes: tuple[str, ...]) -> bool:
    normalized = str(name).upper()
    if is_secret_like_environment_name(normalized):
        return False
    if normalized in _ALLOWED_EXACT_NAMES or normalized in extra_exact:
        return True
    return any(normalized.startswith(prefix) for prefix in (*_ALLOWED_PREFIXES, *extra_prefixes))


def build_job_environment(
    parent_environment: Mapping[str, Any],
    explicit_environment: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Filter the parent environment, then add caller-explicit values unchanged.

    Additional inherited names may be allowlisted by ``HOME_MCP_JOB_ENV_ALLOW``
    as comma-separated exact names or ``PREFIX*`` rules. Deny patterns always
    take precedence over this setting. Explicit ``env=`` values are intentionally
    distinct and are copied after filtering.
    """
    raw_rules = parent_environment.get("HOME_MCP_JOB_ENV_ALLOW")
    if raw_rules is None:
        raw_rules = parent_environment.get("home_mcp_job_env_allow")
    extra_exact, extra_prefixes = _additional_allow_rules(
        str(raw_rules) if raw_rules is not None else None
    )

    inherited: dict[str, Any] = {}
    dropped_names: list[str] = []
    secret_names: list[str] = []
    explicit = explicit_environment or {}
    explicit_names = {str(name).upper() for name in explicit}
    for raw_name, value in parent_environment.items():
        name = str(raw_name)
        if (
            _allowed_by_name(name, extra_exact, extra_prefixes)
            and name.upper() not in explicit_names
        ):
            inherited[name] = value
            continue
        dropped_names.append(name)
        if is_secret_like_environment_name(name):
            secret_names.append(name)

    child_environment = dict(inherited)
    child_environment.update(explicit)

    metadata = {
        "env_policy": "allowlist-v1",
        "env_inherited_count": len(inherited),
        "env_dropped_count": len(dropped_names),
        "env_dropped_secret_like_names": sorted(set(secret_names), key=str.upper),
    }
    return child_environment, metadata
