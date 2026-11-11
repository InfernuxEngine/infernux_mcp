"""Local Python automation runs on the existing editor owner."""

from infernux.host import EditorAutomationHost, Operation, OperationKind

from .operation_support import on_editor, operation


def build_python_operations(project_path: str) -> tuple[Operation, ...]:
    def execute(code: str):
        def apply():
            import infernux as inx

            namespace = {"inx": inx, "host": EditorAutomationHost.instance(),
                         "project_root": project_path, "__name__": "__mcp__"}
            exec(compile(code, "<MCP Python>", "exec"), namespace)
            return {"result": namespace.get("result")}
        return on_editor("infernux.editor.python.execute", apply)

    return (operation(
        "infernux.editor.python.execute", OperationKind.COMMAND,
        "Execute custom Python on the editor owner. The namespace contains inx, host and project_root. Assign a JSON-serializable result to return it; exceptions retain the script traceback. GPU author files remain statically compiled by shader.check.",
        execute, capability="editor.write", input_properties={"code": {"type": "string"}},
        required=("code",), side_effects=("Runs the supplied Python against the live project and engine.",),
        tags=("python", "compute", "jit", "automation"),
    ),)
