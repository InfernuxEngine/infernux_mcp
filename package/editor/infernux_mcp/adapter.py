"""OperationSchema adapter and the small default MCP gateway surface."""

from __future__ import annotations

import json
import threading
import time
from functools import wraps
from typing import Any, Mapping
from infernux.host.commands import CommandOwner, command_owner_scope

from infernux.host import (
    OperationError,
    OperationJobRegistry,
    OperationKind,
    OperationRegistry,
    capability_granted,
)


OWNER = "infernux/mcp"
MAX_GATEWAY_TOOLS = 32

_registry: OperationRegistry | None = None
_jobs: OperationJobRegistry | None = None
_operation_ids: set[str] = set()
_gateway_names: set[str] = set()
_state_lock = threading.RLock()
_started_at = 0.0
_registration_ms = 0.0
_full_schema_bytes = 0
_compact_schema_bytes = 0
_config: dict[str, Any] = {}
_command_owner: CommandOwner | None = None


def register_gateways(
    mcp,
    project_path: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Register engine capabilities as operations and expose <=32 gateways."""

    global _registry, _jobs, _started_at, _config, _command_owner
    global _registration_ms, _full_schema_bytes, _compact_schema_bytes
    with _state_lock:
        registration_started = time.perf_counter()
        shutdown_adapter()
        _command_owner = CommandOwner(OWNER)
        _config = dict(config or {})
        registry = OperationRegistry.instance()
        registry.unregister_owner(OWNER)
        from infernux.host import install_editor_operations

        install_editor_operations(project_path, registry)
        from infernux_mcp import capabilities
        if config is None:
            _config = capabilities.configure(project_path, write_default=False)
        else:
            _config = capabilities.apply_config(project_path, dict(config))
        from infernux_mcp import session
        # Registration defines a new plugin lifetime and policy session.
        session.configure(project_path, dict(_config))
        from infernux_mcp.operations import build_operations

        _operation_ids.clear()
        for operation in build_operations(project_path):
            registry.register(operation)
            _operation_ids.add(operation.schema.id)
        _registry = registry
        _jobs = OperationJobRegistry(registry)
        _started_at = time.time()
        documents = registry.list()
        _full_schema_bytes = len(
            json.dumps(
                documents,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        _compact_schema_bytes = len(
            json.dumps(
                [_compact_schema(value) for value in documents],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        _register_gateway_tools(mcp, project_path, _command_owner)
        if len(_gateway_names) > MAX_GATEWAY_TOOLS:
            raise RuntimeError(
                f"Default MCP gateway exceeds {MAX_GATEWAY_TOOLS} tools: "
                f"{len(_gateway_names)}"
            )
        _registration_ms = (time.perf_counter() - registration_started) * 1000.0
        return adapter_status()


def begin_shutdown_adapter() -> None:
    """Reject this generation's queued commands before transport drain."""
    with _state_lock:
        if _command_owner is not None:
            _command_owner.close()


def shutdown_adapter() -> None:
    global _registry, _jobs, _started_at
    global _registration_ms, _full_schema_bytes, _compact_schema_bytes
    with _state_lock:
        begin_shutdown_adapter()
        if _jobs is not None:
            remaining = _jobs.shutdown(
                wait=True, cancel_futures=True, timeout=5.0
            )
            if remaining:
                raise RuntimeError(
                    f"{remaining} MCP operation job(s) did not stop within 5 seconds"
                )
        _jobs = None
        if _registry is not None:
            _registry.unregister_owner(OWNER)
        _registry = None
        _operation_ids.clear()
        _gateway_names.clear()
        _started_at = 0.0
        _registration_ms = 0.0
        _full_schema_bytes = 0
        _compact_schema_bytes = 0


def adapter_status() -> dict[str, object]:
    registry = _registry
    return {
        "owner": OWNER,
        "active": registry is not None,
        "revision": 0 if registry is None else registry.revision,
        "operation_count": 0 if registry is None else len(registry.list()),
        "owned_operation_count": len(_operation_ids),
        "gateway_count": len(_gateway_names),
        "gateway_tools": sorted(_gateway_names),
        "started_at": _started_at,
        "registration_ms": _registration_ms,
        "full_schema_bytes": _full_schema_bytes,
        "compact_schema_bytes": _compact_schema_bytes,
    }


def _register_gateway_tools(mcp, project_path: str, command_owner: CommandOwner) -> None:
    _gateway_names.clear()

    def gateway(name: str):
        _gateway_names.add(name)
        def decorate(fn):
            @wraps(fn)
            def invoke(*args, **kwargs):
                with command_owner_scope(command_owner):
                    return fn(*args, **kwargs)
            return mcp.tool(name=name)(invoke)
        return decorate

    @gateway("mcp_ping")
    def mcp_ping() -> dict[str, object]:
        """Return transport and operation-registry readiness."""

        return _ok({"message": "pong", **adapter_status()})

    @gateway("operation_schema_list")
    def operation_schema_list(
        kind: str = "", capability: str = "", offset: int = 0, limit: int = 200
    ) -> dict[str, object]:
        """List compact OperationSchema records with deterministic pagination."""

        try:
            values = _require_registry().list(kind=kind or None, capability=capability)
            start = max(int(offset), 0)
            stop = start + min(max(int(limit), 1), 200)
            return _ok(
                {
                    "revision": _require_registry().revision,
                    "total": len(values),
                    "offset": start,
                    "operations": [_compact_schema(value) for value in values[start:stop]],
                }
            )
        except (ValueError, OperationError) as exc:
            return _error(exc)

    @gateway("operation_schema_get")
    def operation_schema_get(operation: str) -> dict[str, object]:
        """Return one complete OperationSchema document."""

        try:
            return _ok(_require_registry().get(operation).schema.document())
        except OperationError as exc:
            return _error(exc)

    @gateway("operation_schema_search")
    def operation_schema_search(query: str, limit: int = 50) -> dict[str, object]:
        """Search operation IDs, summaries and domain tags."""

        try:
            values = _require_registry().search(query, limit=min(max(int(limit), 1), 200))
            return _ok(
                {
                    "revision": _require_registry().revision,
                    "operations": [_compact_schema(value) for value in values],
                }
            )
        except OperationError as exc:
            return _error(exc)

    @gateway("operation_query_execute")
    def operation_query_execute(
        operation: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, object]:
        """Execute one query operation after schema and capability validation."""

        return _execute(operation, arguments, expected_kind=OperationKind.QUERY)

    @gateway("operation_command_execute")
    def operation_command_execute(
        operation: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, object]:
        """Execute one command operation after schema and capability validation."""

        return _execute(operation, arguments, expected_kind=OperationKind.COMMAND)

    @gateway("operation_workflow_invoke")
    def operation_workflow_invoke(
        operation: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, object]:
        """Invoke one compound workflow operation."""

        return _execute(operation, arguments, expected_kind=OperationKind.WORKFLOW)

    @gateway("operation_execute")
    def operation_execute(
        operation: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, object]:
        """Execute an operation when its kind is already known from discovery."""

        return _execute(operation, arguments)

    @gateway("operation_batch_execute")
    def operation_batch_execute(
        calls: list[dict[str, Any]], stop_on_error: bool = True
    ) -> dict[str, object]:
        """Execute a bounded ordered batch of schema operations."""

        try:
            maximum = int((_config.get("limits") or {}).get("batch_max_steps", 100))
            if len(calls) > maximum:
                raise OperationError(
                    "operation.batch_too_large", f"Batch exceeds {maximum} calls"
                )
            normalized = [
                {
                    **dict(call),
                    "operation": str(call.get("operation", "")),
                }
                for call in calls
            ]
            return _ok(
                {
                    "results": list(
                        _require_registry().execute_batch(
                            normalized,
                            capabilities=_granted_capabilities(),
                            stop_on_error=bool(stop_on_error),
                        )
                    )
                }
            )
        except OperationError as exc:
            return _error(exc)

    @gateway("operation_job_submit")
    def operation_job_submit(
        operation: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, object]:
        """Submit a potentially long operation to the bounded Host job pool."""

        try:
            job_id = _require_jobs().submit(
                operation,
                arguments,
                capabilities=_granted_capabilities(),
            )
            return _ok({"job_id": job_id})
        except OperationError as exc:
            return _error(exc)

    @gateway("operation_job_status")
    def operation_job_status(job_id: str) -> dict[str, object]:
        """Inspect one asynchronous operation job."""

        try:
            return _ok(_require_jobs().status(job_id))
        except OperationError as exc:
            return _error(exc)

    @gateway("operation_job_cancel")
    def operation_job_cancel(job_id: str) -> dict[str, object]:
        """Cancel a queued operation job when it has not begun execution."""

        try:
            return _ok({"job_id": job_id, "cancelled": _require_jobs().cancel(job_id)})
        except OperationError as exc:
            return _error(exc)

    @gateway("host_capabilities")
    def host_capabilities() -> dict[str, object]:
        """Describe granted capabilities, per-capability grant status, and the
        machine-readable list of operations blocked by missing grants."""

        try:
            registry = _require_registry()
            granted = _granted_capabilities()
            documents = registry.list()
            rows: dict[str, dict[str, object]] = {}
            blocked: list[dict[str, object]] = []
            for document in documents:
                required = tuple(str(item) for item in document.get("capabilities", ()))
                for name in required:
                    row = rows.setdefault(
                        name,
                        {
                            "capability": name,
                            "granted": capability_granted(name, granted),
                            "operations": 0,
                        },
                    )
                    row["operations"] = int(row["operations"]) + 1
                missing = [name for name in required if not capability_granted(name, granted)]
                if missing:
                    blocked.append({"operation": document["id"], "missing": missing})
            from infernux_mcp import capabilities as capability_config

            return _ok(
                {
                    "granted": list(granted),
                    "capabilities": sorted(rows.values(), key=lambda row: str(row["capability"])),
                    "blocked_operations": blocked,
                    "blocked_operation_count": len(blocked),
                    "config_path": capability_config.config_path(),
                    "operation_revision": registry.revision,
                    "operation_count": len(documents),
                    "owned_operation_count": len(_operation_ids),
                    "gateway_count": len(_gateway_names),
                }
            )
        except OperationError as exc:
            return _error(exc)

    @gateway("host_session_status")
    def host_session_status() -> dict[str, object]:
        """Return project/session state without advertising session internals."""

        try:
            from infernux_mcp import session

            value = session.status()
        except Exception as exc:
            value = {"project_path": project_path, "error": f"{type(exc).__name__}: {exc}"}
        maximum = int((_config.get("limits") or {}).get("batch_max_steps", 100))
        return _ok({
            "session": value,
            **adapter_status(),
            "workflow_guidance": {
                "schema_discovery": (
                    "Call operation_schema_list once with limit=200, then cache the compact "
                    "schemas until the reported revision changes."
                ),
                "batch_execution": (
                    "Use operation_batch_execute for ordered groups of known operations instead "
                    "of making one gateway round trip per operation."
                ),
                "visual_capture": (
                    "Use Scene, Game, or Player render-target capture for visual validation."
                ),
                "schema_page_size": 200,
                "batch_max_steps": maximum,
            },
        })


def _compact_schema(value: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value[key]
        for key in (
            "id", "version", "kind", "summary", "thread", "side_effects",
            "reversible", "capabilities", "cost", "tags", "availability", "phase",
        )
        if key in value
    }


def _granted_capabilities() -> tuple[str, ...]:
    configured = _config.get("granted_capabilities", ["*"])
    if not isinstance(configured, (list, tuple)):
        return ("*",)
    return tuple(str(item) for item in configured)


def _execute(
    operation: str,
    arguments: Mapping[str, object] | None,
    *,
    expected_kind: OperationKind | None = None,
) -> dict[str, object]:
    started = time.perf_counter()
    try:
        value = _require_registry().execute(
            operation,
            arguments,
            capabilities=_granted_capabilities(),
            expected_kind=expected_kind,
        )
        from infernux_mcp.trace import record_operation

        record_operation(
            operation,
            ok=True,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            arguments=dict(arguments or {}),
            result=value,
        )
        return _ok({"operation": operation, "result": value})
    except OperationError as exc:
        from infernux_mcp.trace import record_operation

        record_operation(
            operation,
            ok=False,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            arguments=dict(arguments or {}),
            error=str(exc),
        )
        if exc.code == "operation.permission_denied":
            return _error(_with_grant_remediation(exc))
        return _error(exc)


def _with_grant_remediation(exc: OperationError) -> OperationError:
    """Attach a machine-readable grant remediation to a permission failure."""

    from infernux_mcp import capabilities as capability_config

    details = dict(exc.details) if isinstance(exc.details, Mapping) else {}
    details.setdefault("granted", list(_granted_capabilities()))
    details["grant_remediation"] = {
        "config_path": capability_config.config_path(),
        "config_pointer": "/granted_capabilities",
        "hint": (
            "Append the required capabilities to granted_capabilities in the "
            "project mcp_capabilities.json, then re-register the MCP adapter. "
            "Grants accept fnmatch patterns such as 'scene.*' or '*.read'."
        ),
    }
    return OperationError(exc.code, str(exc), details=details)


def _require_registry() -> OperationRegistry:
    if _registry is None:
        raise OperationError("adapter.not_running", "MCP operation adapter is not running")
    return _registry


def _require_jobs() -> OperationJobRegistry:
    if _jobs is None:
        raise OperationError("adapter.not_running", "MCP operation job service is not running")
    return _jobs


def _ok(data: object) -> dict[str, object]:
    return {"ok": True, "data": data}


def _error(exc: Exception) -> dict[str, object]:
    if isinstance(exc, OperationError):
        return exc.envelope()
    return OperationError("adapter.failed", f"{type(exc).__name__}: {exc}").envelope()


__all__ = [
    "MAX_GATEWAY_TOOLS",
    "OWNER",
    "adapter_status",
    "register_gateways",
    "shutdown_adapter",
]
