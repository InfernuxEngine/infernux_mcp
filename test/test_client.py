"""Client-only regression tests; no Editor, GPU, or third-party packages needed."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "package" / "editor"))
from infernux_mcp import client


class FakeClient:
    def __init__(self, replies=None):
        self.replies = iter(replies or [])
        self.calls = []
        self.entries = 0
        self.exits = 0

    async def __aenter__(self):
        self.entries += 1
        return self

    async def __aexit__(self, *args):
        self.exits += 1

    async def call_tool(self, tool, arguments):
        self.calls.append((tool, arguments))
        value = next(self.replies, {"ok": True, "data": {}})
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(data=value)

    async def list_tools(self):
        return []


class ClientTests(unittest.TestCase):
    def run_cli(self, argv, replies=None, stdin=""):
        fake = FakeClient(replies)
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(client, "create_loopback_client", return_value=fake) as factory, \
             patch.object(sys, "stdin", io.StringIO(stdin)), \
             patch.object(sys, "stdout", output), patch.object(sys, "stderr", errors):
            status = client.main(argv)
        return status, output.getvalue(), errors.getvalue(), fake, factory

    def test_success_preserves_gateway_envelope(self):
        result = {"ok": True, "data": {"result": {"ok": False}}}
        code, out, _, fake, _ = self.run_cli(["call", "operation_query_execute"], [result])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), result)
        self.assertEqual((fake.entries, fake.exits), (1, 1))

    def test_operation_errors_exit_nonzero(self):
        result = {"ok": False, "error": {"code": "operation.permission_denied"}}
        code, out, _, _, _ = self.run_cli(["call", "operation_command_execute"], [result])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), result)

    def test_partial_batch_failure_exits_nonzero_without_losing_results(self):
        result = {"ok": True, "data": {"results": [{"ok": True}, {"ok": False}]}}
        code, out, _, _, _ = self.run_cli(["call", "operation_batch_execute"], [result])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), result)

    def test_job_failure_is_detected_only_for_job_status(self):
        result = {"ok": True, "data": {"done": True, "ok": False, "error": {"code": "operation.failed"}}}
        self.assertTrue(client._failed(result, tool="operation_job_status"))
        self.assertFalse(client._failed(result, tool="some.other.tool"))

    def test_json_stdin_arguments(self):
        code, _, _, fake, _ = self.run_cli(
            ["call", "operation_command_execute", "--args-file", "-"],
            stdin='{"operation":"create","arguments":{"name":"风帆 \\"A\\""}}',
        )
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls[0][1]["arguments"]["name"], '风帆 "A"')

    def test_file_arguments(self):
        with patch.object(Path, "read_text", return_value='{"limit": 20}') as read:
            code, _, _, fake, _ = self.run_cli(["call", "operation_schema_list", "--args-file", "args.json"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [("operation_schema_list", {"limit": 20})])
        read.assert_called_once_with(encoding="utf-8")

    def test_bad_arguments_fail_before_connecting(self):
        for payload in ("[1]", "{"):
            code, out, err, fake, factory = self.run_cli(["call", "tool", "--args", payload])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertFalse(json.loads(err)["ok"])
            factory.assert_not_called()
            self.assertEqual(fake.calls, [])

    def test_session_reuses_one_connection_and_preserves_ids(self):
        lines = '\n'.join(json.dumps(row) for row in [
            {"id": "inspect", "tool": "operation_query_execute", "arguments": {"operation": "read"}},
            {"id": 2, "tool": "operation_schema_list"},
        ])
        code, out, _, fake, factory = self.run_cli(["session"], stdin=lines)
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(row)["id"] for row in out.splitlines()], ["inspect", 2])
        self.assertEqual(fake.entries, 1)
        self.assertEqual(fake.exits, 1)
        self.assertEqual(len(fake.calls), 2)
        factory.assert_called_once()

    def test_session_errors_do_not_replay_or_drop_later_input(self):
        lines = '\n'.join([
            '{', '[]', '{"id": 1, "tool": "first"}',
            '{"id": 2, "tool": "second"}',
        ])
        code, out, _, fake, _ = self.run_cli(["session"], [TimeoutError(), {"ok": True}], lines)
        rows = [json.loads(row) for row in out.splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[2]["result"]["error"]["phase"], "request")
        self.assertIn("does not replay", rows[2]["result"]["error"]["hint"])
        self.assertTrue(rows[3]["result"]["ok"])
        self.assertEqual(fake.calls, [("first", {}), ("second", {})])

    def test_session_rejects_invalid_fields_before_dispatch(self):
        lines = '\n'.join(json.dumps(row) for row in [
            {"id": [], "tool": "tool"}, {"tool": "tool", "arguments": []},
            {"tool": "tool", "args": {}}, {"tool": 1},
        ])
        code, out, _, fake, _ = self.run_cli(["session"], stdin=lines)
        self.assertEqual(code, 1)
        self.assertEqual(len(out.splitlines()), 4)
        self.assertEqual(fake.calls, [])

    def test_timing_records_are_separate_and_contain_no_arguments(self):
        code, out, err, _, _ = self.run_cli(["--timings", "call", "tool", "--args", '{"private":"value"}'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])
        rows = [json.loads(row) for row in err.splitlines()]
        self.assertEqual({row["phase"] for row in rows},
                         {"client_create", "connect_initialize", "request", "disconnect", "serialize", "total"})
        self.assertNotIn("private", err)
        self.assertNotIn("value", err)
        self.assertTrue(all(row["elapsed_ms"] >= 0 for row in rows))

    def test_timeout_must_be_finite_and_positive(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit):
                client.main(["--timeout", value, "session"])

    def test_remote_endpoints_still_rejected_before_importing_transport(self):
        for endpoint in ("https://127.0.0.1/mcp", "http://example.com/mcp", "http://127.0.0.1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                client.create_loopback_client(endpoint)


if __name__ == "__main__":
    unittest.main()
