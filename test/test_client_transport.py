"""Optional real HTTP transport tests: install package/requirements.txt first."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "package" / "editor"))
from infernux_mcp.client import create_loopback_client

try:
    from mcp.server.fastmcp import FastMCP
    import uvicorn
except ImportError:
    FastMCP = None


@unittest.skipUnless(FastMCP, "requires package/requirements.txt")
class ClientTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cancelled = threading.Event()
        mcp = FastMCP("Client Transport Test")

        @mcp.tool()
        def mcp_ping() -> dict[str, object]:
            return {"ok": True, "data": "pong"}

        @mcp.tool()
        async def wait_until_cancelled() -> dict[str, object]:
            try:
                await asyncio.sleep(30)
                return {"ok": True}
            finally:
                cls.cancelled.set()

        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.endpoint = f"http://127.0.0.1:{cls.sock.getsockname()[1]}/mcp"
        cls.server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 5
        while not cls.server.started and cls.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("Test MCP server did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.sock.close()
        if cls.thread.is_alive():
            raise RuntimeError("Test MCP server did not shut down")

    def cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "package" / "editor")
        return subprocess.Popen(
            [sys.executable, "-m", "infernux_mcp.client", "--endpoint", self.endpoint, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )

    def test_reuses_real_http_session(self):
        async def run():
            async with create_loopback_client(self.endpoint, timeout_seconds=2) as connection:
                for _ in range(3):
                    self.assertEqual((await connection.call_tool("mcp_ping", {})).data, {"ok": True, "data": "pong"})
        asyncio.run(run())

    def test_request_timeout_does_not_wait_indefinitely_and_server_receives_cancel(self):
        self.cancelled.clear()
        started = time.monotonic()
        with self.cli("--timeout", "0.2", "call", "wait_until_cancelled") as process:
            out, _ = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(json.loads(out)["error"]["phase"], "request")
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(self.cancelled.wait(2), "Server did not receive cancellation")

    @unittest.skipIf(os.name == "nt", "SIGINT subprocess test is POSIX-specific")
    def test_interrupt_while_stdin_idle_exits_without_waiting_for_eof(self):
        with self.cli("session") as process:
            try:
                process.stdin.write('{"id": 1, "tool": "mcp_ping"}\n')
                process.stdin.flush()
                received = []
                reader = threading.Thread(target=lambda: received.append(process.stdout.readline()), daemon=True)
                reader.start(); reader.join(timeout=5)
                self.assertFalse(reader.is_alive(), "Session did not flush its reply")
                self.assertTrue(json.loads(received[0])["result"]["ok"])
                process.send_signal(signal.SIGINT)
                self.assertEqual(process.wait(timeout=5), 130)
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
