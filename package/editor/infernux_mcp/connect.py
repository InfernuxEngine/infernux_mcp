"""Generate/merge native MCP configuration, touching only the named server."""
import argparse
from datetime import date, time
import json
import math
from pathlib import Path
import sys
import subprocess


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return ("nan" if math.isnan(value) else "inf" if value > 0 else "-inf") if not math.isfinite(value) else repr(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(key, ensure_ascii=False) + " = " + _toml_value(item)
                                  for key, item in value.items()) + " }"
    raise TypeError(f"Unsupported TOML value: {type(value).__name__}")


def _toml_document(document):
    lines = []
    def table(values, path=()):
        if path:
            lines.extend(("", "[" + ".".join(json.dumps(part, ensure_ascii=False) for part in path) + "]"))
        for key, value in values.items():
            if not isinstance(value, dict):
                lines.append(json.dumps(key, ensure_ascii=False) + " = " + _toml_value(value))
        for key, value in values.items():
            if isinstance(value, dict):
                table(value, (*path, key))
    table(document)
    return "\n".join(lines).lstrip("\n") + "\n"


def configuration(client, project, name="infernux"):
    import infernux_automation
    module_root = str(Path(__file__).resolve().parent.parent)
    sdk_root = str(Path(infernux_automation.__file__).resolve().parent.parent)
    import os
    entry = {"command": sys.executable, "args": ["-m", "infernux_mcp", "--project", str(Path(project).resolve())],
             "env": {"PYTHONPATH": os.pathsep.join([module_root, sdk_root])}}
    if client == "codex":
        return {"mcp_servers": {name: entry}}
    if client == "dsh":
        return [{"id": "mcp-" + name, "name": "@deepseek-ai/dsh-mcp-client",
                 "config": {"serverName": name, "transport": "stdio", **entry}}]
    return {"mcpServers": {name: {"type": "stdio", **entry}}}


def merge_configuration(path, client, project, name="infernux"):
    path = Path(path)
    value = configuration(client, project, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if client == "codex":
        import tomllib
        current = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        # Parse real keys: a quoted name containing a dot is one server, and an
        # inline server table has exactly the same identity as a section table.
        current.setdefault("mcp_servers", {}).update(value["mcp_servers"])
        text = _toml_document(current)
        tomllib.loads(text)
        path.write_text(text, encoding="utf-8")
    elif client == "dsh":
        import yaml
        from yaml.nodes import MappingNode, ScalarNode, SequenceNode
        # DSH's documented !!js values are YAML nodes, never Python objects or
        # executable expressions here. Serialize the untouched tagged nodes.
        current = yaml.compose(path.read_text(encoding="utf-8"), Loader=yaml.SafeLoader) if path.exists() else None
        current = current or SequenceNode("tag:yaml.org,2002:seq", [])
        if not isinstance(current, SequenceNode):
            raise ValueError("DSH plugin configuration must be a YAML list")
        def ours(node):
            return isinstance(node, MappingNode) and any(
                isinstance(key, ScalarNode) and key.value == "id"
                and isinstance(item, ScalarNode) and item.value == "mcp-" + name
                for key, item in node.value)
        current.value[:] = [node for node in current.value if not ours(node)]
        current.value.extend(yaml.compose(yaml.safe_dump(value), Loader=yaml.SafeLoader).value)
        path.write_text(yaml.serialize(current, Dumper=yaml.SafeDumper, allow_unicode=True), encoding="utf-8")
    else:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if client == "workbuddy" and set(current.get("mcpServers", {})) - {name}:
            raise ValueError("WorkBuddy uses only the first server in a Connector's mcp.json. "
                             "Use a separate Infernux Connector file; the existing configuration was not changed.")
        current.setdefault("mcpServers", {}).update(value["mcpServers"])
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def merge_claude_hook(path, project):
    import infernux_automation
    path = Path(path)
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    script = Path(infernux_automation.__file__).parent / "claude_hook.py"
    command = subprocess.list2cmdline([sys.executable, str(script), "--project", str(Path(project).resolve())])
    hooks = current.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    # Only replace our own hook; unrelated user hooks and settings stay intact.
    hooks[:] = [entry for entry in hooks if not any("infernux_automation" in hook.get("command", "")
                                                    for hook in entry.get("hooks", []))]
    hooks.append({"hooks": [{"type": "command", "command": command}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=("claude", "codex", "dsh", "workbuddy"), required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--name", default="infernux")
    parser.add_argument("--output", help="Client config file; WorkBuddy requires a separate Connector's mcp.json. "
                                        "Omit to print a reusable declaration.")
    parser.add_argument("--hook-output", help="Optional Claude settings.json for actual UserPromptSubmit report delivery.")
    args = parser.parse_args()
    if args.hook_output:
        if args.client != "claude":
            parser.error("--hook-output uses Claude's public UserPromptSubmit hook")
        merge_claude_hook(args.hook_output, args.project)
    try:
        value = merge_configuration(args.output, args.client, args.project, args.name) if args.output else configuration(args.client, args.project, args.name)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
