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
    """Activate valid policy; materialize defaults only for a missing file."""
    global _CURRENT_CONFIG, _PROJECT_PATH
    root = resolved_path(project_path or "")
    path = config_path(root)
    config = _read_config(path)
    if config is None:
        config = copy.deepcopy(DEFAULT_CAPABILITY_CONFIG)
        if write_default and config["write_default_config_on_bootstrap"]:
            _write_json_atomically(path, config, create_only=True)
    _PROJECT_PATH, _CURRENT_CONFIG = root, config
    return copy.deepcopy(_CURRENT_CONFIG)


def current_config() -> dict[str, Any]:
    return copy.deepcopy(_CURRENT_CONFIG)


def apply_config(project_path: str, config: dict[str, Any]) -> dict[str, Any]:
    """Activate an already resolved config for one deterministic adapter build."""

    global _CURRENT_CONFIG, _PROJECT_PATH
    root = resolved_path(project_path or "")
    normalized = _normalized_config(config)
    _PROJECT_PATH, _CURRENT_CONFIG = root, normalized
    return current_config()


def project_path() -> str:
    return _PROJECT_PATH


def config_path(project_path: str | None = None) -> str:
    root = resolved_path(project_path or _PROJECT_PATH or "")
    return os.path.join(root, CONFIG_REL_PATH)


def load_capability_config(project_path: str) -> dict[str, Any]:
    config = _read_config(config_path(project_path))
    return copy.deepcopy(DEFAULT_CAPABILITY_CONFIG) if config is None else config


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate field {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _read_config(path: str) -> dict[str, Any] | None:
    """None means absent. Existing unreadable or invalid policy must reject."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
        return _normalized_config(data)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise
        return None
    except ValueError as exc:
        raise ValueError(f"Invalid MCP capability configuration {path}: {exc}") from exc


def write_default_config(project_path: str | None = None) -> str:
    """Write a complete default-on config file if it does not exist yet."""
    path = config_path(project_path)
    if _read_config(path) is None:
        _write_json_atomically(path, DEFAULT_CAPABILITY_CONFIG, create_only=True)
    return path


def save_config(config: dict[str, Any] | None = None, project_path: str | None = None) -> str:
    global _CURRENT_CONFIG
    normalized = _normalized_config(_CURRENT_CONFIG if config is None else config)
    path = config_path(project_path)
    _write_json_atomically(path, normalized)
    _CURRENT_CONFIG = normalized
    return path


def _write_json_atomically(path: str, value: dict[str, Any], *, create_only: bool = False) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".mcp-capabilities-", suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, indent=2, ensure_ascii=False, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if create_only:
            # Publish one complete file without replacing policy created by
            # another process after the initial read. A collision is an error.
            if os.name == "nt":
                # Windows rename never replaces an existing destination and
                # works on supported filesystems without requiring hard links.
                os.rename(temporary_path, path)
            else:
                os.link(temporary_path, path)
                os.remove(temporary_path)
        else:
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
    if not isinstance(config, dict):
        raise ValueError("configuration must be a JSON object")
    source = config
    normalized = copy.deepcopy(DEFAULT_CAPABILITY_CONFIG)
    for key in ("enabled", "write_default_config_on_bootstrap"):
        if key in source:
            _validate_value(key, source[key], bool)
            normalized[key] = source[key]
    profile = source.get("profile", normalized["profile"])
    if not isinstance(profile, str) or profile not in VALID_PROFILES:
        raise ValueError(f"profile must be one of {sorted(VALID_PROFILES)}")
    normalized["profile"] = profile
    if "granted_capabilities" in source:
        granted = source["granted_capabilities"]
        _validate_value("granted_capabilities", granted, list)
        normalized["granted_capabilities"] = list(dict.fromkeys(granted))
    for section in ("features", "session", "limits"):
        if section not in source:
            continue
        values = source[section]
        if not isinstance(values, dict):
            raise ValueError(f"{section} must be a JSON object")
        schema = {key: type(value) for key, value in normalized[section].items()}
        if section == "session":
            schema.update(session_id=str, managed_checkpoints_required=bool)
        for key, expected in schema.items():
            if key in values:
                _validate_value(f"{section}.{key}", values[key], expected)
                if section == "limits":
                    minimum = 1 if key in {"main_thread_timeout_ms", "batch_max_steps"} else 0
                    if values[key] < minimum:
                        raise ValueError(f"{section}.{key} must be at least {minimum}")
                normalized[section][key] = copy.deepcopy(values[key])
    return normalized


def _validate_value(name: str, value: Any, expected: type) -> None:
    if type(value) is not expected:
        raise ValueError(f"{name} must be {expected.__name__}")
    if expected is list and not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must contain only strings")
