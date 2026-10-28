# Local toolbox

The MCP endpoint binds to loopback. Every registered operation is available in the same session, without credentials, permission lists, operating modes or a separate Supervisor shutdown lease.

Calls validate operation identity, JSON argument types and actual engine state. Commands use the Editor's normal main-thread queue and undo behavior where supported. Observe an asynchronous operation's completion before using its result; a timeout does not imply rollback.

Captures read Scene, Game, Camera or composed Editor render targets and return a PNG path. Both agents and developers may read that file.

Editor shutdown uses the public Editor instance ID to identify the target. The engine's single-writer project lock and the exported Debug Player's process channel remain implementation details of process coordination; they do not configure access to MCP tools.

`ProjectSettings/mcp_settings.json` contains tracing, discovery, limits and session/build metadata. Old capability configs are neither read nor migrated. Host/Origin validation keeps the HTTP endpoint local.
