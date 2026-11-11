"""Build identity capture used by validation sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from infernux.engine.path_utils import relative_path, resolved_path
from infernux.version import ENGINE_COMPATIBILITY_VERSION


def capture_build_identity(
    options: dict[str, Any], build_profile: str
) -> dict[str, Any]:
    source_root = _find_source_root()
    from infernux.lib import _Infernux
    native_module = resolved_path(_Infernux.__file__)
    return {
        "captured_at": time.time(),
        "source_root": source_root,
        "package_version": ENGINE_COMPATIBILITY_VERSION,
        "native_module": native_module,
        "git": _git_identity(source_root),
        "cmake": _cmake_identity(source_root, options, build_profile, native_module),
    }


def _find_source_root() -> str:
    # An installed MCP package can live in an unrelated game project. The
    # running engine, rather than the transport plugin, owns build identity.
    import infernux
    module_path = Path(resolved_path(infernux.__file__))
    for candidate in (module_path.parent, *module_path.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "CMakePresets.json"
        ).is_file():
            return str(candidate)
    return ""


def _git_identity(source_root: str) -> dict[str, Any]:
    revision = _run_git(source_root, "rev-parse", "HEAD")
    if revision is None:
        return {"available": False}
    branch = _run_git(source_root, "rev-parse", "--abbrev-ref", "HEAD") or ""
    status = _run_git(source_root, "status", "--porcelain", "--untracked-files=no")
    lines = status.splitlines() if status is not None else []
    return {
        "available": True,
        "revision": revision,
        "branch": branch,
        "tracked_worktree_dirty": bool(lines),
        "tracked_change_count": len(lines),
    }


def _run_git(source_root: str, *args: str) -> str | None:
    if not source_root:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", source_root, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _cmake_identity(
    source_root: str, options: dict[str, Any], build_profile: str, native_module: str
) -> dict[str, Any]:
    default_preset = "debug" if build_profile == "debug_feedback" else "release"
    configured = str(options.get("cmake_configure_preset", "") or "").strip()
    build = str(options.get("cmake_build_preset", "") or "").strip()
    native_directory = Path(native_module).parent
    cache = native_directory.parent / "CMakeCache.txt"
    cache_path = str(cache) if cache.is_file() else ""
    build_type = ""
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if line.startswith("CMAKE_BUILD_TYPE:"):
                        build_type = line.split("=", 1)[1].strip()
                        break
        except OSError:
            pass
    build_preset = build or default_preset
    configuration, presets_path = _build_preset_configuration(
        source_root, build_preset
    )
    return {
        "configure_preset": configured or default_preset,
        "configure_preset_source": "session_settings" if configured else "build_profile_inference",
        "build_preset": build_preset,
        "build_preset_source": "session_settings" if build else "build_profile_inference",
        "build_configuration": configuration,
        "build_configuration_source": "CMakePresets.json" if configuration else "",
        "presets_path": presets_path,
        "cache_path": _relative(source_root, cache_path) if cache_path else "",
        "cache_configured_build_type": build_type,
        "native_configuration": native_directory.name if cache_path else "",
    }


def _build_preset_configuration(
    source_root: str, build_preset: str
) -> tuple[str, str]:
    path = os.path.join(source_root, "CMakePresets.json") if source_root else ""
    if not path or not os.path.isfile(path):
        return "", ""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return "", _relative(source_root, path)
    for preset in document.get("buildPresets") or []:
        if isinstance(preset, dict) and str(preset.get("name", "")) == build_preset:
            return str(preset.get("configuration", "") or ""), _relative(
                source_root, path
            )
    return "", _relative(source_root, path)


def _relative(root: str, path: str) -> str:
    if not path:
        return ""
    try:
        return relative_path(path, root, allow_root=True) if root else path
    except ValueError:
        return path


__all__ = ["capture_build_identity"]
