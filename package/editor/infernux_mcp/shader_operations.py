"""Static GPU source tools use the engine's preparation and diagnostic service."""

from infernux.host import Operation, OperationKind

from .operation_support import on_editor, operation


def build_shader_operations() -> tuple[Operation, ...]:
    return (
        operation(
            "infernux.shader.check", OperationKind.QUERY,
            "Check a GPU author file or unsaved source draft for SPIR-V and WGSL. Returns valid, logical schema and structured source diagnostics; does not execute Python source, publish assets or create pipelines.",
            _check, capability="asset.read",
            input_properties={"asset": {"type": "string"}, "source": {"type": ["string", "null"], "default": None}},
            required=("asset",), tags=("shader", "slang", "diagnostics", "compile"),
        ),
        operation(
            "infernux.shader.schema", OperationKind.QUERY,
            "Read the native GPU asset's logical parameters and input ports by asset GUID or path.",
            _schema, capability="asset.read", input_properties={"asset": {"type": "string"}},
            required=("asset",), tags=("shader", "schema", "parameters"),
        ),
        operation(
            "infernux.shader.reload", OperationKind.COMMAND,
            "Prepare and publish an edited Shader or ShaderLib and its affected consumers through the engine owner.",
            _reload, capability="asset.write", input_properties={"asset": {"type": "string"}},
            required=("asset",), side_effects=("Reimports and publishes the affected GPU programs.",),
            tags=("shader", "shaderlib", "reload"),
        ),
    )


def _check(asset: str, source: str | None = None):
    from infernux import Shader
    return on_editor("infernux.shader.check", lambda: Shader.check(asset, source=source))


def _schema(asset: str):
    from infernux import Shader
    return on_editor("infernux.shader.schema", lambda: Shader.get_schema(asset))


def _reload(asset: str):
    from infernux import Shader
    return on_editor("infernux.shader.reload", lambda: {"published": Shader.reload(asset)})
