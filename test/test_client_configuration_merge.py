"""Merge actual tagged DSH YAML and quoted/inline Codex TOML in temp files."""
from copy import deepcopy
import datetime
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

import yaml
from yaml.nodes import MappingNode, ScalarNode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "package/editor"))
from infernux_mcp.connect import configuration, merge_configuration


def node_value(node):
    if isinstance(node, ScalarNode):
        return node.tag, node.value
    if isinstance(node, MappingNode):
        return node.tag, tuple((node_value(key), node_value(value)) for key, value in node.value)
    return node.tag, tuple(node_value(value) for value in node.value)


def row_id(node):
    return next(value.value for key, value in node.value if key.value == "id")


class ClientConfigurationMergeTests(unittest.TestCase):
    def test_workbuddy_does_not_append_a_second_ignored_server_or_change_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mcp.json"
            for servers in ({"other": {"command": "other"}},
                            {"infernux": {"command": "old"}, "other": {"command": "other"}}):
                original = (json.dumps({"mcpServers": servers, "otherSetting": 7}, indent=4) + "\n").encode()
                path.write_bytes(original)
                with self.assertRaisesRegex(ValueError, "separate Infernux Connector file"):
                    merge_configuration(path, "workbuddy", folder)
                self.assertEqual(path.read_bytes(), original)

    def test_workbuddy_updates_its_single_connector_server(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mcp.json"
            path.write_text(json.dumps({"mcpServers": {"infernux": {"command": "old"}}, "otherSetting": 7}), encoding="utf-8")
            merge_configuration(path, "workbuddy", folder)
            merge_configuration(path, "workbuddy", folder)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             {**configuration("workbuddy", folder), "otherSetting": 7})

    def test_dsh_documented_js_tags_survive_adding_and_replacing_our_server(self):
        # The env form is from DSH's first-party mcp-client README. Nodes keep
        # both ordinary nested data and application tags without executing JS.
        example = """- id: mcp-github
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: github
    transport: stdio
    command: npx
    args: ['-y', '@modelcontextprotocol/server-github']
    env:
      GITHUB_TOKEN: !!js process.env.GITHUB_TOKEN
    custom:
      nested:
        - !dsh/expression {source: !!js process.env.OTHER, enabled: true}
"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plugins.yaml"
            path.write_text(example, encoding="utf-8")
            original = node_value(yaml.compose(example).value[0])
            for project in (folder, str(Path(folder) / "another-project")):
                merge_configuration(path, "dsh", project)
                result = yaml.compose(path.read_text(encoding="utf-8"), Loader=yaml.SafeLoader)
                self.assertEqual(len(result.value), 2)
                github = next(row for row in result.value if row_id(row) == "mcp-github")
                self.assertEqual(node_value(github), original)
                own = next(row for row in result.value if row_id(row) == "mcp-infernux")
                ordinary = yaml.safe_load(yaml.serialize(own))
                self.assertEqual(ordinary, configuration("dsh", project)[0])
            self.assertIn("!!js", path.read_text(encoding="utf-8"))

    def test_codex_exact_server_key_does_not_delete_a_quoted_dotted_name(self):
        text = '''model = "existing"
[mcp_servers."infernux.other"]
command = "other"
[mcp_servers."infernux.other".env]
TOKEN = "unrelated"
[mcp_servers.infernux]
command = "old"
[projects.'E:/Games/Project']
trust_level = "trusted"
'''
        self.check_codex_merge(text)

    def test_codex_inline_server_tables_and_other_value_types_keep_their_meaning(self):
        text = '''model = "existing"
settings = {enabled = true, weights = [1, 2.5], nested = {value = "你好"}}
timestamp = 2026-11-15T08:30:00+08:00
date = 2026-11-15
time = 08:30:00
limit = inf
[mcp_servers]
infernux = {command = "old", env = {OLD = "value"}}
other = {command = "other", args = ["--value", "literal"]}
[[rules]]
pattern = "one"
[[rules]]
pattern = "two"
'''
        result = self.check_codex_merge(text)
        self.assertEqual(result["timestamp"], datetime.datetime.fromisoformat("2026-11-15T08:30:00+08:00"))
        self.assertEqual(len(result["rules"]), 2)
        self.check_codex_merge('mcp_servers = {infernux = {command="old"}, other = {command="other"}}\n')

    def check_codex_merge(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text(text, encoding="utf-8")
            expected = deepcopy(tomllib.loads(text))
            expected.setdefault("mcp_servers", {}).update(configuration("codex", folder)["mcp_servers"])
            merge_configuration(path, "codex", folder)
            result = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result, expected)
            once = path.read_bytes()
            merge_configuration(path, "codex", folder)
            self.assertEqual(path.read_bytes(), once)
            return result


if __name__ == "__main__":
    unittest.main()
