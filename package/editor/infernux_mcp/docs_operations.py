"""Concise documentation operations for the Infernux MCP package."""

from __future__ import annotations

from infernux.host import Operation, OperationError, OperationKind, OperationRegistry

from infernux_mcp.operation_support import operation


_GUIDES = {
    "shader-authoring": {
        "title": "Static GPU author files and diagnostics",
        "body": (
            "GPU assets use .vert.py, .frag.py, .shadingmodel.py and .shaderlib.py. Import infernux.shader; "
            "native scalar/vector/matrix types and typed static library functions are compiled by Slang, never "
            "executed as host Python. NumPy, Torch and host imports do not belong in GPU author files. "
            "A shader.vert deforms VertexData; shader.frag returns raw mesh color; shader.surface fills SurfaceData; "
            "shader.shadingmodel evaluates light; shader.fullscreen processes graph images and buffers. "
            "Use shader_id for logical lookup, with slash-separated menu groups, and hidden=True for implementation "
            "assets. Materials select vertex and fragment independently; do not put vertex_shader in a fragment. "
            "Declare mesh values with shader.material and fullscreen values with shader.parameter. For example, "
            "smoothness = shader.material.float(default=0.5, range=(0.0, 1.0)) gives an Inspector slider. "
            "Use infernux.shader.check for a file or unsaved draft, repair source/range/code/phase diagnostics, then "
            "infernux.shader.reload to publish. The schema operation returns logical parameters and input ports. "
            "Slang reflection owns layouts and bindings; ordinary value changes do not recompile the shader. "
            "The current Deferred evaluator registers built-in PBR/Unlit/Toon; project lighting models use "
            "Forward/Forward+ until registered with a compatible GBuffer representation."
        ),
        "tags": ("shader", "slang", "shaderlib", "surface", "material", "diagnostics", "parameters"),
    },
    "shader-resources": {
        "title": "Typed shader graph resources",
        "body": (
            "A fullscreen author declares ports with shader.input(semantic, resource_type); the graph binds the "
            "field name through set_texture or set_buffer. Color2D, Depth2D, UInt2D and History2D describe 2D inputs; "
            "Color3D samples a volume. Integer images use load, not filtered sample. Color2DMS/Depth2DMS/UInt2DMS "
            "retain individual multisample values: use load(image, pixel, sample_index), dimensions(image) and "
            "sample_count(image). shader.multisample_input exposes sample_index for per-sample fragment invocation; "
            "ordinary fragment_input retains pixel-rate execution. Single-sample images use dimensions(image, mip). "
            "Buffer[shader.vec3] and other 32-bit float/int/uint scalar or vector types read tightly packed compute "
            "storage. Indexing reads an element; len reports the logical view count. graph.import_buffer accepts "
            "initialized GPU values and subviews without a hidden readback. Engine-owned offsets, descriptor "
            "alignment, producer dependencies and leases preserve the same allocation; do not pad vec3 to 16 bytes. "
            "Buffer ports are read-only in graphics; make writes in Compute. Host uploads/readbacks remain explicit."
        ),
        "tags": ("shader", "buffer", "compute", "texture", "volume", "multisample", "resources"),
    },
    "python-automation": {
        "title": "Custom editor Python",
        "body": "infernux.editor.python.execute runs code on the existing editor owner with inx, host and project_root. Assign a JSON-serializable result. Use the engine's compute/nn and asset APIs; it does not create another engine, thread owner, shader interpreter, or permission mode. Shader source itself goes through shader.check and is never exec'ed.",
        "tags": ("python", "compute", "jit", "automation"),
    },
    "discovery": {
        "title": "Discovering operations",
        "body": "Search schemas by task or domain, fetch the selected full schema, then invoke its stable dotted operation ID through the matching query, command, or workflow gateway.",
        "tags": ("schema", "search", "gateway", "operation"),
    },
    "assets": {
        "title": "Asset identity",
        "body": "Existing assets are addressed by GUID. Paths are contextual and are accepted only when selecting a new destination. Asset mutations use the editor's shared history and AssetDatabase.",
        "tags": ("asset", "guid", "path", "database"),
    },
    "validation": {
        "title": "Testing and observation",
        "body": "All local tools are available in the same session: edit scenes and scripts, inject input, observe semantic UI, capture render targets, and build or control a Debug Player. No permissions or MCP mode switches are needed. Input uses the engine event queue; captures return actual engine render-target PNG paths. Debug Player control requires an exported Player with the engine's control channel enabled. Supervisor can manage processes and optional checkpoint workflows.",
        "tags": ("validation", "supervisor", "capture", "input", "player"),
    },
    "authoring": {
        "title": "Authoring and Undo",
        "body": "Scene, component, material, particle, and asset commands enter authoritative editor services. Prefer GUID and object/component identities returned by queries instead of cached paths or display labels.",
        "tags": ("scene", "component", "material", "particle", "undo"),
    },
    "python-scripting": {
        "title": "Python transforms and component properties",
        "body": "Transform.rotation and local_rotation are quaternion values (quatf). For Euler angles in degrees, assign Vector3 to euler_angles or local_euler_angles. Before writing an unfamiliar component property, call infernux.scene.component.schema and use the declared field type, enum members, range, and readonly state.",
        "tags": ("python", "script", "transform", "rotation", "quaternion", "component", "schema"),
    },
    "player-build": {
        "title": "Building and validating a Player",
        "body": "Use infernux.player.build through operation_job_submit for a long-running build, then poll the job. A debug_feedback build can be launched with infernux.player.validation.launch and controlled, observed, captured, and shut down through the validation operations.",
        "tags": ("player", "build", "job", "validation", "debug_feedback"),
    },
}


def build_docs_operations() -> tuple[Operation, ...]:
    return (
        operation(
            "infernux.docs.search",
            OperationKind.QUERY,
            "Search concise Infernux MCP guides and registered operation schemas.",
            _search,
            capability="docs.read",
            input_properties={
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            required=("query",),
            tags=("docs", "help", "search", "schema"),
        ),
        operation(
            "infernux.docs.read",
            OperationKind.QUERY,
            "Read one concise Infernux MCP guide by ID.",
            _read,
            capability="docs.read",
            input_properties={"guide": {"type": "string"}},
            required=("guide",),
            tags=("docs", "help", "guide"),
        ),
    )


def _search(query: str, limit: int = 20):
    terms = [part.casefold() for part in str(query).split() if part]
    guides = []
    for guide_id, guide in _GUIDES.items():
        haystack = " ".join((guide_id, guide["title"], guide["body"], *guide["tags"])).casefold()
        if all(term in haystack for term in terms):
            guides.append({"id": guide_id, "title": guide["title"], "tags": list(guide["tags"])})
    schemas = OperationRegistry.instance().search(query, limit=max(1, min(int(limit), 100)))
    return {
        "guides": guides[: max(1, min(int(limit), 100))],
        "operations": list(schemas),
    }


def _read(guide: str):
    guide_id = str(guide).strip().casefold()
    value = _GUIDES.get(guide_id)
    if value is None:
        raise OperationError("docs.not_found", f"Unknown guide: {guide}")
    return {"id": guide_id, "title": value["title"], "body": value["body"], "tags": list(value["tags"])}


__all__ = ["build_docs_operations"]
