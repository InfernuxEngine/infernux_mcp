"""Real Streamable HTTP MCP Tasks and the real SDK, without engine/native imports.

The engine endpoint below controls when scene/capture publications become ready;
it records identities and resource handles rather than faking the MCP scheduler.
"""
import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
from threading import Condition, Event, Thread
from time import monotonic
import unittest
import uuid

import uvicorn
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "package/editor"))
from infernux_mcp.server import create_server


class EngineEndpoint:
    def __init__(self, project):
        self.condition = Condition()
        self.calls = []
        self.queries = {}
        self.scenes = {}
        self.captures = {}
        self.closed_sessions = set()
        self.scene_ready = True
        self.capture_ready = True
        self.creation_gate = None
        self.capture_gate = None
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                document = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    value = engine.request(document["session_id"], self.path[1:], document["arguments"])
                    response = {"ok": True, "result": value}
                except Exception as exc:
                    response = {"ok": False, "error": {"code": "test.failed", "message": str(exc)}}
                data = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        descriptor = Path(project) / "Cache/Automation/engine.json"
        descriptor.parent.mkdir(parents=True)
        descriptor.write_text(json.dumps({"instance_id": "controlled-engine",
                                          "endpoint": f"http://127.0.0.1:{self.server.server_port}"}), encoding="utf-8")

    def request(self, session, method, arguments):
        with self.condition:
            self.calls.append((session, method, arguments))
            self.condition.notify_all()
        if method == "query_start" and self.creation_gate is not None:
            self.creation_gate.wait(5)
        if method == "call" and arguments.get("operation") == "infernux.capture.request" and self.capture_gate is not None:
            self.capture_gate.wait(5)
        with self.condition:
            if method == "info":
                return {"instance_id": "controlled-engine", "session_id": session}
            if method == "session_ping":
                return {"connected": True}
            if method == "session_close":
                self.closed_sessions.add(session)
                for identity, query in tuple(self.queries.items()):
                    if query["session"] == session:
                        del self.queries[identity]
                self.condition.notify_all()
                return {"queries_closed": 0, "reports_closed": 0}
            if method == "reports":
                return []
            if method == "call":
                operation, args = arguments["operation"], arguments["arguments"]
                if operation == "infernux.scene.open":
                    self.scenes[session] = args["asset_guid"]
                    return {"path": "/" + args["asset_guid"]}
                if operation == "infernux.capture.request":
                    identity = uuid.uuid4().hex
                    self.captures[identity] = {"session": session, "cancelled": False}
                    self.condition.notify_all()
                    return {"capture_id": identity}
                if operation == "infernux.capture.cancel":
                    self.captures[args["capture_id"]]["cancelled"] = True
                    self.condition.notify_all()
                    return {"cancelled": True}
                raise ValueError(operation)
            if method == "query_start":
                identity = uuid.uuid4().hex
                self.queries[identity] = {"session": session, "operation": arguments["operation"],
                                          "arguments": arguments["arguments"]}
                self.condition.notify_all()
                return {"id": identity, "status": "pending"}
            if method == "query_stop":
                result = self.queries.pop(arguments["query_id"], None) is not None
                self.condition.notify_all()
                return result
            if method == "query_wait":
                identity = arguments["query_id"]
                self.condition.wait_for(lambda: identity not in self.queries or arguments["after"] == 0
                                        or self._ready(self.queries[identity]), timeout=min(arguments["timeout"], 3))
                if identity not in self.queries:
                    return {"id": identity, "status": "closed", "revision": 0, "value": None, "error": None}
                query = self.queries[identity]
                ready = self._ready(query)
                if query["operation"] == "infernux.project.info":
                    value = {"active_scene": {"last_load": {"status": "loaded" if ready else "loading",
                                                            "path": "/" + self.scenes[session]}}}
                else:
                    value = {"capture_id": query["arguments"]["capture_id"], "terminal": ready,
                             "status": "completed" if ready else "pending"}
                return {"id": identity, "status": "ready", "revision": 2 if ready else 1,
                        "value": value, "error": None}
            raise ValueError(method)

    def _ready(self, query):
        return self.scene_ready if query["operation"] == "infernux.project.info" else self.capture_ready

    def wait_for(self, predicate, timeout=4):
        with self.condition:
            return self.condition.wait_for(predicate, timeout=timeout)

    def close(self):
        for gate in (self.creation_gate, self.capture_gate):
            if gate is not None:
                gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@asynccontextmanager
async def running_service(project):
    channel = socket.socket()
    channel.bind(("127.0.0.1", 0))
    port = channel.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_server(project).http_app(path="/mcp"),
                                            log_level="critical", lifespan="on"))
    thread = Thread(target=server.run, kwargs={"sockets": [channel]}, daemon=True)
    thread.start()
    deadline = monotonic() + 5
    while not server.started and thread.is_alive() and monotonic() < deadline:
        await asyncio.sleep(0.01)
    if not server.started:
        raise RuntimeError("MCP HTTP service did not start")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 8)
        channel.close()
        if thread.is_alive():
            raise RuntimeError("MCP HTTP service did not stop")


class TaskSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.engine = EngineEndpoint(self.folder.name)

    async def asyncTearDown(self):
        await asyncio.to_thread(self.engine.close)
        self.folder.cleanup()

    async def engine_wait(self, predicate):
        self.assertTrue(await asyncio.to_thread(self.engine.wait_for, predicate), self.engine.calls)

    async def task_status(self, client, identity, expected):
        deadline = monotonic() + 4
        while monotonic() < deadline:
            status = await client.get_task_status(identity)
            if status.status == expected:
                return status
            await asyncio.sleep(0.01)
        self.fail(f"Task {identity}: expected {expected}, got {status.status}")

    async def test_background_recipe_uses_the_normal_session_and_releases_queries(self):
        async with running_service(self.folder.name) as endpoint:
            async with Client(endpoint) as client:
                normal = (await client.call_tool("engine_status")).data["session_id"]
                task = await client.call_tool("load_and_capture", {"asset_guid": "scene"}, task=True)
                self.assertFalse(task._is_immediate)
                self.assertEqual((await task.result()).data["status"], "completed")
                self.assertEqual({session for session, _, _ in self.engine.calls}, {normal})
                self.assertEqual(self.engine.queries, {})
            await self.engine_wait(lambda: normal in self.engine.closed_sessions)
        self.assertEqual(sum(method == "call" and args["operation"] == "infernux.scene.open"
                             for _, method, args in self.engine.calls), 1)

    async def test_two_clients_keep_distinct_task_owners_and_disconnect_stops_later_steps(self):
        self.engine.scene_ready = False
        async with running_service(self.folder.name) as endpoint:
            first = Client(endpoint)
            async with Client(endpoint) as second:
                async with first:
                    one = (await first.call_tool("engine_status")).data["session_id"]
                    two = (await second.call_tool("engine_status")).data["session_id"]
                    self.assertNotEqual(one, two)
                    abandoned = await first.call_tool("load_and_capture", {"asset_guid": "one"}, task=True)
                    other = await second.call_tool("load_and_capture", {"asset_guid": "two"}, task=True)
                    await self.engine_wait(lambda: len(self.engine.queries) == 2)
                await self.engine_wait(lambda: one in self.engine.closed_sessions
                                       and all(q["session"] == two for q in self.engine.queries.values()))
                with self.engine.condition:
                    self.engine.scene_ready = True
                    self.engine.condition.notify_all()
                self.assertEqual((await other.result()).data["status"], "completed")
                await self.task_status(second, abandoned.task_id, "failed")
                self.assertEqual([capture["session"] for capture in self.engine.captures.values()], [two])
                self.assertNotIn(two, self.engine.closed_sessions)
                self.assertEqual(self.engine.queries, {})
            await self.engine_wait(lambda: two in self.engine.closed_sessions)

    async def test_cancelling_a_waiting_capture_releases_query_and_capture_without_replay(self):
        self.engine.capture_ready = False
        async with running_service(self.folder.name) as endpoint:
            async with Client(endpoint) as client:
                task = await client.call_tool("load_and_capture", {"asset_guid": "scene"}, task=True)
                await self.engine_wait(lambda: bool(self.engine.captures) and any(
                    q["operation"] == "infernux.capture.status" for q in self.engine.queries.values()))
                status = await client.cancel_task(task.task_id)
                self.assertEqual(status.status, "cancelled")
                await self.engine_wait(lambda: not self.engine.queries and all(
                    capture["cancelled"] for capture in self.engine.captures.values()))
                self.assertEqual(len(self.engine.captures), 1)
                self.assertEqual(len(self.engine.scenes), 1)  # Scene open was applied, not rolled back.
                await self.task_status(client, task.task_id, "cancelled")

    async def test_cancelling_while_query_handle_is_in_flight_releases_the_returned_handle(self):
        self.engine.creation_gate = Event()
        async with running_service(self.folder.name) as endpoint:
            async with Client(endpoint) as client:
                task = await client.call_tool("load_and_capture", {"asset_guid": "scene"}, task=True)
                await self.engine_wait(lambda: any(method == "query_start" for _, method, _ in self.engine.calls))
                self.assertEqual((await client.cancel_task(task.task_id)).status, "cancelled")
                self.engine.creation_gate.set()
                await self.engine_wait(lambda: any(method == "query_stop" for _, method, _ in self.engine.calls))
                self.assertEqual(self.engine.queries, {})
                self.assertEqual(self.engine.captures, {})

    async def test_cancelling_while_capture_handle_is_in_flight_cancels_that_capture(self):
        self.engine.capture_gate = Event()
        async with running_service(self.folder.name) as endpoint:
            async with Client(endpoint) as client:
                task = await client.call_tool("load_and_capture", {"asset_guid": "scene"}, task=True)
                await self.engine_wait(lambda: any(method == "call" and args["operation"] == "infernux.capture.request"
                                                  for _, method, args in self.engine.calls))
                self.assertEqual((await client.cancel_task(task.task_id)).status, "cancelled")
                self.engine.capture_gate.set()
                await self.engine_wait(lambda: bool(self.engine.captures) and all(
                    capture["cancelled"] for capture in self.engine.captures.values()))
                self.assertEqual(len(self.engine.captures), 1)
                self.assertEqual(self.engine.queries, {})

    async def test_task_only_session_renews_its_observation_lease(self):
        self.engine.scene_ready = False
        async with running_service(self.folder.name) as endpoint:
            async with Client(endpoint) as client:
                task = await client.call_tool("load_and_capture", {"asset_guid": "long-scene", "timeout": 45}, task=True)
                renewed = await asyncio.to_thread(self.engine.wait_for,
                                                  lambda: any(method == "session_ping" for _, method, _ in self.engine.calls), 30)
                self.assertTrue(renewed, self.engine.calls)
                sessions = {session for session, _, _ in self.engine.calls}
                self.assertEqual(len(sessions), 1)
                self.assertNotIn("service", sessions)
                with self.engine.condition:
                    self.engine.scene_ready = True
                    self.engine.condition.notify_all()
                self.assertEqual((await task.result()).data["status"], "completed")
                self.assertEqual(self.engine.queries, {})


if __name__ == "__main__":
    unittest.main()
