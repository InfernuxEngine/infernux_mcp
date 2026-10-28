# 0.2.0 — Unrestricted local toolbox (Infernux >=0.4.3)

- Remove MCP capability grants, developer/validation modes, mode-switch restarts and shutdown leases.
- Make authoring, input injection, semantic UI, capture and Player tools available in the same session.
- Replace the old capability configuration with `ProjectSettings/mcp_settings.json`; no migration or old API aliases.
- Use public Editor instance identity for normal shutdown; retain ordinary schema validation and process coordination.
- Return render-target PNG paths for direct agent and developer inspection.
- Keep discovery, batching, asynchronous jobs, traces, checkpoints and engine undo integration.

# 0.1.20 — Infernux 0.4.2

- Harden the local MCP transport and simplify the standalone client lifecycle.
- Serialize supervisor Player lifecycle operations.
- Drop bearer authentication and grant all capabilities by default; grant the
  loopback-only server and keep the Host/Origin checks.
- The package content changed under the same version after 0.1.19, so the
  version is bumped to keep the package cache immutable.

# 0.1.19 — Infernux 0.4.2

- Publish the editor transport and capability policy for Infernux 0.4.2.
- Preserve persistent client sessions, virtual-environment launchers and deterministic package text identities.
- Retain the engine operation host as the shared editor control boundary.


- Declare Infernux >=0.4.2 compatibility.
