"""Regression coverage for a modeless local toolbox.

Settings tests run without the engine. Host integration tests additionally run
when the interpreter has Infernux installed; they use the real operation registry
and replace only the native Editor boundary.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "package" / "editor"))
from infernux_mcp import settings


class SettingsTests(unittest.TestCase):
    def test_defaults_ignore_old_config_and_have_no_permission_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "ProjectSettings").mkdir()
            old = project / "ProjectSettings/mcp_capabilities.json"
            old.write_text('{"enabled":false,"profile":"invalid","granted_capabilities":[]}', encoding="utf-8")
            config = settings.configure(directory)
            self.assertTrue(config["enabled"])
            self.assertEqual(set(config), {"enabled", "write_default_config_on_bootstrap", "features", "session", "limits"})
            self.assertEqual(settings.load_config(directory), config)
            self.assertTrue((project / "ProjectSettings/mcp_settings.json").is_file())
            self.assertFalse(json.loads(old.read_text())["enabled"])

    def test_no_old_policy_fields_are_accepted_in_new_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            for config in ({"profile": "developer_assist"}, {"granted_capabilities": []},
                           {"session": {"allowed_project_roots": []}},
                           {"session": {"whl_readonly_source": []}}):
                with self.subTest(config=config), self.assertRaises(ValueError):
                    settings.apply_config(directory, config)

    def test_limits_and_features_round_trip_and_copies_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings.apply_config(directory, {"limits": {"batch_max_steps": 7}, "features": {"trace_recorder": False}})
            settings.save_config(config, directory)
            config["limits"]["batch_max_steps"] = 99
            self.assertEqual(settings.load_config(directory)["limits"]["batch_max_steps"], 7)
            self.assertFalse(settings.feature_enabled("trace_recorder"))


class GatewayRecorder:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name):
        def register(fn):
            self.tools[name] = fn.__wrapped__ if inspect.iscoroutinefunction(fn) else fn
            return fn
        return register


@unittest.skipUnless(importlib.util.find_spec("infernux"), "requires an installed Infernux host")
class HostToolboxTests(unittest.TestCase):
    def setUp(self):
        from infernux_mcp import adapter
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        (self.project / "Assets").mkdir()
        recorder = GatewayRecorder()
        adapter.register_gateways(recorder, str(self.project), {"features": {"trace_recorder": False}})
        self.addCleanup(adapter.shutdown_adapter)
        self.tools = recorder.tools

    def test_direct_batch_and_job_execute_new_categories_without_configuration(self):
        from infernux.host import OperationKind
        from infernux_mcp import adapter
        from infernux_mcp.operation_support import operation
        calls = []
        name = "test.toolbox.dynamic"
        adapter._registry.register(operation(name, OperationKind.COMMAND, "Dynamic plugin tool",
            lambda value: calls.append(value) or {"value": value}, capability="new-plugin.custom.write",
            input_properties={"value": {"type": "integer"}}, required=("value",)))
        self.assertTrue(self.tools["operation_command_execute"](name, {"value": 1})["ok"])
        batch = self.tools["operation_batch_execute"]([{"operation": name, "arguments": {"value": 2}}])
        self.assertTrue(batch["data"]["results"][0]["ok"])
        job = self.tools["operation_job_submit"](name, {"value": 3})["data"]["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = self.tools["operation_job_status"](job)["data"]
            if status["done"]:
                break
            time.sleep(.01)
        self.assertTrue(status["done"])
        self.assertEqual(status["result"], {"value": 3})
        self.assertEqual(calls, [1, 2, 3])
        invalid = self.tools["operation_command_execute"](name, {"value": "wrong"})
        self.assertFalse(invalid["ok"])
        self.assertEqual(calls, [1, 2, 3])

    def test_discovery_and_session_have_no_permission_or_mode_contract(self):
        status = self.tools["host_session_status"]()["data"]["session"]
        self.assertNotIn("mode", status)
        self.assertNotIn("supervisor_lease_configured", status)
        capabilities = self.tools["host_capabilities"]()["data"]
        self.assertNotIn("granted", capabilities)
        self.assertNotIn("blocked_operations", capabilities)
        self.assertTrue(capabilities["capabilities"])
        from infernux_mcp import session
        from infernux_mcp.supervisor import SupervisorSession
        self.assertFalse(hasattr(session, "require_mode"))
        self.assertFalse(hasattr(SupervisorSession, "switch_mode"))

    def test_input_ui_and_capture_share_the_authoring_session(self):
        from types import SimpleNamespace
        from infernux.host import EditorAutomationHost
        from infernux_mcp import capture_operations, input_operations, session, ui_operations
        session.current().build_profile = "release_exploration"  # metadata cannot restrict tools
        host = SimpleNamespace(
            input_status=lambda: {"last_processed_sequence": 4},
            queue_input=lambda kind, **kw: {"sequence": 4},
            semantic_snapshot=lambda: {"frame": 9},
            request_capture=lambda source, output, camera: 12,
            capture_status=lambda capture_id: {"status": "completed", "output_path": str(self.project / "image.png")},
        )
        with patch.object(EditorAutomationHost, "instance", return_value=host), \
             patch.object(input_operations, "on_editor", side_effect=lambda name, fn: fn()), \
             patch.object(ui_operations, "on_editor", side_effect=lambda name, fn: fn()), \
             patch.object(capture_operations, "on_editor", side_effect=lambda name, fn: fn()):
            self.assertTrue(self.tools["operation_command_execute"]("infernux.input.key", {"key": "W", "pressed": True})["ok"])
            self.assertTrue(self.tools["operation_query_execute"]("infernux.ui.semantic.status", {})["ok"])
            result = self.tools["operation_command_execute"]("infernux.capture.request", {})
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["data"]["result"]["output_path"].endswith(".png"))
            self.assertNotIn("human_review_only", result["data"]["result"])
            self.assertEqual(capture_operations._capture_status(12)["output_path"], str(self.project / "image.png"))

    def test_shutdown_uses_public_target_identity_without_supervisor_launch(self):
        from types import SimpleNamespace
        from infernux.host import EditorAutomationHost
        from infernux_mcp import operations, session
        closed = []
        host = SimpleNamespace(request_editor_close=lambda: closed.append(True))
        with patch.object(EditorAutomationHost, "instance", return_value=host), \
             patch.object(operations, "on_editor", side_effect=lambda name, fn: fn()):
            wrong = self.tools["operation_command_execute"]("infernux.mcp.supervisor.shutdown", {"editor_instance_id": "stale"})
            self.assertFalse(wrong["ok"])
            self.assertFalse(closed)
            result = self.tools["operation_command_execute"]("infernux.mcp.supervisor.shutdown", {"editor_instance_id": session.current().editor_instance_id})
            self.assertTrue(result["ok"], result)
            self.assertEqual(closed, [True])

    def test_supervisor_round_trip_does_not_reintroduce_modes_or_leases(self):
        from infernux_mcp.supervisor import SupervisorSession
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
            supervisor = SupervisorSession(str(self.project), mcp_port=port)
            status = supervisor.prepare_project()
            self.assertNotIn("mode", status)
            self.assertNotIn("mode", status["agent_handoff"])
            self.assertTrue((self.project / "ProjectSettings/mcp_settings.json").is_file())
            persisted = json.loads(Path(supervisor.state_path).read_text(encoding="utf-8"))
            self.assertNotIn("supervisor_lease", persisted)
            resumed = SupervisorSession.resume(str(self.project), supervisor.session_id)
            self.assertEqual(resumed.mcp_port, port)
            self.assertEqual(resumed.session_id, supervisor.session_id)


if __name__ == "__main__":
    unittest.main()
