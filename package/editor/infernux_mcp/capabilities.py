"""Configurable capability gates for the Infernux MCP layer."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any

from infernux.engine.path_utils import resolved_path


CONFIG_REL_PATH = os.path.join("ProjectSettings", "mcp_capabilities.json")


VALID_PROFILES = frozenset({"developer_assist", "global_validation"})

# Capability domains served by the built-in operation set. The default grant
# is an explicit enumeration instead of "*": the persisted project config
# shows the user exactly what the agent may touch, grants can be trimmed per
# project, and capabilities added by future operation domains are never
# granted silently. test_mcp_server guards this list against drift.
READ_CAPABILITY_DOMAINS = (
    "asset",
    "camera",
    "capture",
    "console",
    "docs",
    "input",
    "material",
    "particle",
    "player",
    "project",
    "runtime",
    "scene",
    "session",
    "ui",
)
WRITE_CAPABILITY_DOMAINS = (
    "asset",
    "camera",
    "capture",
    "input",
    "material",
    "particle",
    "player",
    "runtime",
    "scene",
    "session",
    "ui",
)
DEFAULT_GRANTED_CAPABILITIES: tuple[str, ...] = tuple(
    f"{domain}.read" for domain in READ_CAPABILITY_DOMAINS
) + tuple(f"{domain}.write" for domain in WRITE_CAPABILITY_DOMAINS)

DEFAULT_CAPABILITY_CONFIG: dict[str, Any] = {
    "enabled": True,
    "profile": "developer_assist",
    "write_default_config_on_bootstrap": True,
    "granted_capabilities": list(DEFAULT_GRANTED_CAPABILITIES),
    "features": {
        "trace_recorder": True,
        "session_call_log": True,
        "discovery_files": True,
    },
    "session": {
        "build_profile": "debug_feedback",
        "recording_enabled": False,
        "cmake_configure_preset": "",
        "cmake_build_preset": "",
        "allowed_project_roots": [],
        "whl_readonly_source": [],
        "workaround_allowlist": [],
    },
    "limits": {
        "main_thread_timeout_ms": 30000,
        "trace_argument_max_string": 240,
        "trace_result_max_string": 480,
        "session_log_result_max_string": 480,
        "batch_max_steps": 100,
    },
}

_CURRENT_CONFIG: dict[str, Any] = copy.deepcopy(DEFAULT_CAPABILITY_CONFIG)
_PROJECT_PATH = ""


def configure(project_path: str, *, write_default: bool = True) -> dict[str, Any]:
    """Load, merge, and optionally materialize project MCP capability config."""
    global _CURRENT_CONFIG, _PROJECT_PATH
    _PROJECT_PATH = resolved_path(project_path or "")
    config = load_capability_config(_PROJECT_PATH)
    _CURRENT_CONFIG = config
    if write_default and bool(config.get("write_default_config_on_bootstrap", True)):
        save_config(config, _PROJECT_PATH)
    return copy.deepcopy(_CURRENT_CONFIG)


def current_config() -> dict[str, Any]:
    return copy.deepcopy(_CURRENT_CONFIG)


def apply_config(project_path: str, config: dict[str, Any]) -> dict[str, Any]:
    """Activate an already resolved config for one deterministic adapter build."""

    global _CURRENT_CONFIG, _PROJECT_PATH
    _PROJECT_PATH = resolved_path(project_path or "")
    _CURRENT_CONFIG = _normalized_config(config)
    return current_config()


def project_path() -> str:
    return _PROJECT_PATH


def config_path(project_path: str | None = None) -> str:
    root = resolved_path(project_path or _PROJECT_PATH or "")
    return os.path.join(root, CONFIG_REL_PATH)


def load_capability_config(project_path: str) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CAPABILITY_CONFIG)
    path = config_path(project_path)
    if not path or not os.path.isfile(path):
        return config
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            config = _normalized_config(data)
    except Exception:
        return config
    return config


def write_default_config(project_path: str | None = None) -> str:
    """Write a complete default-on config file if it does not exist yet."""
    path = config_path(project_path)
    if not path:
        return ""
    if os.path.isfile(path):
        return path
    _write_json_atomically(path, DEFAULT_CAPABILITY_CONFIG)
    return path


def save_config(config: dict[str, Any] | None = None, project_path: str | None = None) -> str:
    global _CURRENT_CONFIG
    if config is not None:
        _CURRENT_CONFIG = _normalized_config(config)
    path = config_path(project_path)
    _write_json_atomically(path, _CURRENT_CONFIG)
    return path


def _write_json_atomically(path: str, value: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".mcp-capabilities-", suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        raise


def is_enabled() -> bool:
    return bool(_CURRENT_CONFIG.get("enabled", True))


def feature_enabled(name: str) -> bool:
    return bool((_CURRENT_CONFIG.get("features") or {}).get(name, True))


def profile_name() -> str:
    """Return the active current MCP mode/profile."""
    profile = str(_CURRENT_CONFIG.get("profile", "developer_assist") or "developer_assist")
    return profile if profile in VALID_PROFILES else "developer_assist"


def session_config() -> dict[str, Any]:
    """Return the project-local remote-session policy block."""
    value = _CURRENT_CONFIG.get("session") or {}
    return copy.deepcopy(value if isinstance(value, dict) else {})


def limit(name: str, default: Any = None) -> Any:
    return (_CURRENT_CONFIG.get("limits") or {}).get(name, default)


def set_feature(name: str, enabled: bool) -> dict[str, Any]:
    _CURRENT_CONFIG.setdefault("features", {})[str(name)] = bool(enabled)
    return current_config()


def _normalized_config(config: dict[str, Any]) -> dict[str, Any]:
    source = config if isinstance(config, dict) else {}
    normalized = copy.deepcopy(DEFAULT_CAPABILITY_CONFIG)
    for key in ("enabled", "write_default_config_on_bootstrap"):
        if key in source:
            normalized[key] = bool(source[key])
    profile = str(source.get("profile", normalized["profile"]))
    normalized["profile"] = profile if profile in VALID_PROFILES else "developer_assist"
    granted = source.get("granted_capabilities")
    if isinstance(granted, list) and all(isinstance(item, str) for item in granted):
        normalized["granted_capabilities"] = list(dict.fromkeys(granted))
    for section in ("features", "session", "limits"):
        values = source.get(section)
        if not isinstance(values, dict):
            continue
        allowed = set(normalized[section])
        if section == "session":
            allowed.update({"session_id", "managed_checkpoints_required"})
        for key in allowed:
            if key in values:
                normalized[section][key] = copy.deepcopy(values[key])
    return normalized
