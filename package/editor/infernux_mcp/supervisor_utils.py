"""Pure validation, process, and persistence helpers for SupervisorSession."""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict
from typing import Any

from infernux.engine.path_utils import (
    is_path_within,
    path_fingerprint,
    relative_path,
    resolved_path,
    same_path,
)
from infernux.engine.platform_player_bootstrap import read_player_build_manifest


HANDOFF_STATES = frozenset({"idle", "started", "completed", "failed"})


def _validate_player_executable(executable_path: str, project_root: str) -> tuple[str, dict[str, Any]]:
    launcher = resolved_path(str(executable_path or ""))
    if not os.path.isfile(launcher):
        raise FileNotFoundError(f"Player executable was not found: {launcher}")

    output_root = os.path.dirname(launcher)
    game_name = os.path.splitext(os.path.basename(launcher))[0]
    data_root = resolved_path(os.path.join(output_root, f"{game_name}_Data"))
    try:
        manifest = read_player_build_manifest(data_root)
    except (OSError, RuntimeError, ValueError):
        manifest = {}
    if not manifest:
        raise FileNotFoundError(
            "Player BuildManifest was not found in the sealed asset catalog: "
            f"{data_root}"
        )

    player_manifest = _read_json_object(os.path.join(data_root, "Player.inxmanifest"))
    product = player_manifest.get("product") or {}
    if product.get("layout") != "single_executable_native_packages":
        raise ValueError("Player executable is not the launcher of a current Infernux Player layout.")
    entry_points = [str(value or "") for value in product.get("entry_points", []) or []]
    if not bool(product.get("single_entry_point", False)) or entry_points != [os.path.basename(launcher)]:
        raise ValueError("Player single-entry manifest does not match the selected executable.")
    build_output = manifest.get("build_output") or {}
    if str(build_output.get("project_identity", "") or "") != path_fingerprint(project_root):
        raise ValueError("Player build output belongs to a different project.")
    if not bool(manifest.get("debug_build", False)):
        raise RuntimeError("Supervisor validation control is available only in a Debug Player build.")
    runtime_policy = (manifest.get("runtime_contract") or {}).get("runtime_policy") or {}
    if runtime_policy.get("player_control") != "token_authenticated":
        raise RuntimeError("Debug Player does not expose the authenticated validation control channel.")
    return launcher, manifest


def _player_data_root(runtime_executable: str) -> str:
    runtime = resolved_path(str(runtime_executable or ""))
    runtime_directory = os.path.dirname(runtime)
    single_entry_data = resolved_path(
        os.path.join(runtime_directory, f"{os.path.splitext(os.path.basename(runtime))[0]}_Data")
    )
    player_manifest = _read_json_object(os.path.join(single_entry_data, "Player.inxmanifest"))
    if (
        (player_manifest.get("product") or {}).get("layout")
        != "single_executable_native_packages"
    ):
        raise ValueError("Player runtime executable is not inside the current organized Player layout.")
    return single_entry_data


def _resolve_player_start_scene(start_scene: str, project_root: str, manifest: dict[str, Any]) -> str:
    """Resolve an authoring scene request to its BuildManifest GUID."""
    requested = str(start_scene or "").strip()
    if not requested:
        return ""

    scene_guids = manifest.get("scene_guids", []) or []
    if requested in scene_guids:
        return requested
    root = resolved_path(project_root)
    candidate = resolved_path(
        requested if os.path.isabs(requested) else os.path.join(root, requested)
    )
    index = _read_json_object(os.path.join(root, "Library", "AssetIndex.json"))
    for entry in index.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        guid = str(entry.get("guid", ""))
        source = str(entry.get("normalized_path", ""))
        source_path = (
            source if os.path.isabs(source) else os.path.join(root, source)
        )
        if guid in scene_guids and source and same_path(source_path, candidate):
            return guid
    raise ValueError("Player validation start_scene must be declared by the current Debug Player BuildManifest.")


def _compact_checkpoint_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    ledger = manifest.get("ledger") or {}
    return {
        "checkpoint_id": str(manifest.get("checkpoint_id", "") or ""),
        "created_at": float(manifest.get("created_at", 0.0) or 0.0),
        "manifest_path": str(manifest.get("manifest_path", "") or ""),
        "ledger_digest": str(ledger.get("digest", "") or ""),
        "file_count": int(ledger.get("file_count", 0) or 0),
        "total_bytes": int(ledger.get("total_bytes", 0) or 0),
        "metadata": dict(manifest.get("metadata") or {}),
    }


def _validate_project_root(path: str) -> None:
    root = resolved_path(path)
    if not os.path.isabs(root):
        raise ValueError("Project root must be an absolute path.")
    home = resolved_path(os.path.expanduser("~"))
    desktop = os.path.join(home, "Desktop")
    if same_path(root, desktop):
        raise ValueError("Project root must be a named folder under Desktop, not the entire Desktop.")


def _require_choice(name: str, value: str, allowed: frozenset[str]) -> str:
    normalized = str(value or "")
    if normalized not in allowed:
        raise ValueError(f"Unsupported {name}: {normalized!r}")
    return normalized


def _require_loopback_host(value: str) -> str:
    host = str(value or "").strip()
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Supervisor MCP host must remain loopback-only.")
    return host


def _require_port(value: int) -> int:
    port = int(value)
    if port < 1024 or port > 65535:
        raise ValueError("Supervisor MCP port must be between 1024 and 65535.")
    return port


def _available_port(host: str, preferred: int) -> int:
    if _port_available(host, preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, int(port)))
        return True
    except OSError:
        return False


def _pid_is_running(pid: int) -> bool:
    """Return whether a persisted process identifier still belongs to a live process."""
    target = int(pid or 0)
    if target <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information,
                False,
                target,
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(target, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> None:
    """Terminate a reattached process after the caller has completed preflight."""
    target = int(pid or 0)
    if target <= 0:
        return
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(target), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0 and _pid_is_running(target):
            raise RuntimeError(f"Failed to terminate attached Editor process {target}.")
        return
    os.kill(target, signal.SIGTERM)


def _wait_for_pid_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(float(timeout_seconds), 0.1)
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_running(pid)


def _read_json_object(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_json_line(line: str) -> dict[str, Any]:
    try:
        value = json.loads(str(line or ""))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_handoff_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    state = str(value.get("state", "") or "")
    if state and state not in HANDOFF_STATES:
        return {}
    return dict(value)


def _normalize_player_component_probes(
    component_probes: list[dict[str, Any]] | None,
    object_names: list[str],
) -> list[dict[str, Any]]:
    raw_probes = component_probes or []
    if len(raw_probes) > 16:
        raise ValueError("component_probes cannot contain more than 16 entries.")
    allowed_names = set(object_names)
    probes = []
    for raw in raw_probes:
        if not isinstance(raw, dict):
            raise ValueError("component_probes entries must be objects.")
        object_name = str(raw.get("object_name", "") or "").strip()
        component_type = str(raw.get("component_type", "") or "").strip()
        fields = [str(field or "").strip() for field in raw.get("fields", [])]
        ordinal = int(raw.get("ordinal", 0) or 0)
        if object_name not in allowed_names:
            raise ValueError("component probe object_name must also be present in object_names.")
        if not component_type or ordinal < 0 or not fields or len(fields) > 16:
            raise ValueError("component probes require a public component type, ordinal, and 1-16 fields.")
        if any(not field or field.startswith("_") for field in fields):
            raise ValueError("component probe fields must be non-empty public field names.")
        probes.append({
            "object_name": object_name,
            "component_type": component_type,
            "fields": fields,
            "ordinal": ordinal,
        })
    return probes


def _bounded_finite_float(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}.")
    return result


def _normalize_player_hold_scancodes(
    hold_key: str | int | None,
    hold_keys: list[str | int] | None,
) -> list[int]:
    if hold_key is not None and hold_keys:
        raise ValueError("Use hold_key or hold_keys, not both.")
    values = list(hold_keys or ([] if hold_key is None else [hold_key]))
    if len(values) > 8:
        raise ValueError("hold_keys may contain at most 8 keys.")
    from infernux.lib import InputManager

    scancodes = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("hold_keys entries must be key names or SDL scancodes.")
        scancode = int(value) if isinstance(value, int) else int(InputManager.name_to_scancode(str(value)))
        if scancode <= 0:
            raise ValueError(f"Unknown hold key: {value!r}.")
        scancodes.append(scancode)
    if len(set(scancodes)) != len(scancodes):
        raise ValueError("hold_keys must not contain duplicate keys.")
    return scancodes


def _normalize_player_hold_mouse_buttons(value: list[int] | None) -> list[int]:
    buttons = list(value or [])
    if len(buttons) > 5:
        raise ValueError("hold_mouse_buttons may contain at most 5 buttons.")
    normalized: list[int] = []
    for button in buttons:
        if isinstance(button, bool) or int(button) not in range(5):
            raise ValueError("hold_mouse_buttons must use Unity button indices 0 through 4.")
        normalized.append(int(button))
    if len(set(normalized)) != len(normalized):
        raise ValueError("hold_mouse_buttons must not contain duplicate buttons.")
    return normalized


def _normalize_player_stop_assertions(assertions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    values = list(assertions or [])
    if len(values) > 16:
        raise ValueError("stop_assertions may contain at most 16 items.")
    normalized = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("stop_assertions entries must be objects.")
        normalized.append(dict(item))
    return normalized


def _normalize_player_discovery_component_types(component_types: list[str] | None) -> list[str]:
    values = [str(value or "").strip() for value in component_types or [] if str(value or "").strip()]
    if len(values) > 16:
        raise ValueError("discovery_component_types cannot contain more than 16 entries.")
    if any(value.startswith("_") for value in values):
        raise ValueError("discovery_component_types must contain public component type names.")
    return list(dict.fromkeys(values))


def _normalize_player_discovered_object_count(value: int) -> int:
    if isinstance(value, bool):
        raise ValueError("max_discovered_objects must be an integer.")
    count = int(value)
    if count < 1 or count > 64:
        raise ValueError("max_discovered_objects must be between 1 and 64.")
    return count


def _tail_text_lines(path: str, limit: int) -> list[str]:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()
    except OSError:
        return []
    return [line.rstrip("\r\n") for line in lines[-max(1, int(limit)):]]


def _text_file_baseline(path: str) -> dict[str, int]:
    if not path:
        return {}
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    return {
        "size": int(stat.st_size),
        "modified_ns": int(stat.st_mtime_ns),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
    }


def _normalize_file_baselines(value: Any) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, int]] = {}
    for name in ("runtime", "debug", "crash"):
        record = value.get(name)
        if not isinstance(record, dict):
            continue
        try:
            normalized[name] = {
                key: int(record.get(key, 0) or 0)
                for key in ("size", "modified_ns", "device", "inode")
            }
        except (TypeError, ValueError):
            continue
    return normalized


def _tail_text_lines_since(
    path: str,
    limit: int,
    baseline: dict[str, int] | None,
    *,
    append_only: bool = False,
) -> list[str]:
    if not baseline:
        return _tail_text_lines(path, limit)
    current = _text_file_baseline(path)
    if not current:
        return []
    if (
        current["size"] == int(baseline.get("size", 0))
        and current["modified_ns"] == int(baseline.get("modified_ns", 0))
        and current["device"] == int(baseline.get("device", 0))
        and current["inode"] == int(baseline.get("inode", 0))
    ):
        return []
    start = 0
    if (
        append_only
        and current["device"] == int(baseline.get("device", 0))
        and current["inode"] == int(baseline.get("inode", 0))
        and current["size"] >= int(baseline.get("size", 0))
    ):
        start = int(baseline.get("size", 0))
    try:
        with open(path, "rb") as stream:
            stream.seek(start)
            text = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-max(1, int(limit)):]


def _secret_fingerprint(value: str) -> str:
    secret = str(value or "")
    if not secret:
        return ""
    import hashlib

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _mcp_health_is_alive(endpoint: str) -> bool:
    try:
        request = urllib.request.Request(endpoint, method="GET")
        with urllib.request.urlopen(request, timeout=0.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("name") == "Infernux Editor"
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def _write_json(path: str, value: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary_path = os.path.join(directory, f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary_path, "w", encoding="utf-8", newline="\n") as f:
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


def _append_json_line(path: str, value: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def _run_async(factory) -> Any:
    """Run a small async MCP query from either synchronous or async callers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: list[Any] = []
    error: list[BaseException] = []

    def run_in_thread() -> None:
        try:
            result.append(asyncio.run(factory()))
        except BaseException as exc:  # Propagate the original MCP failure to the caller.
            error.append(exc)

    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]

__all__ = [
    "_validate_player_executable",
    "_player_data_root",
    "_resolve_player_start_scene",
    "_compact_checkpoint_manifest",
    "_validate_project_root",
    "_require_choice",
    "_require_loopback_host",
    "_require_port",
    "_available_port",
    "_port_available",
    "_pid_is_running",
    "_terminate_pid",
    "_wait_for_pid_exit",
    "_read_json_object",
    "_read_json_line",
    "_normalize_handoff_record",
    "_normalize_player_component_probes",
    "_bounded_finite_float",
    "_normalize_player_hold_scancodes",
    "_normalize_player_hold_mouse_buttons",
    "_normalize_player_stop_assertions",
    "_normalize_player_discovery_component_types",
    "_normalize_player_discovered_object_count",
    "_tail_text_lines",
    "_text_file_baseline",
    "_normalize_file_baselines",
    "_tail_text_lines_since",
    "_secret_fingerprint",
    "_mcp_health_is_alive",
    "_write_json",
    "_append_json_line",
    "_run_async"
]
