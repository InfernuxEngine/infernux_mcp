"""Generic InxPreload lifecycle for the default MCP Host service."""

from __future__ import annotations

import os
import threading

from infernux.lifecycle import InxPreload, PreloadContext


class InfernuxMCPPreload(InxPreload):
    def __init__(self) -> None:
        self._loaded = False
        self._starter: threading.Thread | None = None
        self._stop_server = None

    def preload(self, context: PreloadContext) -> None:
        # Project Cook runs the plugin manager in runtime mode, even though it
        # imports package declarations to publish authored types.  Only that
        # explicit runtime boundary suppresses the editor service.  Do not
        # infer editor-ness from Application here: headless/editor harnesses
        # legitimately preload the MCP service before a native Application
        # object exists.
        if context.runtime:
            return

        project_root = context.project_root
        host = os.environ.get("INFERNUX_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port = int(os.environ.get("INFERNUX_MCP_PORT", "9713"))

        # Resolve the plugin entry points while PluginManager still owns the
        # temporary import path.  Deferring this import to the worker races the
        # context exit and can resolve a different checkout of the same plugin.
        from infernux_mcp.server import start_server, stop_server

        # PluginManager.uninstall() is synchronous: discovery files and the
        # runtime service must be gone before it returns.  Keep the matching
        # synchronous server shutdown here; request_stop_server() only starts
        # the reaper and would leave mcp.json visible to the caller briefly.
        self._stop_server = stop_server

        # The FastMCP/starlette/uvicorn import chain plus the server readiness
        # wait is the single heaviest piece of editor plugin preload, and the
        # splash screen sits on "Preloading project plugins…" for all of it.
        # Nothing at startup depends on the HTTP endpoint being reachable
        # before the first client connects, so bring it up off-thread and let
        # a failure surface in the log instead of blocking the editor.
        def _start() -> None:
            try:
                if not start_server(project_root, host=host, port=port):
                    raise RuntimeError("Infernux MCP server did not start")
            except Exception as exc:
                from infernux.debug import Debug

                Debug.log_error(f"Infernux MCP server failed to start: {exc}")

        self._starter = threading.Thread(
            target=_start, name="InfernuxMCPStart", daemon=True
        )
        self._starter.start()
        self._loaded = True

    def unload(self) -> None:
        if not self._loaded:
            return
        starter = self._starter
        if starter is not None:
            starter.join(timeout=10.0)
            self._starter = None
        stop_server = self._stop_server
        self._stop_server = None
        if stop_server is not None:
            stop_server()
        self._loaded = False


__all__ = ["InfernuxMCPPreload"]
