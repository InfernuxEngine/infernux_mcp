"""Independent MCP protocol, adapter formats and unavailable-engine behavior."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "package/editor"))
from infernux_mcp.connect import configuration, merge_configuration, merge_claude_hook
from infernux_mcp.server import create_server
from fastmcp import Client


class Engine:
    def __init__(self):
        self.calls = []
    def request(self, method, **arguments):
        self.calls.append((method, arguments))
        return {"info": {"instance_id": "engine"}, "reports": [],
                "operations": {"revision": 4, "operations": [{"id": "test.value.read", "kind": "query", "summary": "Read", "tags": [], "input_schema": {}}]},
                "schema": {"id": "test.value.read"}, "call": {"answer": 7}, "job_submit": {"job_id": "owned-job"},
                "job_status": {"done": False}, "job_cancel": False,
                "query_start": {"id": "observation"}, "report_start": {"remaining": 3}}[method]

    def close(self):
        pass


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_child_without_native_engine(self):
        from fastmcp.client.transports import StdioTransport
        entry = configuration("claude", "/no-engine")["mcpServers"]["infernux"]
        async with Client(StdioTransport(command=entry["command"], args=entry["args"], env=entry["env"])) as client:
            result = await client.call_tool("help")
            self.assertIn("Automation SDK", result.data["architecture"])

    async def test_protocol_help_without_engine_and_tool_discovery(self):
        async with Client(create_server("/no-engine")) as client:
            tools = await client.list_tools()
            self.assertEqual(len(tools), 15)
            result = await client.call_tool("help", {})
            self.assertIn("Automation SDK", result.data["architecture"])
            resources = await client.list_resources()
            self.assertTrue(any(str(resource.uri) == "infernux://catalog" for resource in resources))
        self.assertNotIn("infernux", sys.modules)

    async def test_public_requests_keep_engine_job_and_query_identity(self):
        engine = Engine()
        with patch("infernux_mcp.server.AutomationClient", return_value=engine):
            async with Client(create_server("/project")) as client:
                search = await client.call_tool("operation_search", {"query": "value"})
                self.assertNotIn("input_schema", search.data["operations"][0])
                result = await client.call_tool("operation_call", {"operation": "test.value.read", "arguments": {}})
                self.assertEqual(result.data, {"answer": 7})
                job = await client.call_tool("operation_submit", {"operation": "test.long.work", "arguments": {}})
                self.assertEqual(job.data["job_id"], "owned-job")
                watch = await client.call_tool("observe", {"operation": "test.value.read", "arguments": {}, "consumer": "chat"})
                self.assertEqual(watch.data["observation"]["id"], "observation")
        self.assertEqual(sum(method == "call" for method, _ in engine.calls), 1)

    async def test_failed_world_write_is_not_replayed(self):
        engine = Engine()
        def request(method, **arguments):
            engine.calls.append((method, arguments))
            raise RuntimeError("unknown outcome")
        engine.request = request
        with patch("infernux_mcp.server.AutomationClient", return_value=engine):
            async with Client(create_server("/project")) as client:
                with self.assertRaises(Exception):
                    await client.call_tool("operation_call", {"operation": "test.write", "arguments": {}})
        self.assertEqual(len(engine.calls), 1)


class ConfigurationTests(unittest.TestCase):
    def test_claude_prompt_hook_is_explicit_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other"}]}]}, "existing": True}), encoding="utf-8")
            merge_claude_hook(path, folder)
            merge_claude_hook(path, folder)
            current = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(current["existing"])
            self.assertEqual(len(current["hooks"]["UserPromptSubmit"]), 2)
            self.assertIn("--project", current["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"])

    def test_native_formats_preserve_unrelated_settings_with_one_workbuddy_connector(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for client in ("claude", "codex", "dsh", "workbuddy"):
                value = configuration(client, str(folder))
                self.assertTrue(value)
                path = folder / (client + (".toml" if client == "codex" else ".yaml" if client == "dsh" else ".json"))
                if client == "codex":
                    path.write_text('model = "existing"\n[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8")
                elif client == "dsh":
                    path.write_text('- id: other\n  name: other-plugin\n', encoding="utf-8")
                elif client == "claude":
                    path.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}, "otherSetting": 7}), encoding="utf-8")
                else:
                    path.write_text(json.dumps({"otherSetting": 7}), encoding="utf-8")
                merge_configuration(path, client, str(folder))
                merge_configuration(path, client, str(folder))
                text = path.read_text(encoding="utf-8")
                self.assertIn("other", text)
                if client == "codex":
                    self.assertEqual(tomllib.loads(text)["model"], "existing")
                elif client != "dsh":
                    self.assertEqual(json.loads(text)["otherSetting"], 7)
                    if client == "workbuddy":
                        self.assertEqual(set(json.loads(text)["mcpServers"]), {"infernux"})


if __name__ == "__main__":
    unittest.main()
