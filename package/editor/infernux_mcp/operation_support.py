"""MCP-owned operation helpers built on the engine Host contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from infernux.host import Operation, OperationKind
from infernux.host.operation_support import (
    active_scene,
    asset_database,
    asset_identity,
    asset_path,
    component,
    components,
    game_object,
    interaction_core,
    on_editor,
    operation as _engine_operation,
    plugin_manager,
    serializable_component,
    set_json_pointer,
)


OWNER = "infernux/mcp"


def operation(
    operation_id: str,
    kind: OperationKind,
    summary: str,
    handler: Callable[..., Any],
    *,
    capability: str,
    input_properties: Mapping[str, object] | None = None,
    required: tuple[str, ...] = (),
    side_effects: tuple[str, ...] = (),
    reversible: bool = False,
    tags: tuple[str, ...] = (),
) -> Operation:
    return _engine_operation(
        operation_id,
        kind,
        summary,
        handler,
        capability=capability,
        input_properties=input_properties,
        required=required,
        side_effects=side_effects,
        reversible=reversible,
        tags=tags,
        owner=OWNER,
        thread="mixed",
        availability=("editor",),
    )


__all__ = [
    "OWNER",
    "active_scene",
    "asset_database",
    "asset_identity",
    "asset_path",
    "component",
    "components",
    "game_object",
    "interaction_core",
    "on_editor",
    "operation",
    "plugin_manager",
    "serializable_component",
    "set_json_pointer",
]
