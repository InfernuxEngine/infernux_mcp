"""Embedded HTTP MCP server for Infernux Editor."""

from __future__ import annotations

import json
import os
import threading
import time
import socket
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Optional

from infernux.debug import Debug
from infernux.engine.path_utils import resolved_path
from infernux.runtime_services import (
    get_runtime_service,
    install_runtime_service,
    remove_runtime_service,
)

HOST = "127.0.0.1"
PORT = 9713
PATH = "/mcp"
HEALTH_PATH = "/health"
SERVER_NAME = "Infernux Editor"
_RUNTIME_SERVICE_NAME = "infernux.editor.mcp.http"
_TRANSPORT_CONTRACT = 2


@dataclass
class _ServerState:
    project_path: str
    host: str
    port: int
    mcp: object
    transport: object
    adapter_shutdown: object
    transport_contract: int = _TRANSPORT_CONTRACT
    adapter_begin_shutdown: object = None
    thread: Optional[threading.Thread] = None
    error: BaseException | None = None
    stop_requested: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)
    reaper_started: threading.Event = field(default_factory=threading.Event)
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock)

    def matches(self, project_path: str, host: str, port: int) -> bool:
        return (
            resolved_path(project_path) == resolved_path(self.project_path)
            and str(host) == self.host
            and int(port) == self.port
        )

    def is_alive(self) -> bool:
        thread = self.thread
        return thread is not None and thread.is_alive()


_lifecycle_lock = threading.RLock()


def _active_state() -> _ServerState | None:
    state = get_runtime_service(_RUNTIME_SERVICE_NAME)
    # Do not use isinstance here. The service intentionally survives unloading
    # and re-importing this plugin module, so its class may belong to the prior
    # module generation.
    if state is None:
        return None
    required = (
        "project_path",
        "host",
        "port",
        "transport",
        "adapter_shutdown",
        "thread",
        "stop_requested",
        "stopped",
        "reaper_started",
        "cleanup_lock",
    )
    if not all(hasattr(state, name) for name in required):
        raise RuntimeError("Infernux MCP runtime service has an invalid owner")
    return state


def start_server(project_path: str, *, host: str = HOST, port: int = PORT) -> bool:
    """Start the embedded HTTP MCP server if it is not already running."""
    from infernux_mcp.supervisor_utils import _require_loopback_host, _require_port

    host = _require_loopback_host(host)
    port = _require_port(port)
    with _lifecycle_lock:
        existing = _active_state()
        contract = getattr(existing, "transport_contract", 0) if existing is not None else _TRANSPORT_CONTRACT
        if contract > _TRANSPORT_CONTRACT:
            raise RuntimeError("Infernux MCP transport owner has an unsupported version contract")
        if existing is not None and contract < _TRANSPORT_CONTRACT:
            # Owners from an older plugin generation survive module reload.
            # Retire that transport before publishing the current one.
            _stop_state(existing)
            existing = _active_state()
        if existing is not None:
            if existing.stop_requested.is_set():
                if not existing.stopped.wait(timeout=10.0):
                    raise RuntimeError(
                        "Previous Infernux MCP transport did not retire within 10 seconds"
                    )
                existing = _active_state()
                if existing is not None:
                    raise RuntimeError(
                        "Retired Infernux MCP transport still owns the runtime service"
                    )
        if existing is not None:
            if existing.is_alive() and existing.matches(
                project_path, host, int(port)
            ):
                return True
            if existing.is_alive():
                raise RuntimeError(
                    "Infernux MCP is already serving a different project or endpoint"
                )
            # A terminated owner must be fully removed before another transport
            # is published. Never start a second server over stale global state.
            _stop_state(existing)

        try:
            FastMCP = _import_fastmcp()
        except ImportError as exc:
            Debug.log_warning(
                "Infernux MCP disabled: install PyPI packages 'mcp' and 'fastmcp' to enable "
                f"the embedded HTTP server ({exc})."
            )
            return False

        from infernux_mcp.settings import configure, feature_enabled, is_enabled
        config = configure(project_path, write_default=True)
        if not is_enabled():
            Debug.log_internal(
                "Infernux MCP server disabled by "
                "ProjectSettings/mcp_settings.json"
            )
            return False
        from infernux_mcp.session import configure as configure_session
        session_state = configure_session(project_path, config)
        Debug.log_internal(
            "Infernux MCP session configured: "
            f"build_profile={session_state.build_profile}, "
            f"recording={session_state.recording_enabled}"
        )
        if feature_enabled("session_call_log"):
            try:
                from infernux_mcp.trace import start_session_log
                info = start_session_log(project_path)
                Debug.log_internal(
                    f"Infernux MCP session log initialized: {info.get('path')}"
                )
            except Exception as exc:
                Debug.log_suppressed("infernux_mcp.start_session_log", exc)
        from mcp.server.transport_security import TransportSecuritySettings
        # No authentication: MCP is driven by the local user. The SDK guard only
        # stops remote web pages from reaching the loopback port via DNS
        # rebinding; local browser tools (e.g. MCP Inspector) stay allowed.
        mcp = FastMCP(
            SERVER_NAME, stateless_http=True, json_response=True, log_level="WARNING",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
                allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
            ),
        )

        # Keep health probing outside the streamable HTTP mount. A bare GET on
        # /mcp is transport negotiation, not a stable readiness endpoint.
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        @mcp.custom_route(HEALTH_PATH, methods=["GET"])  # type: ignore[attr-defined]
        async def _mcp_health_probe(request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "name": SERVER_NAME,
                    "ready": True,
                }
            )

        from infernux_mcp.adapter import begin_shutdown_adapter, register_gateways, shutdown_adapter
        register_gateways(mcp, project_path, config)

        import uvicorn

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET,
                                socket.SO_EXCLUSIVEADDRUSE if os.name == "nt" else socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(2048)
            listener.setblocking(False)
        except OSError as exc:
            listener.close()
            shutdown_adapter()
            raise RuntimeError("Infernux MCP transport failed before becoming ready") from exc

        transport = uvicorn.Server(
            uvicorn.Config(
                mcp.streamable_http_app(),
                host=str(host),
                port=int(port),
                log_config=None,
                log_level="warning",
                # Proactor reports normal client TCP resets as callback errors
                # on Windows. Scope Selector to this HTTP server's loop; the
                # Editor's process-wide event loop policy stays untouched.
                loop="asyncio:SelectorEventLoop" if os.name == "nt" else "auto",
            )
        )
        state = _ServerState(
            project_path=resolved_path(project_path),
            host=str(host),
            port=int(port),
            mcp=mcp,
            transport=transport,
            adapter_shutdown=shutdown_adapter,
            adapter_begin_shutdown=begin_shutdown_adapter,
        )

        def _run() -> None:
            try:
                # Capture this generation's transport. Reading a module global
                # here lets hot reload redirect an old thread to a new server.
                transport.run(sockets=[listener])
            except BaseException as exc:
                state.error = exc
                if not state.stop_requested.is_set():
                    Debug.log_error(f"Infernux MCP HTTP server stopped: {exc}")

        context = copy_context()
        state.thread = threading.Thread(
            target=lambda: context.run(_run), name="InfernuxMCPHTTP", daemon=True)
        install_runtime_service(_RUNTIME_SERVICE_NAME, state)
        state.thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if bool(getattr(transport, "started", False)):
                if feature_enabled("discovery_files"):
                    _write_discovery_files(project_path, host=host, port=int(port))
                Debug.log_internal(
                    f"Infernux MCP HTTP server ready at "
                    f"{endpoint_url(host=host, port=int(port))}"
                )
                return True
            if state.error is not None or not state.is_alive():
                error = state.error
                _stop_state(state)
                raise RuntimeError(
                    "Infernux MCP transport failed before becoming ready"
                ) from error
            time.sleep(0.01)
        _stop_state(state)
        raise RuntimeError("Infernux MCP transport readiness timed out after 5 seconds")


def stop_server() -> None:
    """Stop the process-owned transport and wait until its port is released."""
    with _lifecycle_lock:
        state = _active_state()
        if state is not None:
            _stop_state(state)


def request_stop_server() -> None:
    """Request transport retirement without waiting on an in-flight request."""
    with _lifecycle_lock:
        state = _active_state()
        if state is not None:
            _request_stop_state(state)


def _stop_state(state: _ServerState) -> None:
    _request_stop_state(state)
    if not state.stopped.wait(timeout=10.0):
        raise RuntimeError("Infernux MCP transport did not stop within 10 seconds")


def _request_stop_state(state: _ServerState) -> None:
    state.stop_requested.set()
    if state.adapter_begin_shutdown is not None:
        state.adapter_begin_shutdown()
    state.transport.should_exit = True
    with state.cleanup_lock:
        if state.reaper_started.is_set():
            return
        state.reaper_started.set()
    threading.Thread(
        target=_reap_state,
        args=(state,),
        name="InfernuxMCPRetire",
        daemon=True,
    ).start()


def _reap_state(state: _ServerState) -> None:
    thread = state.thread
    if thread is not None and thread is not threading.current_thread():
        thread.join()
    try:
        state.adapter_shutdown()
        if state.project_path:
            _remove_discovery_files(
                state.project_path, host=state.host, port=state.port
            )
        remove_runtime_service(_RUNTIME_SERVICE_NAME, state)
    except BaseException as exc:
        state.error = exc
        Debug.log_error(f"Infernux MCP transport retirement failed: {exc}")
    else:
        state.stopped.set()


def is_running() -> bool:
    state = _active_state()
    return state is not None and state.is_alive()


def endpoint_url(*, host: str | None = None, port: int | None = None) -> str:
    state = _active_state()
    resolved_host = (
        (state.host if state is not None else HOST) if host is None else host
    )
    resolved_port = (
        (state.port if state is not None else PORT) if port is None else int(port)
    )
    return f"http://{resolved_host}:{resolved_port}{PATH}"


def health_url(*, host: str | None = None, port: int | None = None) -> str:
    state = _active_state()
    resolved_host = (
        (state.host if state is not None else HOST) if host is None else host
    )
    resolved_port = (
        (state.port if state is not None else PORT) if port is None else int(port)
    )
    return f"http://{resolved_host}:{resolved_port}{HEALTH_PATH}"


def connection_info(*, host: str | None = None, port: int | None = None) -> dict:
    state = _active_state()
    resolved_host = (
        (state.host if state is not None else HOST) if host is None else host
    )
    resolved_port = (
        (state.port if state is not None else PORT) if port is None else int(port)
    )
    url = endpoint_url(host=resolved_host, port=resolved_port)
    return {
        "name": SERVER_NAME,
        "transport": "streamable-http",
        "host": resolved_host,
        "port": resolved_port,
        "path": PATH,
        "url": url,
        "health_url": health_url(host=resolved_host, port=resolved_port),
        "clients": _client_connection_configs(url),
    }


def _client_connection_configs(url: str) -> dict:
    """Direct streamable-HTTP entries in each client's own config dialect."""
    streamable = {"url": url, "transport": "streamable-http"}
    typed_http = {"type": "http", "url": url}
    clients = {}
    for name, file, root_key, entry in (
        ("generic", "mcp.json", "mcpServers", streamable),
        ("cursor", ".cursor/mcp.json", "mcpServers", streamable),
        ("claude_code", ".mcp.json", "mcpServers", typed_http),
        ("vscode_copilot", ".vscode/mcp.json", "servers", typed_http),
        ("trae", ".trae/mcp.json", "mcpServers", typed_http),
        ("gemini", ".gemini/settings.json", "mcpServers",
         {"httpUrl": url, "timeout": 600000, "trust": False}),
    ):
        clients[name] = {"file": file, "format": root_key,
                         "config": {root_key: {"infernux-editor": dict(entry)}}}
    return clients


def _write_discovery_files(project_path: str, *, host: str, port: int) -> None:
    """Write small project-local MCP discovery files for external agents.

    These files are intentionally data-only and safe to regenerate. They make
    the embedded editor MCP endpoint discoverable without hard-coding the port
    in an agent prompt.
    """
    root = resolved_path(project_path or "")
    if not root:
        return
    info = connection_info(host=host, port=port)
    _update_discovery_file(os.path.join(root, "mcp.json"), _write_generic_manifest_file, info)
    for client_name, client in info["clients"].items():
        if client_name != "generic":
            target = os.path.join(root, client["file"])
            _update_discovery_file(target, _merge_client_json_config, client["config"])


def _remove_discovery_files(
    project_path: str, *, host: str = HOST, port: int = PORT
) -> None:
    """Remove only discovery entries owned by this plugin.

    Client configuration files may contain unrelated user servers, so unload
    must be the inverse of the merge performed by ``_write_discovery_files``
    rather than deleting those files wholesale.
    """

    root = resolved_path(project_path or "")
    if not root:
        return
    _update_discovery_file(os.path.join(root, "mcp.json"), _remove_generic_manifest)
    info = connection_info(host=host, port=port)
    for client_name, client in info["clients"].items():
        if client_name != "generic":
            target = os.path.join(root, client["file"])
            root_key = next(iter(client["config"]))
            _update_discovery_file(target, _remove_json_server, root_key, "infernux-editor")


def _update_discovery_file(path: str, update, *args) -> None:
    try:
        update(path, *args)
    except (OSError, ValueError) as exc:
        Debug.log_suppressed(f"infernux_mcp.discovery_file: {path}", exc)


def _remove_generic_manifest(path: str) -> None:
    data = _read_json_object(path)
    if not data:
        return
    data.pop("infernux", None)
    data.pop("clients", None)
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        servers.pop("infernux-editor", None)
        if not servers:
            data.pop("mcpServers", None)
    _write_or_remove_json(path, data)


def _remove_json_server(path: str, root_key: str, server_name: str) -> None:
    data = _read_json_object(path)
    servers = data.get(root_key)
    if not isinstance(servers, dict) or server_name not in servers:
        return
    servers.pop(server_name, None)
    if not servers:
        data.pop(root_key, None)
    _write_or_remove_json(path, data)


def _write_or_remove_json(path: str, value: dict) -> None:
    if value:
        _write_json_if_changed(path, value)
        return
    _remove_generated_file(path)


def _remove_generated_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    parent = os.path.dirname(path)
    if os.path.basename(parent) in {".cursor", ".vscode", ".trae", ".gemini"}:
        try:
            os.rmdir(parent)
        except OSError:
            pass


def _write_generic_manifest_file(path: str, info: dict) -> None:
    generic = info["clients"]["generic"]["config"]
    data = _read_json_object(path)
    data["infernux"] = {
        "name": info["name"],
        "transport": info["transport"],
        "host": info["host"],
        "port": info["port"],
        "path": info["path"],
        "url": info["url"],
    }
    data["clients"] = {
        name: {"file": client["file"], "format": client["format"]}
        for name, client in info["clients"].items()
    }
    for root_key, root_value in generic.items():
        if isinstance(root_value, dict):
            bucket = data.setdefault(root_key, {})
            if isinstance(bucket, dict):
                bucket.update(root_value)
            else:
                data[root_key] = root_value
        else:
            data[root_key] = root_value
    _write_json_if_changed(path, data)


def _merge_client_json_config(path: str, config: dict) -> None:
    data = _read_json_object(path)
    for root_key, root_value in config.items():
        if isinstance(root_value, dict):
            bucket = data.setdefault(root_key, {})
            if isinstance(bucket, dict):
                bucket.update(root_value)
            else:
                data[root_key] = root_value
        else:
            data[root_key] = root_value
    _write_json_if_changed(path, data)


def _read_json_object(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"MCP client configuration must be a JSON object: {path}")
    return value


def _write_json_if_changed(path: str, value: dict) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    _write_text_if_changed(path, text)


def _write_text_if_changed(path: str, text: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == text:
                return
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _import_fastmcp():
    # The embedded server is owned by the official MCP SDK.  The separate
    # ``fastmcp`` distribution is used by our loopback client, but its server
    # class has a different ASGI application contract.  Mixing the two behind
    # an import fallback makes startup depend on import order.
    from mcp.server.fastmcp import FastMCP

    return FastMCP
