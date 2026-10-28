"""Embedded MCP integration for Infernux Editor.

The package initializer stays side-effect free.  Build-time declaration scans
and editor lifecycle discovery import the package before deciding whether an
MCP host belongs in the current process; importing the HTTP server here would
instantiate FastMCP even in a Player Cook worker.
"""


def current_config():
    from infernux_mcp.settings import current_config as implementation

    return implementation()


def endpoint_url(*, host=None, port=None):
    from infernux_mcp.server import endpoint_url as implementation

    return implementation(host=host, port=port)


def health_url(*, host=None, port=None):
    from infernux_mcp.server import health_url as implementation

    return implementation(host=host, port=port)


def is_running():
    from infernux_mcp.server import is_running as implementation

    return implementation()


def start_server(project_path, *, host="127.0.0.1", port=9713):
    from infernux_mcp.server import start_server as implementation

    return implementation(project_path, host=host, port=port)


def stop_server():
    from infernux_mcp.server import stop_server as implementation

    return implementation()

__all__ = ["current_config", "endpoint_url", "health_url", "is_running", "start_server", "stop_server"]
