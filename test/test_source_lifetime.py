"""Real independent processes stop old tools and release sessions on source events."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from time import monotonic
import unittest

import httpx
import infernux_automation
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from test_task_sessions import EngineEndpoint


@asynccontextmanager
async def stopped_service_client(connection, stopped):
    client = Client(connection, timeout=10, init_timeout=15)
    try:
        await client.__aenter__()
    except BaseException:
        await client.close()
        raise
    try:
        yield client
    finally:
        try:
            await client.__aexit__(*sys.exc_info())
        except httpx.TransportError:
            # The server intentionally closed its HTTP stream. Validate the
            # stopped tools and released resources in the body, independently
            # of the SDK's final DELETE/read on that already-closed transport.
            if not stopped.is_set():
                raise


class SourceLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, transport, changed_package, *, delete=False):
        with tempfile.TemporaryDirectory(prefix="infernux_mcp_source_events_") as folder:
            root = Path(folder).resolve()
            modules = root / "installed"
            source = Path(__file__).resolve().parents[1] / "package/editor/infernux_mcp"
            for name, origin in (("infernux_mcp", source),
                                 ("infernux_automation", Path(infernux_automation.__file__).parent)):
                shutil.copytree(origin, modules / name, ignore=shutil.ignore_patterns("__pycache__"))
            environment = dict(os.environ, PYTHONPATH=str(modules),
                               INFERNUX_NATIVE_MODULE_DIR=str(root / "no-native"))
            engine = EngineEndpoint(root / "project")
            engine.scene_ready = False
            log_path = root / "service.log"
            process = None
            try:
                with log_path.open("w", encoding="utf-8") as log:
                    arguments = ["-m", "infernux_mcp", "--project", str(root / "project")]
                    if transport == "stdio":
                        connection = StdioTransport(command=sys.executable, args=arguments,
                                                    env=environment, log_file=log)
                    else:
                        process = subprocess.Popen([sys.executable, *arguments, "--transport", "http"],
                                                   env=environment, stdin=subprocess.DEVNULL,
                                                   stdout=log, stderr=log,
                                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                        descriptor = root / "project/Cache/Automation/mcp.json"
                        deadline = monotonic() + 15
                        while not descriptor.is_file() and process.poll() is None and monotonic() < deadline:
                            await asyncio.sleep(0.01)
                        self.assertTrue(descriptor.is_file(), log_path.read_text(encoding="utf-8"))
                        connection = json.loads(descriptor.read_text(encoding="utf-8"))["endpoint"]
                    stopped = asyncio.Event()
                    async with stopped_service_client(connection, stopped) as client:
                        await client.call_tool("load_and_capture", {"asset_guid": "applied-scene"}, task=True)
                        self.assertTrue(await asyncio.to_thread(engine.wait_for, lambda: bool(engine.queries)))
                        original_sessions = {query["session"] for query in engine.queries.values()}
                        changed = modules / changed_package / ("server.py" if changed_package == "infernux_mcp" else "client.py")
                        if delete:
                            changed.unlink()
                        else:
                            changed.write_text("def broken(:\n", encoding="utf-8")
                        self.assertTrue(await asyncio.to_thread(engine.wait_for,
                                        lambda: original_sessions <= engine.closed_sessions and not engine.queries))
                        stopped.set()
                        with self.assertRaises(Exception):
                            await asyncio.wait_for(client.call_tool("operation_call", {
                                "operation": "infernux.scene.open", "arguments": {"asset_guid": "stale-tool"}}), 3)
                        # Opening the first scene was a real applied effect. Stop
                        # closes observations and prevents the old recipe's next step.
                        self.assertEqual(set(engine.scenes.values()), {"applied-scene"})
                        self.assertEqual(engine.captures, {})
                    if process is not None:
                        self.assertEqual(await asyncio.to_thread(process.wait, 5), 1)
                        self.assertFalse(descriptor.exists())
                self.assertIn("restart to load current code", log_path.read_text(encoding="utf-8"))
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    await asyncio.to_thread(process.wait, 5)
                await asyncio.to_thread(engine.close)

    async def test_stdio_stops_after_service_source_save(self):
        await self.exercise("stdio", "infernux_mcp")

    async def test_stdio_stops_after_sdk_source_deletion(self):
        await self.exercise("stdio", "infernux_automation", delete=True)

    async def test_http_stops_after_service_source_save(self):
        await self.exercise("http", "infernux_mcp")

    async def test_http_stops_after_sdk_source_deletion(self):
        await self.exercise("http", "infernux_automation", delete=True)


if __name__ == "__main__":
    unittest.main()
