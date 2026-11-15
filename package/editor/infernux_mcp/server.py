"""Independent standard MCP service backed by the public Automation SDK."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import json
import anyio
from fastmcp import FastMCP, Context
from fastmcp.server.middleware import Middleware
from fastmcp.server.tasks.context import get_task_session_id
from infernux_automation import AutomationClient, AutomationError


@dataclass
class _EngineSession:
    identity: str | None = None
    client: AutomationClient | None = None
    observing: bool = False
    closed: bool = False
    pulse: asyncio.Task | None = None
    protocol_ids: set[str] = field(default_factory=set)


def create_server(project, *, stopping=None):
    mcp = FastMCP("Infernux", instructions=(
        "Use operation_search and operation_schema to discover current engine capabilities. "
        "World edits, input, builds, captures and observations run through the engine's public Host. "
        "Observe uses actual domain signals. Turn reports require a real conversation_turn or hook; "
        "tool calls do not count as user turns. Failed writes are never replayed automatically."))
    sessions = {}
    session_scope = ContextVar("infernux_mcp_session", default=None)

    # Docket restores the standard MCP task snapshot, not arbitrary ContextVars.
    # Bind that snapshot's protocol session to the same owner as normal calls.
    class SessionBinding(Middleware):
        async def on_request(self, context, call_next):
            if stopping is not None and stopping.is_set():
                raise AutomationError("mcp.service_stopped", "MCP source changed; restart the service to load current code")
            owner = session_scope.get()
            if owner is not None and context.fastmcp_context is not None:
                identity = context.fastmcp_context.session_id
                owner.identity = identity
                owner.protocol_ids.add(identity)
                sessions[identity] = owner
            return await call_next(context)

    mcp.add_middleware(SessionBinding())

    def current_session():
        task_session = get_task_session_id()
        owner = sessions.get(task_session) if task_session is not None else session_scope.get()
        if owner is None or owner.closed or owner.identity is None:
            raise AutomationError("mcp.session_closed", "The originating MCP session is closed")
        return owner

    def connection():
        owner = current_session()
        if owner.client is None:
            owner.client = AutomationClient(project, session_id=owner.identity)
        return owner.client

    async def send(owner, client, method, **arguments):
        if stopping is not None and stopping.is_set():
            raise AutomationError("mcp.service_stopped", "MCP source changed; restart the service to load current code")
        if owner.closed or owner.client is not client:
            raise AutomationError("mcp.session_closed", "The originating MCP connection was closed or replaced")
        value = await asyncio.to_thread(client.request, method, **arguments)
        if method == "query_start":
            owner.observing = True
        return value

    async def request(method, **arguments):
        return await send(current_session(), connection(), method, **arguments)

    async def release(client, method, **arguments):
        # Cleanup keeps its original engine binding, even after MCP disconnect or
        # an explicit engine_connect. It never replays the operation that ran.
        with anyio.CancelScope(shield=True):
            try:
                await asyncio.to_thread(client.request, method, **arguments)
            except AutomationError:
                pass  # An unreachable engine expires query leases; effects stay unknown.

    async def acquire_resource(owner, client, method, arguments, cleanup):
        # A cancelled to_thread wait does not cancel the HTTP operation. Retain
        # its eventual handle so cancellation cannot abandon a created resource.
        pending = asyncio.create_task(send(owner, client, method, **arguments))
        value = None
        try:
            value = await asyncio.shield(pending)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                try:
                    value = await pending
                except Exception:
                    pass  # No handle was returned; do not repeat an unknown operation.
            if value is not None:
                await cleanup(client, value)
            raise
        if owner.closed or owner.client is not client:
            await cleanup(client, value)
            raise AutomationError("mcp.session_closed", "The originating MCP connection was closed or replaced")
        return value

    @asynccontextmanager
    async def owned_resource(owner, client, method, arguments, cleanup):
        value = await acquire_resource(owner, client, method, arguments, cleanup)
        try:
            yield value
        finally:
            await cleanup(client, value)

    def watched_query(owner, client, **arguments):
        return owned_resource(owner, client, "query_start", arguments,
                              lambda bound, watch: release(bound, "query_stop", query_id=watch["id"]))

    # The MCP SDK invokes its low-level lifespan once for each protocol session,
    # including stdio EOF, HTTP DELETE/expiry and in-process client disconnect.
    # Compose that lifecycle; no client-brand or hand-built protocol is involved.
    protocol_lifespan = mcp._mcp_server.lifespan

    async def close_session(owner):
        if owner.closed:
            return
        owner.closed = True
        for identity in owner.protocol_ids:
            if sessions.get(identity) is owner:
                del sessions[identity]
        with anyio.CancelScope(shield=True):
            owner.pulse.cancel()
            await asyncio.gather(owner.pulse, return_exceptions=True)
            owner.observing = False
            if owner.client is not None:
                try:
                    await asyncio.to_thread(owner.client.close)
                except AutomationError:
                    pass  # Engine leases cover an unreachable/killed service.

    # FastMCP 3.4.8 waits for task-status subscriptions before exiting its
    # protocol lifespan. Release observations when the SDK input stream closes,
    # so a task waiting on those observations cannot hold its own session open.
    # The stream still passes every protocol message to the unmodified SDK.
    protocol_run = mcp._mcp_server.run

    class SessionInput(anyio.abc.ObjectReceiveStream):
        def __init__(self, stream):
            self.stream = stream

        async def receive(self):
            try:
                return await self.stream.receive()
            except (anyio.EndOfStream, anyio.ClosedResourceError):
                owner = session_scope.get()
                if owner is not None:
                    await close_session(owner)
                raise

        async def aclose(self):
            await self.stream.aclose()

    async def session_run(read_stream, write_stream, *args, **kwargs):
        return await protocol_run(SessionInput(read_stream), write_stream, *args, **kwargs)

    mcp._mcp_server.run = session_run

    @asynccontextmanager
    async def session_lifespan(server):
        owner = _EngineSession()
        token = session_scope.set(owner)
        async def heartbeat():
            while True:
                await asyncio.sleep(25.0)
                if owner.observing and owner.client is not None:
                    try:
                        await asyncio.to_thread(owner.client.keepalive)
                    except AutomationError:
                        # A disconnected engine is never silently rebound.
                        owner.observing = False
        owner.pulse = asyncio.create_task(heartbeat())
        async def stop_source_session():
            await stopping.wait()
            await close_session(owner)
        source_stop = asyncio.create_task(stop_source_session()) if stopping is not None else None
        try:
            async with protocol_lifespan(server) as context:
                yield context
        finally:
            if source_stop is not None:
                if not stopping.is_set():
                    source_stop.cancel()
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(source_stop, return_exceptions=True)
            await close_session(owner)
            session_scope.reset(token)

    mcp._mcp_server.lifespan = session_lifespan

    async def response(value, ctx, consumer=""):
        consumer = consumer or ctx.session_id
        try:
            reports = await request("reports", consumer=consumer)
        except AutomationError as exc:
            return {"result": value, "reports_error": {"code": exc.code, "message": str(exc)}}
        return {"result": value, "reports": reports} if reports else value

    @mcp.tool(annotations={"readOnlyHint": True})
    async def engine_status() -> dict:
        """Read the selected project and engine instance. No native engine import occurs in this service."""
        return await request("info")

    @mcp.tool
    async def engine_connect() -> dict:
        """Explicitly bind to the currently running engine of this project after an engine restart."""
        owner = current_session()
        previous = owner.client
        owner.client = None
        if previous is not None:
            try:
                await asyncio.to_thread(previous.close)
            except AutomationError:
                pass
        owner.client = AutomationClient(project, session_id=owner.identity)
        owner.observing = False
        return await request("info")

    @mcp.tool(annotations={"readOnlyHint": True})
    async def operation_search(query: str = "", limit: int = 30) -> dict:
        """Find current operations by intent or domain; retrieve the full schema only for selected operations."""
        value = await request("operations", query=query, limit=limit)
        value["operations"] = [{key: item[key] for key in ("id", "kind", "summary", "tags")}
                               for item in value["operations"]]
        return value

    @mcp.tool(annotations={"readOnlyHint": True})
    async def operation_schema(operation: str) -> dict:
        """Read the complete input/output schema of one current operation."""
        return await request("schema", operation=operation)

    @mcp.tool
    async def operation_call(operation: str, arguments: dict, ctx: Context, consumer: str = ""):
        """Invoke one current query/command/workflow. Unknown write outcomes are never retried."""
        value = await request("call", operation=operation, arguments=arguments)
        return await response(value, ctx, consumer)

    @mcp.tool
    async def operation_batch(calls: list[dict], ctx: Context, consumer: str = ""):
        """Execute ordered explicit operations, stopping at the first failure; no world rollback is implied."""
        return await response(await request("batch", calls=calls), ctx, consumer)

    @mcp.tool
    async def operation_submit(operation: str, arguments: dict) -> dict:
        """Submit a long engine operation and retain its engine-owned job handle."""
        return await request("job_submit", operation=operation, arguments=arguments)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def operation_job(job_id: str) -> dict:
        """Read the actual engine job status/result."""
        return await request("job_status", job_id=job_id)

    @mcp.tool
    async def operation_cancel(job_id: str) -> dict:
        """Cancel queued engine work. A running operation may already have applied changes."""
        return {"cancelled": await request("job_cancel", job_id=job_id)}

    @mcp.tool
    async def observe(operation: str, arguments: dict, ctx: Context, signals: list[str] | None = None,
                      reports: int = 3, mode: str = "adaptive", consumer: str = "") -> dict:
        """Observe a query on domain signals, optionally reporting through later conversation turns.

        modes: changes/adaptive count changed results; turns counts distinct real cycle IDs.
        Use a stable conversation ID as consumer when a native prompt hook is connected.
        """
        owner, client = current_session(), connection()
        async def close_query(bound, watch):
            await release(bound, "query_stop", query_id=watch["id"])
        watch = await acquire_resource(owner, client, "query_start",
                                       {"operation": operation, "arguments": arguments, "signals": signals}, close_query)
        try:
            delivery = await send(owner, client, "report_start", query_id=watch["id"],
                                  consumer=consumer or ctx.session_id, reports=reports, mode=mode)
        except BaseException:
            await close_query(client, watch)
            raise
        return {"observation": watch, "delivery": delivery}

    @mcp.tool(annotations={"readOnlyHint": True})
    async def observation(query_id: str, after: int | None = None, timeout: float = 25.0) -> dict:
        """Read a query snapshot or wait for its next actual signal/result change; no frame polling."""
        if after is None:
            return await request("query_snapshot", query_id=query_id)
        return await request("query_wait", query_id=query_id, after=after, timeout=timeout)

    @mcp.tool
    async def stop_observing(query_id: str, report_id: str = "") -> dict:
        """Release a query subscription and its optional report delivery."""
        if report_id:
            await request("report_stop", report_id=report_id)
        return {"stopped": await request("query_stop", query_id=query_id)}

    @mcp.tool
    async def conversation_turn(consumer: str, cycle_id: str) -> dict:
        """Deliver scheduled reports for a real user turn. Repeating the same ID never consumes twice."""
        return {"reports": await request("reports", consumer=consumer, cycle_id=cycle_id)}

    @mcp.resource("infernux://catalog")
    async def catalog_resource():
        return json.dumps(await request("operations", query="", limit=10000), ensure_ascii=False)

    @mcp.resource("infernux://operation/{operation}")
    async def schema_resource(operation: str):
        return json.dumps(await request("schema", operation=operation), ensure_ascii=False)

    @mcp.resource("infernux://query/{query_id}")
    async def query_resource(query_id: str):
        return json.dumps(await request("query_snapshot", query_id=query_id), ensure_ascii=False)

    @mcp.tool(annotations={"readOnlyHint": True})
    def help() -> dict:
        """Read connection/workflow guidance even while no engine is running."""
        return {"project": str(Path(project).resolve()), "architecture": "MCP -> Automation SDK -> public engine Host",
                "queries": "Signals: world.changed, scene.changed, runtime.changed, player.changed, assets.changed, scripts.changed, console.changed, capture.completed, operations.changed; defaults follow the operation domain; custom signals are supported.",
                "reports": "changes/adaptive report new results; turns requires a distinct real cycle_id. MCP notifications never start a new model turn.",
                "clients": "Claude Code, Codex, DSH and WorkBuddy connect through standard stdio or Streamable HTTP. No private chat databases are read."}

    @mcp.tool(task=True)
    async def load_and_capture(asset_guid: str, source: str = "game", timeout: float = 30.0) -> dict:
        """Open a scene, await its real ready state, then capture it. Supports standard MCP background tasks."""
        from time import monotonic
        owner, client = current_session(), connection()
        async def invoke(method, **arguments):
            return await send(owner, client, method, **arguments)
        deadline = monotonic() + timeout
        async def changed(watch, revision):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Scene/capture workflow did not finish within its time budget")
            snapshot = await invoke("query_wait", query_id=watch["id"], after=revision, timeout=min(25.0, remaining))
            if snapshot["status"] == "closed":
                raise RuntimeError("The engine observation was closed")
            if snapshot["error"]:
                raise RuntimeError(snapshot["error"])
            return snapshot
        opened = await invoke("call", operation="infernux.scene.open", arguments={"asset_guid": asset_guid})
        async with watched_query(owner, client, operation="infernux.project.info", arguments={}, signals=["scene.changed"]) as watch:
            revision = 0
            while True:
                snapshot = await changed(watch, revision)
                info = snapshot["value"] or {}
                outcome = (info.get("active_scene") or {}).get("last_load") or {}
                if outcome.get("status") == "failed":
                    raise RuntimeError(str(outcome))
                if outcome.get("status") == "loaded" and Path(outcome["path"]) == Path(opened["path"]):
                    break
                revision = snapshot["revision"]
        completed = False
        async def close_capture(bound, capture):
            if not completed:
                await release(bound, "call", operation="infernux.capture.cancel", arguments={"capture_id": capture["capture_id"]})
        async with owned_resource(owner, client, "call",
                                  {"operation": "infernux.capture.request", "arguments": {"source": source}}, close_capture) as capture:
            async with watched_query(owner, client, operation="infernux.capture.status",
                                     arguments={"capture_id": capture["capture_id"]}, signals=["capture.completed"]) as watch:
                revision = 0
                while True:
                    snapshot = await changed(watch, revision)
                    value = snapshot["value"] or {}
                    if value.get("terminal"):
                        if value["status"] != "completed":
                            raise RuntimeError(str(value))
                        completed = True
                        return value
                    revision = snapshot["revision"]

    return mcp
