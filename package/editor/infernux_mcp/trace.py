"""Minimal trace recorder for MCP operation calls."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
import threading
import time
import uuid
from typing import Any

from infernux.engine.path_utils import relative_path, resolved_path
from infernux.host.operations import OperationError
from infernux_mcp.capabilities import feature_enabled

_active_trace: dict[str, Any] | None = None
_last_trace: dict[str, Any] | None = None
_session_project_path = ""
_session_log_path = ""
_lock = threading.RLock()
_pending: set[OperationTrace] = set()
_CONTROL_OPERATIONS = frozenset({"infernux.mcp.attempt.start", "infernux.mcp.attempt.stop"})


@dataclass(eq=False, slots=True)
class OperationTrace:
    operation: str
    arguments: dict[str, Any]
    trace: dict[str, Any] | None
    step: dict[str, Any] | None
    session_path: str
    started: float
    control: bool = False
    finished: bool = False


def begin_operation(operation: str, *, arguments: dict[str, Any]) -> OperationTrace:
    """Reserve the originating attempt before a call can enter a worker queue."""
    with _lock:
        control = operation in _CONTROL_OPERATIONS
        target = None if control else _active_trace
        step = None
        if target is not None and feature_enabled("trace_recorder"):
            step = {"index": len(target["steps"]), "operation": str(operation), "status": "pending"}
            target["steps"].append(step)
        ticket = OperationTrace(
            str(operation), deepcopy(arguments), target, step,
            _session_log_path if _session_log_enabled() else "", time.perf_counter(), control,
        )
        if target is not None:
            _pending.add(ticket)
        return ticket


def _secret_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            if any(marker in str(key).casefold() for marker in ("token", "secret", "password", "lease")):
                if isinstance(item, str) and item:
                    result.append(item)
            else:
                result.extend(_secret_values(item))
        return result
    if isinstance(value, (list, tuple)):
        return [secret for item in value for secret in _secret_values(item)]
    return []


def finish_operation(ticket: OperationTrace, *, ok: bool, result: Any = None,
                     error: str = "", elapsed_ms: float | None = None, cancelled: bool = False) -> None:
    with _lock:
        if ticket.finished:
            return
        ticket.finished = True
        _pending.discard(ticket)
        if elapsed_ms is None:
            elapsed_ms = (time.perf_counter() - ticket.started) * 1000.0
        for secret in sorted(_secret_values(ticket.arguments), key=len, reverse=True):
            error = error.replace(secret, "<redacted>")
        step = ticket.step
        # Start creates the attempt during execution; Stop saves it before return.
        # These control operations must not count as outstanding work themselves.
        if ticket.control and _active_trace is not None and feature_enabled("trace_recorder"):
            step = {"index": len(_active_trace["steps"]), "operation": ticket.operation}
            _active_trace["steps"].append(step)
        if step is not None:
            step.update(ok=bool(ok), elapsed_ms=round(float(elapsed_ms), 3))
            step.pop("status", None)
            if cancelled:
                step["status"] = "cancelled"
            if ticket.arguments:
                step["arguments"] = _jsonable_summary(ticket.arguments)
            if result is not None:
                step["result"] = _jsonable_summary(result, max_string=_trace_result_max_string(), limit_name="trace_result_max_string")
            if error:
                step["error"] = str(error)
        if ticket.session_path:
            _record_session_operation(ticket.operation, ok=ok, elapsed_ms=elapsed_ms,
                arguments=ticket.arguments, result=result, error=error, path=ticket.session_path,
                status="cancelled" if cancelled else "")
def set_session_project_path(project_path: str) -> dict[str, Any]:
    """Bind trace output to a project without creating a log file yet."""
    global _session_project_path, _session_log_path
    _session_project_path = resolved_path(project_path or "") if project_path else ""
    _session_log_path = _session_log_file(_session_project_path)
    return session_log_info(_session_project_path)


def start_session_log(project_path: str) -> dict[str, Any]:
    """Clear and initialize the per-editor-session MCP call log."""
    set_session_project_path(project_path)
    if not _session_log_path:
        return session_log_info(project_path)
    os.makedirs(os.path.dirname(_session_log_path), exist_ok=True)
    with open(_session_log_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({
            "event": "session_start",
            "time": time.time(),
            "project_path": _session_project_path,
        }, ensure_ascii=False) + "\n")
    return session_log_info()


def session_log_info(project_path: str | None = None) -> dict[str, Any]:
    path = _session_log_path or _session_log_file(project_path or _session_project_path)
    exists = bool(path and os.path.isfile(path))
    return {
        "enabled": _session_log_enabled(),
        "path": _rel(project_path or _session_project_path, path) if path else "",
        "absolute_path": path,
        "exists": exists,
        "size": os.path.getsize(path) if exists else 0,
    }


def clear_session_log(project_path: str | None = None) -> dict[str, Any]:
    return start_session_log(project_path or _session_project_path)


def read_session_log(project_path: str | None = None, limit: int = 200) -> dict[str, Any]:
    path = _session_log_path or _session_log_file(project_path or _session_project_path)
    if not path or not os.path.isfile(path):
        return {"entries": [], **session_log_info(project_path)}
    entries = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"event": "raw", "text": line})
    return {"entries": entries[-max(int(limit), 1):], **session_log_info(project_path)}


def start_trace(
    project_path: str,
    task: str = "",
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a trace, optionally attaching immutable attempt/session context."""
    global _active_trace
    with _lock:
        if _active_trace is not None:
            raise OperationError("trace.already_active", "Stop the active trace before starting another")
        _active_trace = {
            "trace_id": f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
            "task": str(task or ""),
            "started_at": time.time(),
            "steps": [],
        }
        if context:
            _active_trace["context"] = _jsonable_summary(context)
        return current_trace()


def stop_trace(project_path: str, save: bool = True) -> dict[str, Any]:
    global _active_trace, _last_trace
    with _lock:
        if _active_trace is None:
            return {"active": False, "trace": None, "saved_path": ""}
        pending = sum(ticket.trace is _active_trace for ticket in _pending)
        if pending:
            raise OperationError("trace.pending_operations",
                f"Wait for or cancel {pending} outstanding operation(s) before stopping this trace",
                details={"pending_operations": pending})
        trace = deepcopy(_active_trace)
        trace["ended_at"] = time.time()
        trace["elapsed_seconds"] = max(0.0, trace["ended_at"] - float(trace.get("started_at", trace["ended_at"])))
        saved_path = _save_trace(project_path, trace) if save else ""
        _last_trace = trace
        _active_trace = None
        return {"active": False, "trace": deepcopy(trace), "saved_path": saved_path}


def current_trace() -> dict[str, Any]:
    with _lock:
        return {"active": _active_trace is not None, "trace": deepcopy(_active_trace)}


def last_trace() -> dict[str, Any]:
    with _lock:
        return {"trace": deepcopy(_last_trace)}


def record_operation(
    operation: str,
    *,
    ok: bool,
    elapsed_ms: float = 0.0,
    arguments: dict[str, Any] | None = None,
    result: Any = None,
    error: str = "",
) -> None:
    ticket = begin_operation(operation, arguments=dict(arguments or {}))
    finish_operation(ticket, ok=ok, elapsed_ms=elapsed_ms, result=result, error=error)


def list_traces(project_path: str, limit: int = 50) -> list[dict[str, Any]]:
    trace_dir = _trace_dir(project_path)
    if not os.path.isdir(trace_dir):
        return []
    entries = []
    for name in sorted(os.listdir(trace_dir), reverse=True):
        if not name.endswith(".json"):
            continue
        path = os.path.join(trace_dir, name)
        entries.append({
            "file": relative_path(path, project_path),
            "name": name,
            "size": os.path.getsize(path),
        })
        if len(entries) >= max(int(limit), 1):
            break
    return entries


def _save_trace(project_path: str, trace: dict[str, Any]) -> str:
    trace_dir = _trace_dir(project_path)
    os.makedirs(trace_dir, exist_ok=True)
    file_path = os.path.join(trace_dir, f"{trace['trace_id']}.json")
    with open(file_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(trace, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return relative_path(file_path, project_path)


def _trace_dir(project_path: str) -> str:
    return os.path.join(resolved_path(project_path), ".infernux", "mcp_traces")


def _session_log_file(project_path: str) -> str:
    root = str(project_path or "").strip()
    if not root:
        return ""
    return os.path.join(resolved_path(root), "Logs", "mcp_session.jsonl")


def _record_session_operation(
    operation: str,
    *,
    ok: bool,
    elapsed_ms: float = 0.0,
    arguments: dict[str, Any] | None = None,
    result: Any = None,
    error: str = "",
    path: str = "",
    status: str = "",
) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        entry = {
            "event": "operation_call",
            "time": time.time(),
            "operation": str(operation),
            "ok": bool(ok),
            "elapsed_ms": round(float(elapsed_ms), 3),
        }
        if arguments:
            entry["arguments"] = _jsonable_summary(arguments)
        if result is not None:
            entry["result"] = _jsonable_summary(
                result,
                max_string=_session_result_max_string(),
                limit_name="session_log_result_max_string",
            )
        if error:
            entry["error"] = str(error)
        if status:
            entry["status"] = status
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _session_log_enabled() -> bool:
    try:
        from infernux_mcp.capabilities import feature_enabled
        return feature_enabled("session_call_log")
    except Exception:
        return True


def _session_result_max_string() -> int:
    try:
        from infernux_mcp.capabilities import limit
        return int(limit("session_log_result_max_string", 480) or 480)
    except Exception:
        return 480


def _trace_result_max_string() -> int:
    try:
        from infernux_mcp.capabilities import limit
        return int(limit("trace_result_max_string", 480) or 480)
    except Exception:
        return 480


def _rel(project_path: str, path: str) -> str:
    if not project_path or not path:
        return path
    try:
        return relative_path(path, project_path, allow_root=True)
    except Exception:
        return path


def _jsonable_summary(
    value: Any,
    *,
    max_string: int = 240,
    limit_name: str = "trace_argument_max_string",
) -> Any:
    if limit_name:
        try:
            from infernux_mcp.capabilities import limit
            max_string = int(limit(limit_name, max_string) or max_string)
        except Exception:
            pass
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + "...<truncated>"
    if isinstance(value, dict):
        return {
            str(k): (
                "<redacted>"
                if any(marker in str(k).casefold() for marker in ("token", "secret", "password", "lease"))
                else _jsonable_summary(v, max_string=max_string, limit_name=limit_name)
            )
            for k, v in list(value.items())[:40]
        }
    if isinstance(value, (list, tuple)):
        items = list(value)
        summarized = [
            _jsonable_summary(v, max_string=max_string, limit_name=limit_name)
            for v in items[:40]
        ]
        if len(items) > 40:
            summarized.append(f"...<{len(items) - 40} more>")
        return summarized
    return str(value)

