"""MCP session state, traces and optional reproducible validation workflows."""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any

from infernux.engine.path_utils import is_path_within, portable_path, relative_path, resolved_path
from infernux_mcp import checkpoints as checkpoint_store
from infernux_mcp.session_identity import capture_build_identity


VALID_BUILD_PROFILES = frozenset({"debug_feedback", "release_exploration"})
VALID_BLOCKER_CATEGORIES = frozenset({
    "project_bug",
    "editor_ui_bug",
    "engine_bug",
    "mcp_bug",
    "public_api_gap",
    "nonportable_workaround",
    "inconclusive",
})

# This is also the advertised JSON Schema pattern. IDs are portable filenames,
# including on Windows; the negative end assertion excludes a trailing newline.
BLOCKER_REPORT_ID_PATTERN = (
    r"^(?!(?:[Cc][Oo][Nn]|[Pp][Rr][Nn]|[Aa][Uu][Xx]|[Nn][Uu][Ll]|"
    r"[Cc][Oo][Mm][1-9]|[Ll][Pp][Tt][1-9])(?![\s\S]))"
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}(?![\s\S])"
)
_BLOCKER_REPORT_ID = re.compile(BLOCKER_REPORT_ID_PATTERN)

_CURRENT: "McpSession | None" = None


class McpSessionError(RuntimeError):
    """Raised when session data or a requested workflow is invalid."""


@dataclass
class McpSession:
    session_id: str
    project_root: str
    build_profile: str
    recording_enabled: bool
    artifact_root: str
    editor_instance_id: str = ""
    build_identity: dict[str, Any] = field(default_factory=dict)
    managed_checkpoints_required: bool = False
    attempt_id: str = ""
    attempt_active: bool = False
    checkpoint: str = ""
    task: str = ""
    attempt_manifest_path: str = ""
    persistence_proof_path: str = ""
    attempt_baseline_ledger: dict[str, Any] = field(default_factory=dict, repr=False)
    last_trace_id: str = ""
    last_trace_path: str = ""
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("attempt_baseline_ledger", None)
        value["recording_available"] = self.build_profile == "debug_feedback"
        value["recording_enabled"] = bool(self.recording_enabled and value["recording_available"])
        return value


def configure(project_path: str, config: dict[str, Any] | None = None) -> McpSession:
    """Create the active in-process session from the project MCP config."""
    global _CURRENT
    config = config or {}
    root = resolved_path(project_path or ".")
    options = config.get("session") or {}
    options = options if isinstance(options, dict) else {}
    build_profile = str(options.get("build_profile", "debug_feedback") or "debug_feedback")
    if build_profile not in VALID_BUILD_PROFILES:
        build_profile = "debug_feedback"
    session_id = checkpoint_store.normalize_checkpoint_id(options.get("session_id") or _new_session_id())
    artifact_root = os.path.join(root, ".infernux", "mcp_sessions", session_id)
    build_identity = _capture_build_identity(options, build_profile)
    editor_instance_id = str(os.environ.get("INFERNUX_MCP_EDITOR_INSTANCE_ID", "") or "").strip() or uuid.uuid4().hex
    _CURRENT = McpSession(
        session_id=session_id,
        project_root=root,
        build_profile=build_profile,
        recording_enabled=bool(options.get("recording_enabled", False)) and build_profile == "debug_feedback",
        artifact_root=artifact_root,
        editor_instance_id=editor_instance_id,
        build_identity=build_identity,
        managed_checkpoints_required=bool(options.get("managed_checkpoints_required", False)),
    )
    try:
        from infernux_mcp.trace import set_session_project_path

        set_session_project_path(root)
    except Exception:
        pass
    return _CURRENT


def current() -> McpSession:
    if _CURRENT is None:
        raise McpSessionError("MCP session is not configured yet.")
    return _CURRENT


def status() -> dict[str, Any]:
    return current().to_dict()


def checkpoint_status(checkpoint: str) -> dict[str, Any]:
    active = current()
    return checkpoint_store.checkpoint_status(
        active.project_root,
        active.artifact_root,
        checkpoint,
        session_id=active.session_id,
    )


def list_checkpoints() -> list[dict[str, Any]]:
    active = current()
    return checkpoint_store.list_checkpoints(active.project_root, active.artifact_root, session_id=active.session_id)


def validate_script(source: str, *, filename: str = "<script>") -> dict[str, Any]:
    """Validate Python syntax without executing imports or project code."""
    violations: list[dict[str, Any]] = []
    try:
        tree = ast.parse(str(source or ""), filename=filename)
        compile(tree, filename, "exec")
    except SyntaxError as exc:
        violations.append({
            "code": "syntax_error",
            "line": int(exc.lineno or 0),
            "message": str(exc.msg or "Invalid Python syntax."),
        })
        return _lint_result(violations)

    return _lint_result(violations)


def read_project_script(relative_path: str) -> dict[str, Any]:
    session = current()
    target = _script_path(session, relative_path)
    if not os.path.isfile(target):
        raise FileNotFoundError(f"Project script was not found: {relative_path}")
    with open(target, "r", encoding="utf-8") as f:
        return {"path": _relative_to_project(session, target), "content": f.read()}


def prepare_project_script_write(relative_path: str, content: str) -> dict[str, Any]:
    """Validate one developer-authored script without mutating the project."""
    session = current()
    target = _script_path(session, relative_path)
    lint = validate_script(content, filename=target)
    if not lint["passed"]:
        raise McpSessionError("Script syntax validation failed.")
    return {
        "absolute_path": target,
        "path": _relative_to_project(session, target),
        "lint": lint,
    }


def read_release_wheel_source(wheel_path: str, member: str) -> dict[str, Any]:
    """Read a text member from a wheel and record its origin."""
    session = current()
    target = resolved_path(wheel_path)
    if not target.lower().endswith(".whl") or not os.path.isfile(target):
        raise FileNotFoundError("Wheel file was not found.")
    member = portable_path(str(member or "")).lstrip("/")
    if not member.endswith((".py", ".pyi", ".txt", ".json")):
        raise McpSessionError("Only text source members may be read from a wheel.")
    with zipfile.ZipFile(target) as archive:
        try:
            raw = archive.read(member)
        except KeyError as exc:
            raise FileNotFoundError(f"Wheel member was not found: {member}") from exc
    text = raw.decode("utf-8", errors="replace")
    if len(text) > 65536:
        text = text[:65536] + "\n# ...<truncated>"
    audit = {
        "time": time.time(),
        "wheel": target,
        "member": member,
        "session_id": session.session_id,
    }
    _append_jsonl(os.path.join(session.artifact_root, "wheel-audit.jsonl"), audit)
    return {"wheel": target, "member": member, "content": text, "audit": audit}


def write_blocker(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a reproducible blocker report for the Repair Agent."""
    session = current()
    data = copy.deepcopy(payload if isinstance(payload, dict) else {})
    category = str(data.get("category", "") or "")
    if category not in VALID_BLOCKER_CATEGORIES:
        allowed = ", ".join(sorted(VALID_BLOCKER_CATEGORIES))
        raise McpSessionError(
            f"Unsupported blocker category: {category or '<empty>'}. Allowed categories: {allowed}."
        )
    for key in ("title", "expected", "actual"):
        if not str(data.get(key, "") or "").strip():
            raise McpSessionError(f"Blocker report requires non-empty '{key}'.")
    workflow = data.get("normal_workflow")
    if not isinstance(workflow, list) or not workflow:
        raise McpSessionError("Blocker report requires a non-empty normal_workflow list.")
    if not session.attempt_id or not session.checkpoint:
        raise McpSessionError("Blocker reports require a started attempt with a checkpoint.")
    if session.attempt_active:
        raise McpSessionError("Stop and save the current attempt trace before reporting a blocker.")
    if not session.last_trace_id:
        raise McpSessionError("Stop and save the current attempt trace before reporting a blocker.")
    if not isinstance(data.get("logic_evidence"), dict) or not data["logic_evidence"]:
        raise McpSessionError("Blocker report requires non-empty logic_evidence.")
    if not str(data.get("persistence_proof", "") or "").strip():
        raise McpSessionError("Blocker report requires persistence_proof.")
    data.update({
        "session_id": session.session_id,
        "project_root": session.project_root,
        "build_profile": session.build_profile,
        "recording_enabled": bool(session.recording_enabled),
        "attempt_id": session.attempt_id,
        "checkpoint": session.checkpoint,
        "task": session.task,
        "attempt_manifest_path": session.attempt_manifest_path,
        "reproducer_trace": session.last_trace_path,
        "trace_id": session.last_trace_id,
        "reported_at": time.time(),
    })
    report_id = data["report_id"] if "report_id" in data else f"blocker-{uuid.uuid4().hex}"
    if not isinstance(report_id, str) or not _BLOCKER_REPORT_ID.fullmatch(report_id):
        raise McpSessionError(
            "report_id must be 1-96 ASCII letters, digits, '_' or '-', start with a letter "
            "or digit, and must not be a Windows device name. Omit it to generate an ID."
        )
    data["report_id"] = report_id
    report_dir = os.path.join(session.artifact_root, "reports")
    path = os.path.join(report_dir, f"{report_id}.json")
    if not is_path_within(report_dir, session.artifact_root, allow_root=False) or not is_path_within(
        path, report_dir, allow_root=False
    ):
        raise McpSessionError("Blocker report path must stay inside the session reports directory.")
    # Serialize before creating a file, then reserve this ID exclusively. Existing
    # reports (including file links) are never truncated or used as write targets.
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    os.makedirs(report_dir, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except FileExistsError as exc:
        raise McpSessionError(f"Blocker report_id {report_id!r} already exists.") from exc
    return {"report_id": report_id, "path": path, "report": data}


def blocker_report_contract() -> dict[str, Any]:
    """Describe the required trace and evidence for a Repair Agent blocker report."""
    return {
        "operation": "infernux.mcp.blocker.report",
        "allowed_categories": sorted(VALID_BLOCKER_CATEGORIES),
        "required_arguments": {
            "category": "One allowed category describing the owning layer.",
            "title": "Short, concrete failure title.",
            "expected": "What a normal editor or game workflow should do.",
            "actual": "What the same workflow did instead.",
            "normal_workflow": [
                "Ordered human-equivalent UI/input steps that reproduce the failure."
            ],
            "logic_evidence": {
                "example": "Observed values, semantic targets, console output, or trace facts."
            },
            "persistence_proof": "State whether the result repeats after returning to the checkpoint.",
        },
        "optional_arguments": {
            "report_id": (
                "Unique portable identifier: 1-96 ASCII letters, digits, '_' or '-', starting "
                "with a letter or digit; Windows device names are forbidden. Omit to generate "
                "an ID. Existing reports cannot be overwritten."
            ),
            "severity": "low, medium, high, or critical; defaults to medium.",
            "notes": "Concise additional triage context.",
        },
        "required_sequence": [
            "Command infernux.mcp.attempt.start before changing the editor or game.",
            "Perform only the normal human-equivalent UI/input workflow; after an effectful click, wait for its rendered result before judging it.",
            "Command infernux.mcp.attempt.stop after the observation so the trace is saved.",
            "Command infernux.mcp.blocker.report with all required arguments only when a reproducible blocker remains.",
        ],
        "post_action_observation_rule": (
            "Input delivery proves only that an event reached the editor. Observe the expected "
            "rendered or object-graph state through an available formal operation before treating "
            "the action as evidence."
        ),
    }


def start_attempt(task: str, checkpoint: str) -> dict[str, Any]:
    """Start one replayable project attempt from an explicit checkpoint."""
    active = current()
    if active.attempt_active:
        raise McpSessionError("A project attempt is already active. Stop it before starting another attempt.")
    checkpoint = str(checkpoint or "").strip()
    if not checkpoint:
        if active.managed_checkpoints_required:
            raise McpSessionError(
                "Managed project attempts require a checkpoint from "
                "infernux.mcp.checkpoint.list."
            )
        checkpoint = "session-start"
    checkpoint_proof: dict[str, Any] = {
        "managed": False,
        "checkpoint_id": checkpoint,
        "current_match": None,
    }
    baseline_ledger: dict[str, Any] = {}
    if active.managed_checkpoints_required:
        try:
            managed = checkpoint_store.load_checkpoint(
                active.project_root,
                active.artifact_root,
                checkpoint,
                session_id=active.session_id,
                verify_payload=True,
            )
            baseline_ledger = copy.deepcopy(managed["ledger"])
            current_ledger = checkpoint_store.capture_project_ledger(active.project_root)
            delta = checkpoint_store.diff_ledgers(baseline_ledger, current_ledger)
        except (OSError, ValueError, checkpoint_store.CheckpointError) as exc:
            raise McpSessionError(f"Managed checkpoint validation failed: {exc}") from exc
        compact = checkpoint_store.compact_delta(delta)
        if any(compact[f"{kind}_count"] for kind in ("added", "modified", "deleted")):
            raise McpSessionError(
                "Current project does not match the managed checkpoint. "
                "Ask the external Supervisor to restore it or create a new clean checkpoint. "
                f"Delta: {json.dumps(compact, ensure_ascii=False)}"
            )
        checkpoint_proof = {
            "managed": True,
            "checkpoint_id": checkpoint,
            "current_match": True,
            "ledger_digest": baseline_ledger["digest"],
            "file_count": baseline_ledger["file_count"],
            "total_bytes": baseline_ledger["total_bytes"],
        }
    from infernux_mcp.trace import start_trace

    active.attempt_id = f"attempt-{uuid.uuid4().hex[:8]}"
    active.attempt_active = True
    active.checkpoint = checkpoint
    active.task = str(task or "")
    active.attempt_manifest_path = ""
    active.persistence_proof_path = ""
    active.attempt_baseline_ledger = baseline_ledger
    active.last_trace_id = ""
    active.last_trace_path = ""
    trace_context = {
        "attempt_id": active.attempt_id,
        "checkpoint": active.checkpoint,
        "session_id": active.session_id,
        "build_profile": active.build_profile,
        "recording_enabled": active.recording_enabled,
        "build_identity": active.build_identity,
    }
    if checkpoint_proof["managed"]:
        trace_context["checkpoint_proof"] = checkpoint_proof
    trace_state = start_trace(active.project_root, task=active.task, context=trace_context)
    trace = trace_state.get("trace") or {}
    active.attempt_manifest_path = _write_attempt_manifest(active, trace)
    return {
        "attempt_id": active.attempt_id,
        "attempt_active": active.attempt_active,
        "checkpoint": active.checkpoint,
        "task": active.task,
        "trace_id": trace.get("trace_id", ""),
        "attempt_manifest_path": active.attempt_manifest_path,
        "checkpoint_proof": checkpoint_proof,
    }


def stop_attempt() -> dict[str, Any]:
    """Stop and save the active project trace before handoff or triage."""
    active = current()
    if not active.attempt_id:
        raise McpSessionError("No project attempt is active.")
    if not active.attempt_active:
        if not active.last_trace_id:
            raise McpSessionError("The project attempt is already stopped and has no saved trace.")
        response = {
            "attempt_id": active.attempt_id,
            "checkpoint": active.checkpoint,
            "trace_id": active.last_trace_id,
            "trace_path": active.last_trace_path,
            "attempt_manifest_path": active.attempt_manifest_path,
            "elapsed_seconds": 0.0,
            "already_stopped": True,
        }
        if active.persistence_proof_path:
            response["persistence_proof_path"] = active.persistence_proof_path
        return response
    from infernux_mcp.trace import stop_trace

    result = stop_trace(active.project_root, save=True)
    trace = result.get("trace") or {}
    trace_id = str(trace.get("trace_id", "") or "")
    if not trace_id:
        active.attempt_active = False
        raise McpSessionError("Project attempt did not produce a trace.")
    active.attempt_active = False
    active.last_trace_id = trace_id
    active.last_trace_path = str(result.get("saved_path", "") or "")
    active.persistence_proof_path = _write_persistence_proof(active, trace)
    active.attempt_manifest_path = _write_attempt_manifest(active, trace)
    response = {
        "attempt_id": active.attempt_id,
        "checkpoint": active.checkpoint,
        "trace_id": active.last_trace_id,
        "trace_path": active.last_trace_path,
        "attempt_manifest_path": active.attempt_manifest_path,
        "elapsed_seconds": trace.get("elapsed_seconds", 0.0),
        "already_stopped": False,
    }
    if active.persistence_proof_path:
        response["persistence_proof_path"] = active.persistence_proof_path
    return response


def _write_attempt_manifest(active: McpSession, trace: dict[str, Any]) -> str:
    trace_id = str(trace.get("trace_id", "") or "")
    trace_path = active.last_trace_path or _expected_trace_path(trace_id)
    payload = {
        "kind": "infernux.mcp.attempt_manifest",
        "generated_at": time.time(),
        "session": {
            "session_id": active.session_id,
            "project_root": active.project_root,
            "build_profile": active.build_profile,
            "recording_enabled": active.recording_enabled,
        },
        "attempt": {
            "attempt_id": active.attempt_id,
            "active": active.attempt_active,
            "checkpoint": active.checkpoint,
            "task": active.task,
            "trace_id": trace_id,
            "trace_path": trace_path,
            "persistence_proof_path": active.persistence_proof_path,
        },
        "build_identity": copy.deepcopy(active.build_identity),
    }
    attempt_dir = os.path.join(active.artifact_root, "attempts")
    os.makedirs(attempt_dir, exist_ok=True)
    path = os.path.join(attempt_dir, f"{active.attempt_id}-manifest.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return _relative_project_path(active.project_root, path)


def _write_persistence_proof(active: McpSession, trace: dict[str, Any]) -> str:
    if not active.managed_checkpoints_required or not active.attempt_baseline_ledger:
        return ""
    current_ledger = checkpoint_store.capture_project_ledger(active.project_root)
    delta = checkpoint_store.diff_ledgers(active.attempt_baseline_ledger, current_ledger)
    trace_id = str(trace.get("trace_id", "") or "")
    payload = {
        "kind": "infernux.mcp.attempt_persistence_proof",
        "generated_at": time.time(),
        "session_id": active.session_id,
        "attempt_id": active.attempt_id,
        "checkpoint_id": active.checkpoint,
        "checkpoint_ledger_digest": active.attempt_baseline_ledger.get("digest", ""),
        "result_ledger_digest": current_ledger["digest"],
        "trace_id": trace_id,
        "trace_path": active.last_trace_path or _expected_trace_path(trace_id),
        "delta": delta,
        "changed": any(
            int(delta.get(f"{kind}_count", 0))
            for kind in ("added", "modified", "deleted")
        ),
    }
    attempt_dir = os.path.join(active.artifact_root, "attempts")
    os.makedirs(attempt_dir, exist_ok=True)
    path = os.path.join(attempt_dir, f"{active.attempt_id}-persistence.json")
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return _relative_project_path(active.project_root, path)


_capture_build_identity = capture_build_identity

def _expected_trace_path(trace_id: str) -> str:
    return f".infernux/mcp_traces/{trace_id}.json" if trace_id else ""


def _relative_project_path(root: str, path: str) -> str:
    if not path:
        return ""
    try:
        return relative_path(path, root, allow_root=True) if root else path
    except ValueError:
        return path


def _new_session_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _is_within_any_root(path: str, roots: list[str]) -> bool:
    return any(is_path_within(path, root) for root in roots)


def _script_path(session: McpSession, relative_path: str) -> str:
    value = portable_path(str(relative_path or "")).lstrip("/")
    if value.startswith("Assets/"):
        value = value[len("Assets/"):]
    if not value or not value.endswith(".py"):
        raise McpSessionError("Project scripts must use a relative .py path under Assets/.")
    target = resolved_path(os.path.join(session.project_root, "Assets", value))
    assets_root = os.path.join(session.project_root, "Assets")
    if not _is_within_any_root(target, [assets_root]):
        raise McpSessionError("Project script path escapes Assets/.")
    return target


def _relative_to_project(session: McpSession, path: str) -> str:
    return relative_path(path, session.project_root)


def _append_jsonl(path: str, value: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")


def _lint_result(violations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"passed": not violations, "violations": violations}
