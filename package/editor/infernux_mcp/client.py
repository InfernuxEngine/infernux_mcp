"""Verified loopback client transport for Supervisor-driven MCP sessions."""

from __future__ import annotations

import argparse
import concurrent.futures
import asyncio
from contextlib import asynccontextmanager
import json
import math
from pathlib import Path
import sys
import threading
import time
from typing import Any
from urllib.parse import urlparse


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def create_loopback_client(endpoint: str, *, timeout_seconds: float | None = None):
    """Create a FastMCP client that never inherits remote proxy settings.

    Supervisor sessions deliberately bind to loopback. Disabling inherited HTTP
    environment settings prevents remote-desktop proxy configuration from
    redirecting local MCP traffic away from the Editor.
    """
    parsed = urlparse(str(endpoint or ""))
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS or not parsed.path:
        raise ValueError("Loopback MCP client requires an http://127.0.0.1 or http://localhost endpoint.")

    import httpx
    from fastmcp import Client
    from fastmcp.client.transports.http import StreamableHttpTransport

    def http_client_factory(headers=None, auth=None, follow_redirects=True, timeout=None):
        return httpx.AsyncClient(
            headers=headers,
            auth=auth,
            follow_redirects=follow_redirects,
            timeout=timeout or httpx.Timeout(30.0, read=300.0),
            trust_env=False,
        )

    transport = StreamableHttpTransport(str(endpoint), httpx_client_factory=http_client_factory)
    return Client(transport, timeout=timeout_seconds, init_timeout=timeout_seconds)


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _failed(value: Any, *, tool: str = "") -> bool:
    """Recognize gateway failures without interpreting arbitrary result data."""
    if not isinstance(value, dict):
        return False
    if value.get("ok") is False:
        return True
    data = value.get("data")
    if tool == "operation_batch_execute" and isinstance(data, dict):
        return any(isinstance(row, dict) and row.get("ok") is False
                   for row in data.get("results", ()))
    if tool == "operation_job_status" and isinstance(data, dict):
        return data.get("ok") is False or data.get("cancelled") is True
    return False


def _timing(args: argparse.Namespace, phase: str, started: float, **fields: Any) -> None:
    if getattr(args, "timings", False):
        print(json.dumps({"type": "timing", "phase": phase,
                          "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                          **fields}, ensure_ascii=False), file=sys.stderr, flush=True)


def _write_json(args: argparse.Namespace, value: Any, *, compact: bool = False) -> None:
    started = time.perf_counter()
    encoded = json.dumps(value, ensure_ascii=False, indent=None if compact else 2)
    _timing(args, "serialize", started)
    print(encoded, flush=True)


class _PhaseError(RuntimeError):
    def __init__(self, phase: str, cause: Exception):
        super().__init__(str(cause) or type(cause).__name__)
        self.phase = phase


def _error(exc: Exception, phase: str) -> dict[str, Any]:
    phase = getattr(exc, "phase", phase)
    message = str(exc) or type(exc).__name__
    return {"ok": False, "error": {"code": f"client.{phase}_failed", "message": message,
                                   "phase": phase}}


@asynccontextmanager
async def _connected(args: argparse.Namespace):
    phase = "client_create"
    started = time.perf_counter()
    try:
        client = create_loopback_client(args.endpoint, timeout_seconds=args.timeout)
        _timing(args, phase, started)
        phase, started = "connect_initialize", time.perf_counter()
        # Keep entry/exit in one task: AnyIO cancellation scopes must retain
        # their owner. Never reconnect/replay a failed command.
        async with client:
            _timing(args, phase, started)
            phase = "request"
            try:
                yield client
            except Exception as exc:
                raise _PhaseError("request", exc) from exc
            finally:
                phase, started = "disconnect", time.perf_counter()
        _timing(args, phase, started)
    except _PhaseError:
        raise
    except Exception as exc:
        _timing(args, phase, started, failed=True)
        raise _PhaseError(phase, exc) from exc


async def _call(client, args: argparse.Namespace, tool: str, arguments: dict[str, Any]) -> Any:
    started = time.perf_counter()
    try:
        return _json_value((await client.call_tool(tool, arguments)).data)
    except Exception as exc:
        value = _error(exc, "request")
        value["error"]["hint"] = (
            "A transport timeout or disconnect does not prove a command was cancelled. "
            "Inspect project/job state before retrying; this client does not replay calls."
        )
        return value
    finally:
        _timing(args, "request", started, tool=tool)


def _arguments(args: argparse.Namespace) -> dict[str, Any]:
    path = getattr(args, "args_file", None)
    content = (sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")) if path else args.args
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Arguments must decode to a JSON object")
    return value


async def _run_cli(args: argparse.Namespace) -> Any:
    # Parse local inputs before connecting; invalid JSON must never send a call.
    try:
        tool_args = _arguments(args) if args.command == "call" else None
    except (ValueError, OSError) as exc:
        raise _PhaseError("input", exc) from exc
    async with _connected(args) as client:
        if args.command in {"list-tools", "describe"}:
            started = time.perf_counter()
            tools = list(await client.list_tools())
            _timing(args, "list_tools", started)
            if args.command == "describe":
                matches = [tool for tool in tools if tool.name == args.tool]
                if not matches:
                    raise ValueError(f"MCP tool not found: {args.tool}")
                return _json_value(matches[0])
            query = str(args.match or "").casefold()
            if query:
                tools = [tool for tool in tools if query in tool.name.casefold()]
            return _json_value(tools)
        return await _call(client, args, args.tool, tool_args)


async def _stdin_lines():
    # A daemon reader keeps the live MCP event loop responsive while a caller
    # decides its next request. Unlike asyncio.to_thread(readline), this cannot
    # hold asyncio.run() in executor shutdown after Ctrl+C on an idle pipe.
    loop = asyncio.get_running_loop()
    lines = asyncio.Queue(maxsize=1)
    stopped = threading.Event()
    stream = sys.stdin

    def read():
        while not stopped.is_set():
            try:
                line = stream.readline()
            except Exception as exc:
                line = exc
            if stopped.is_set():
                return
            try:
                pending = asyncio.run_coroutine_threadsafe(lines.put(line), loop)
                # Backpressure bounds memory when a large file is piped in.
                while not stopped.is_set():
                    try:
                        pending.result(timeout=0.1)
                        break
                    except concurrent.futures.TimeoutError:
                        continue
                if stopped.is_set():
                    pending.cancel()
                    return
            except Exception:
                return
            if not line or isinstance(line, Exception):
                return

    threading.Thread(target=read, name="InfernuxMCPInput", daemon=True).start()
    try:
        while True:
            line = await lines.get()
            if isinstance(line, Exception):
                raise line
            if not line:
                return
            yield line
    finally:
        stopped.set()


async def _run_session(args: argparse.Namespace) -> int:
    """Read one tool request per line, keeping a single initialized connection."""
    failed = False
    async with _connected(args) as client:
        async for line in _stdin_lines():
            if not line.strip():
                continue
            request_id = None
            tool = ""
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Each session line must be a JSON object")
                request_id = request.get("id")
                if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
                    request_id = None
                    raise ValueError("id must be a string, integer, or null")
                unknown = set(request) - {"id", "tool", "arguments"}
                if unknown:
                    raise ValueError(f"Unknown session fields: {', '.join(sorted(unknown))}")
                tool = request.get("tool", "")
                arguments = request.get("arguments", {})
                if not isinstance(tool, str) or not tool.strip():
                    raise ValueError("tool must be a non-empty string")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be a JSON object")
                value = await _call(client, args, tool, arguments)
            except (ValueError, TypeError) as exc:
                value = _error(exc, "input")
            failed = _failed(value, tool=tool) or failed
            _write_json(args, {"id": request_id, "result": value}, compact=True)
    return int(failed)


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and greater than zero")
    return seconds


def main(argv: list[str] | None = None) -> int:
    """Run a proxy-safe, machine-readable client for loopback Editor MCP sessions."""
    parser = argparse.ArgumentParser(prog="infernux-mcp")
    parser.add_argument("--endpoint", default="http://127.0.0.1:9713/mcp")
    parser.add_argument("--timeout", type=_positive_seconds, default=30.0,
                        help="MCP initialization and per-request timeout in seconds (default: 30)")
    parser.add_argument("--timings", action="store_true", help="Write client phase timings as JSON to stderr")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_tools = subparsers.add_parser("list-tools")
    list_tools.add_argument("--match", default="")
    describe = subparsers.add_parser("describe")
    describe.add_argument("tool")
    call = subparsers.add_parser("call")
    call.add_argument("tool")
    inputs = call.add_mutually_exclusive_group()
    inputs.add_argument("--args", default="{}")
    inputs.add_argument("--args-file", help="Read UTF-8 JSON arguments from a file, or - for stdin")
    subparsers.add_parser("session", help="Read JSON-lines tool calls from stdin using one live connection")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    try:
        if args.command == "session":
            return asyncio.run(_run_session(args))
        value = asyncio.run(_run_cli(args))
        _write_json(args, value)
        return int(_failed(value, tool=getattr(args, "tool", "")))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps(_error(exc, "client"), ensure_ascii=False), file=sys.stderr, flush=True)
        return 1
    finally:
        _timing(args, "total", started)


if __name__ == "__main__":
    raise SystemExit(main())
