"""Standalone Debug Player validation operations."""

from __future__ import annotations

import json
import os

from infernux.host import EditorAutomationHost, Operation, OperationError, OperationKind

from infernux_mcp import session
from infernux_mcp.operation_support import operation
from infernux_mcp.supervisor import SupervisorSession


_LOCAL_SUPERVISORS: dict[tuple[str, str], SupervisorSession] = {}


def build_player_operations(project_path: str) -> tuple[Operation, ...]:
    return (
        operation(
            "infernux.player.targets",
            OperationKind.QUERY,
            "List Player targets contributed by core and enabled platform plugins.",
            lambda: EditorAutomationHost.instance().player_build_targets(),
            capability="player.read",
            tags=("player", "build", "target", "capability"),
        ),
        operation(
            "infernux.player.build",
            OperationKind.WORKFLOW,
            "Build one registered Player target through the shared build service.",
            lambda target="", output_dir="", game_name="", debug_mode=None,
            lto=None, android_artifact="",
            compress_resources=None, persist_settings=True: _build(
                project_path,
                target=target,
                output_dir=output_dir,
                game_name=game_name,
                debug_mode=debug_mode,
                lto=lto,
                android_artifact=android_artifact,
                compress_resources=compress_resources,
                persist_settings=persist_settings,
            ),
            capability="player.write",
            input_properties={
                "target": {
                    "type": "string",
                    "default": "",
                    "description": "Registered target ID; empty uses Build Settings or the current desktop target.",
                },
                "output_dir": {"type": "string", "default": ""},
                "game_name": {"type": "string", "default": ""},
                "debug_mode": {"type": ["boolean", "null"], "default": None},
                "lto": {"type": ["boolean", "null"], "default": None},
                "android_artifact": {
                    "type": "string",
                    "enum": ["", "apk", "aab"],
                    "default": "",
                },
                "compress_resources": {
                    "type": ["boolean", "null"],
                    "default": None,
                },
                "persist_settings": {"type": "boolean", "default": True},
            },
            side_effects=("Writes a standalone Player package and optionally Build Settings.",),
            tags=("player", "build", "package", "workflow"),
        ),
        operation(
            "infernux.player.validation.launch",
            OperationKind.COMMAND,
            "Launch the configured Debug Player under Supervisor control.",
            lambda executable_path="", start_scene="", timeout_seconds=60.0: _launch(project_path, executable_path, start_scene, timeout_seconds),
            capability="player.write",
            input_properties={
                "executable_path": {"type": "string", "default": ""},
                "start_scene": {"type": "string", "default": ""},
                "timeout_seconds": {"type": "number", "default": 60.0},
            },
            side_effects=("Starts a managed standalone Player process.",),
            tags=("player", "validation", "launch", "supervisor"),
        ),
        operation(
            "infernux.player.validation.status",
            OperationKind.QUERY,
            "Read managed Player process and readiness state.",
            lambda: _supervisor().status(),
            capability="player.read",
            tags=("player", "validation", "status"),
        ),
        operation(
            "infernux.player.validation.observe",
            OperationKind.QUERY,
            "Observe bounded public state from the managed Player.",
            _observe,
            capability="player.read",
            input_properties={
                "object_names": {"type": "array", "items": {"type": "string"}},
                "component_probes": {"type": "array", "items": {"type": "object"}},
                "include_scene_objects": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "default": 3.0},
            },
            tags=("player", "validation", "observe", "runtime"),
        ),
        operation(
            "infernux.player.validation.key",
            OperationKind.COMMAND,
            "Send one keyboard transition to the managed Player.",
            _key,
            capability="player.write",
            input_properties={
                "key": {"type": ["string", "integer"]},
                "pressed": {"type": "boolean"},
                "repeat": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "default": 3.0},
            },
            required=("key", "pressed"),
            side_effects=("Injects one SDL keyboard event into the managed Player.",),
            tags=("player", "validation", "input", "keyboard"),
        ),
        operation(
            "infernux.player.validation.key.press",
            OperationKind.WORKFLOW,
            "Press and release one Player key with engine-controlled timing.",
            _press_key,
            capability="player.write",
            input_properties={
                "key": {"type": ["string", "integer"]},
                "duration_seconds": {"type": "number", "default": 0.1},
                "object_names": {"type": "array", "items": {"type": "string"}},
                "component_probes": {"type": "array", "items": {"type": "object"}},
                "timeout_seconds": {"type": "number", "default": 3.0},
            },
            required=("key",),
            side_effects=("Injects a timed SDL key press into the managed Player.",),
            tags=("player", "validation", "input", "keyboard", "press"),
        ),
        operation(
            "infernux.player.validation.pointer.button",
            OperationKind.COMMAND,
            "Send one pointer-button transition to the managed Player.",
            _pointer_button,
            capability="player.write",
            input_properties={
                "button": {"type": "integer"},
                "pressed": {"type": "boolean"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "timeout_seconds": {"type": "number", "default": 3.0},
            },
            required=("button", "pressed", "x", "y"),
            side_effects=("Injects one SDL pointer-button event into the managed Player.",),
            tags=("player", "validation", "input", "pointer"),
        ),
        operation(
            "infernux.player.validation.capture",
            OperationKind.COMMAND,
            "Capture the managed Player Game render target for human review.",
            lambda file_name="player-game.png", timeout_seconds=30.0: _supervisor().player_capture_game(file_name, timeout_seconds=timeout_seconds),
            capability="player.write",
            input_properties={
                "file_name": {"type": "string", "default": "player-game.png"},
                "timeout_seconds": {"type": "number", "default": 30.0},
            },
            side_effects=("Writes one Player render-target capture artifact.",),
            tags=("player", "validation", "capture", "render-target"),
        ),
        operation(
            "infernux.player.validation.logs",
            OperationKind.QUERY,
            "Read bounded managed Player runtime and stdout log tails.",
            lambda limit=200: _supervisor().player_read_logs(limit=max(1, min(int(limit), 2000))),
            capability="player.read",
            input_properties={"limit": {"type": "integer", "default": 200}},
            tags=("player", "validation", "logs", "console"),
        ),
        operation(
            "infernux.player.validation.motion.arm",
            OperationKind.COMMAND,
            "Arm bounded Player-owned motion sampling for public runtime objects.",
            _motion_arm,
            capability="player.write",
            input_properties={
                "object_names": {"type": "array", "items": {"type": "string"}},
                "seconds": {"type": "number", "default": 2.0},
                "sample_interval": {"type": "number", "default": 0.1},
                "trigger_scene_name": {"type": "string", "default": ""},
                "trigger_timeout": {"type": "number", "default": 60.0},
                "hold_keys": {"type": "array", "items": {"type": ["string", "integer"]}},
                "hold_mouse_buttons": {"type": "array", "items": {"type": "integer"}},
                "component_probes": {"type": "array", "items": {"type": "object"}},
                "stop_assertions": {"type": "array", "items": {"type": "object"}},
                "timeout_seconds": {"type": "number", "default": 3.0},
            },
            required=("object_names",),
            side_effects=("Arms a bounded observation job inside the managed Player.",),
            tags=("player", "validation", "motion", "capture", "runtime"),
        ),
        operation(
            "infernux.player.validation.motion.status",
            OperationKind.QUERY,
            "Read one managed Player motion-sampling job.",
            lambda capture_id, timeout_seconds=3.0: _supervisor().player_motion_capture_status(capture_id, timeout_seconds=timeout_seconds),
            capability="player.read",
            input_properties={
                "capture_id": {"type": "string"},
                "timeout_seconds": {"type": "number", "default": 3.0},
            },
            required=("capture_id",),
            tags=("player", "validation", "motion", "status", "runtime"),
        ),
        operation(
            "infernux.player.validation.motion.cancel",
            OperationKind.COMMAND,
            "Cancel one managed Player motion-sampling job.",
            lambda capture_id, timeout_seconds=3.0: _supervisor().player_motion_capture_cancel(capture_id, timeout_seconds=timeout_seconds),
            capability="player.write",
            input_properties={
                "capture_id": {"type": "string"},
                "timeout_seconds": {"type": "number", "default": 3.0},
            },
            required=("capture_id",),
            side_effects=("Cancels a Player-owned observation job.",),
            tags=("player", "validation", "motion", "cancel", "runtime"),
        ),
        operation(
            "infernux.player.validation.shutdown",
            OperationKind.COMMAND,
            "Request normal managed Player shutdown without force termination.",
            lambda timeout_seconds=15.0: _supervisor().stop_player(timeout_seconds=timeout_seconds),
            capability="player.write",
            input_properties={"timeout_seconds": {"type": "number", "default": 15.0}},
            side_effects=("Requests normal shutdown of the managed Player.",),
            tags=("player", "validation", "shutdown", "supervisor"),
        ),
    )


def _supervisor() -> SupervisorSession:
    try:
        active = session.current()
    except session.McpPolicyError as exc:
        raise OperationError("player.session_required", str(exc)) from exc
    if active.build_profile != "debug_feedback":
        raise OperationError("player.unavailable", "Player validation requires debug_feedback.")
    key = (active.project_root, active.session_id)
    supervisor = _LOCAL_SUPERVISORS.get(key)
    if supervisor is None:
        try:
            supervisor = SupervisorSession.attach_current_host(
                active.project_root,
                active.session_id,
                mode=active.mode,
                build_profile=active.build_profile,
                editor_instance_id=active.editor_instance_id,
                mcp_host=os.environ.get("INFERNUX_MCP_HOST", "127.0.0.1"),
                mcp_port=int(os.environ.get("INFERNUX_MCP_PORT", "9713")),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise OperationError("player.supervisor_attach", str(exc)) from exc
        _LOCAL_SUPERVISORS[key] = supervisor
    return supervisor


def _build(
    project_path: str,
    *,
    target: str = "",
    output_dir: str = "",
    game_name: str = "",
    debug_mode: bool | None = None,
    lto: bool | None = None,
    android_artifact: str = "",
    compress_resources: bool | None = None,
    persist_settings: bool = True,
) -> dict[str, object]:
    return EditorAutomationHost.instance().build_player(
        project_path,
        target=target,
        output_dir=output_dir,
        game_name=game_name,
        debug_mode=debug_mode,
        lto=lto,
        android_artifact=android_artifact,
        compress_resources=compress_resources,
        persist_settings=bool(persist_settings),
    )


def _configured_executable(project_path: str) -> str:
    settings_path = os.path.join(project_path, "ProjectSettings", "BuildSettings.json")
    try:
        with open(settings_path, "r", encoding="utf-8") as stream:
            settings = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationError("player.build_settings", f"Build Settings could not be read: {settings_path}") from exc
    output_dir = os.path.abspath(str(settings.get("output_dir", "") or ""))
    game_name = str(settings.get("game_name", "") or "").strip()
    if not output_dir or not game_name:
        raise OperationError("player.build_settings", "Build Settings must define output_dir and game_name.")
    return os.path.join(output_dir, game_name + (".exe" if os.name == "nt" else ""))


def _launch(project_path: str, executable_path: str, start_scene: str, timeout_seconds: float):
    executable = os.path.abspath(executable_path) if executable_path else _configured_executable(project_path)
    result = _supervisor().launch_player(executable, start_scene=start_scene, wait_for_ready=True, timeout_seconds=timeout_seconds)
    if not bool(result.get("player_ready")):
        raise OperationError("player.startup", str(result.get("ready_error", "Player did not become ready.")), details=result)
    return result


def _observe(object_names: list[str] | None = None, component_probes: list[dict] | None = None, include_scene_objects: bool = False, timeout_seconds: float = 3.0):
    return _supervisor().player_observe(
        object_names or [],
        component_probes=[dict(item) for item in component_probes or []],
        include_scene_objects=bool(include_scene_objects),
        timeout_seconds=timeout_seconds,
    )


def _key(key, pressed: bool, repeat: bool = False, timeout_seconds: float = 3.0):
    return _supervisor().player_send_key(key, bool(pressed), repeat=bool(repeat), timeout_seconds=timeout_seconds)


def _press_key(key, duration_seconds: float = 0.1, object_names: list[str] | None = None, component_probes: list[dict] | None = None, timeout_seconds: float = 3.0):
    return _supervisor().player_press_key(
        key,
        duration_seconds,
        object_names=object_names or [],
        component_probes=[dict(item) for item in component_probes or []],
        timeout_seconds=timeout_seconds,
    )


def _pointer_button(button: int, pressed: bool, x: float, y: float, timeout_seconds: float = 3.0):
    return _supervisor().player_send_mouse_button(
        button, bool(pressed), x, y, timeout_seconds=timeout_seconds
    )


def _motion_arm(
    object_names: list[str],
    seconds: float = 2.0,
    sample_interval: float = 0.1,
    trigger_scene_name: str = "",
    trigger_timeout: float = 60.0,
    hold_keys: list | None = None,
    hold_mouse_buttons: list[int] | None = None,
    component_probes: list[dict] | None = None,
    stop_assertions: list[dict] | None = None,
    timeout_seconds: float = 3.0,
):
    return _supervisor().player_motion_capture_arm(
        list(object_names or []),
        seconds=seconds,
        sample_interval=sample_interval,
        trigger_scene_name=trigger_scene_name,
        trigger_timeout=trigger_timeout,
        hold_keys=list(hold_keys or []),
        hold_mouse_buttons=list(hold_mouse_buttons or []),
        component_probes=[dict(item) for item in component_probes or []],
        stop_assertions=[dict(item) for item in stop_assertions or []],
        timeout_seconds=timeout_seconds,
    )


__all__ = ["build_player_operations"]
