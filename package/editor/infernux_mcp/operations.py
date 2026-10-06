"""Native OperationSchema implementations owned by the MCP plugin."""

from __future__ import annotations

from collections.abc import Callable

from infernux.host import (
    EditorAutomationHost,
    Operation,
    OperationError,
    OperationKind,
)

from infernux_mcp import session
from infernux_mcp.camera_operations import build_camera_operations
from infernux_mcp.capture_operations import build_capture_operations
from infernux_mcp.console_operations import build_console_operations
from infernux_mcp.docs_operations import build_docs_operations
from infernux_mcp.input_operations import build_input_operations
from infernux_mcp.material_operations import build_material_operations
from infernux_mcp.operation_support import OWNER, on_editor, operation
from infernux_mcp.particle_operations import build_particle_operations
from infernux_mcp.player_operations import build_player_operations
from infernux_mcp.runtime_operations import build_runtime_operations
from infernux_mcp.ui_operations import build_ui_operations


def build_operations(project_path: str) -> tuple[Operation, ...]:
    """Build the explicit operation surface for this plugin lifetime."""

    session_operations = (
        operation(
            "infernux.project.info",
            OperationKind.QUERY,
            "Read the project, scene document state, and last scene-load outcome. After scene.open/reload is scheduled, check active_scene.last_load until loaded or failed; failures include the parser diagnostic.",
            _project_info_handler(project_path),
            capability="project.read",
            tags=("project", "scene", "status"),
        ),
        operation(
            "infernux.mcp.checkpoint.list",
            OperationKind.QUERY,
            "List payload-verified Supervisor checkpoints for this session.",
            lambda: {"checkpoints": session.list_checkpoints()},
            capability="session.read",
            tags=("session", "checkpoint"),
        ),
        operation(
            "infernux.mcp.checkpoint.status",
            OperationKind.QUERY,
            "Verify one Supervisor checkpoint against the current project ledger.",
            lambda checkpoint: session.checkpoint_status(checkpoint),
            capability="session.read",
            input_properties={"checkpoint": {"type": "string"}},
            required=("checkpoint",),
            tags=("session", "checkpoint", "verification"),
        ),
        operation(
            "infernux.mcp.supervisor.shutdown",
            OperationKind.COMMAND,
            "Request normal Editor shutdown using the private Supervisor lease.",
            _supervisor_shutdown,
            capability="session.write",
            input_properties={"lease_token": {"type": "string"}},
            required=("lease_token",),
            side_effects=("Requests the Editor's normal close lifecycle.",),
            tags=("session", "supervisor", "shutdown"),
        ),
        operation(
            "infernux.mcp.attempt.start",
            OperationKind.COMMAND,
            "Start a checkpoint-bound validation attempt and trace.",
            lambda task, checkpoint="": session.start_attempt(task, checkpoint),
            capability="session.write",
            input_properties={
                "task": {"type": "string"},
                "checkpoint": {"type": "string", "default": ""},
            },
            required=("task",),
            side_effects=("Starts a validation trace for the active project.",),
            tags=("session", "attempt", "trace"),
        ),
        operation(
            "infernux.mcp.attempt.stop",
            OperationKind.COMMAND,
            "Stop and save the active validation attempt trace.",
            session.stop_attempt,
            capability="session.write",
            side_effects=("Writes the active trace into the session artifact directory.",),
            tags=("session", "attempt", "trace"),
        ),
        operation(
            "infernux.mcp.blocker.report",
            OperationKind.COMMAND,
            "Persist a trace-backed validation blocker report.",
            lambda report: session.write_blocker(report),
            capability="session.write",
            input_properties={"report": {
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "pattern": session.BLOCKER_REPORT_ID_PATTERN,
                        "description": session.blocker_report_contract()["optional_arguments"]["report_id"],
                    },
                },
            }},
            required=("report",),
            side_effects=("Writes a blocker report into the session artifact directory.",),
            tags=("session", "validation", "blocker", "report"),
        ),
    )
    return (
        session_operations
        + build_material_operations()
        + build_particle_operations()
        + build_camera_operations()
        + build_runtime_operations()
        + build_input_operations()
        + build_ui_operations()
        + build_capture_operations()
        + build_console_operations()
        + build_docs_operations()
        + build_player_operations(project_path)
    )


def _project_info_handler(project_path: str) -> Callable[[], dict[str, object]]:
    def execute() -> dict[str, object]:
        return on_editor(
            "infernux.project.info",
            lambda: EditorAutomationHost.instance().project_info(project_path),
        )

    return execute


def _supervisor_shutdown(lease_token: str) -> dict[str, object]:
    try:
        active = session.require_supervisor_lease(lease_token)
    except session.McpPolicyError as exc:
        raise OperationError("mcp.supervisor_lease", str(exc)) from exc

    def request_close() -> dict[str, object]:
        EditorAutomationHost.instance().request_editor_close()
        return {
            "close_requested": True,
            "editor_instance_id": active.editor_instance_id,
        }

    return on_editor("infernux.mcp.supervisor.shutdown", request_close)


__all__ = ["OWNER", "build_operations"]
