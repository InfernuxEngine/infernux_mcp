# Infernux MCP

[简体中文](README.zh-CN.md)

Requires an engine build containing the public Automation Host. The final minimum release is set when the engine changes are published.

A standard MCP service for Infernux. The editor owns scene editing, assets,
input, capture, builds and Player control. This plugin owns protocol transport,
operation discovery, resource projection, conversation delivery and reusable
workflows. Disabling MCP does not remove the engine's automation capabilities.

## Run and connect

Install `infernux/mcp` through the normal editor plugin manager. Enabling it
starts a separate loopback Streamable HTTP process. Its dynamically allocated
endpoint is in `<project>/Cache/Automation/mcp.json`; the engine's independent
endpoint is in `engine.json` beside it. There is no shared default port and no
permission mode or allow list.

Standard stdio is also supported:

```powershell
python -m infernux_mcp --project E:/Games/MyGame
```

The standalone environment needs `package/requirements.txt`, the plugin's
`package/editor` on `PYTHONPATH`, and the lightweight Automation SDK. The SDK
is included in the engine environment; outside it, install the engine source's
`python/` subproject. Importing the service or SDK does not load the native
engine or require a graphics device.

Generate a native client declaration, or merge it into a file you specify:

```powershell
python -m infernux_mcp.connect --client claude --project E:/Games/MyGame --output E:/Games/MyGame/.mcp.json
python -m infernux_mcp.connect --client codex --project E:/Games/MyGame --output <your-config.toml>
python -m infernux_mcp.connect --client dsh --project E:/Games/MyGame --output <your-plugins.yaml>
python -m infernux_mcp.connect --client workbuddy --project E:/Games/MyGame --output E:/Connectors/Infernux/mcp.json
```

Omitting `--output` prints the declaration. Claude, Codex and DSH merge only the
named `infernux` server; unrelated servers and client settings keep their values.
WorkBuddy output is the `mcp.json` of a separate Infernux Connector: its
[official Connector format](https://open.workbuddy.cn/en/docs/connector) uses only
the first server in that file. The generator refuses a WorkBuddy file containing
another server without changing it; a file containing only Infernux can be updated.
Merging
TOML or YAML normalizes formatting and does not preserve comments; DSH's tagged
values such as `!!js` retain their tags and contents without being executed. Client formats
follow the public [Claude MCP](https://code.claude.com/docs/en/mcp),
[Codex MCP](https://developers.openai.com/codex/mcp),
[DSH MCP client](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)
and [WorkBuddy connector](https://open.workbuddy.cn/en/docs/connector) interfaces.
The generator does not guess private client paths or read conversation databases.

## Use

1. Read `help` and `engine_status`.
2. Use `operation_search` to discover current operations, then `operation_schema`
   for the chosen operation. Install or retire an engine plugin and the catalog
   changes with its actual providers.
3. Invoke `operation_call`, or `operation_batch` for ordered calls. A batch stops
   at the first error; earlier world mutations are not undone.
4. Submit long work with `operation_submit`, retain its engine job ID and inspect
   it with `operation_job`. `operation_cancel` reports whether cancellation
   actually succeeded. A running command can already have changed the world.
5. `load_and_capture` opens a scene, waits for its loading signal, requests a
   native render-target capture and waits for the actual GPU completion signal.
   It supports standard MCP background tasks and a bounded timeout.

Resources: `infernux://catalog`, `infernux://operation/{operation}` and
`infernux://query/{query_id}`. Captures are engine output files under the project's
`Cache/Automation/artifacts/`, not desktop screenshots. Existing Host operations
also expose keyboard/pointer input and managed Debug Player processes.

Plugin install, compatible release discovery, overwrite updates, enable/disable,
uninstall and reload are public `infernux.plugin.*` operations. Editor themes use
`infernux.theme.list`, `.select` and `.refresh`. They delegate to the same engine
services as the editor UI and the SDK CLI. Updating without a tag selects the
highest compatible published release; an explicit tag or local source is also
supported. The returned activation/error/restart state describes the actual result.

## Signal queries and later conversation turns

`observe` subscribes a read operation to named signals, for example
`scene.changed`, `assets.changed`, `runtime.changed`, `console.changed`,
`capture.completed` or a signal from a game/plugin. Query evaluation is triggered
by these signals, not every editor frame. `observation` reads or long-waits for a
new result. `stop_observing` releases it.
Finishing a report quota releases its managed query. Queries also retire when
their current provider disappears/replaces itself. Each MCP protocol session
owns its observations: closing that session releases them without touching
another client or cancelling an already running engine job. Heartbeats renew
idle observation leases; a killed sidecar's leases expire instead of remaining
permanent engine observers.

`reports` limits deliveries. `changes` and `adaptive` deliver changed results;
`turns` delivers at distinct real user-turn IDs. Use a stable conversation ID as
`consumer`. Tools never invent a new user turn, and an MCP notification does not
force a model to run. Hosts that own the conversation can call `conversation_turn`
or the SDK's `reports` endpoint once per user turn, and insert its result into
that turn's context. Repeating the same cycle ID does not consume twice.

Claude Code can use its public UserPromptSubmit hook:

```powershell
python -m infernux_mcp.connect --client claude --project E:/Games/MyGame --hook-output E:/Games/MyGame/.claude/settings.json
```

The hook adds `infernux_consumer` and available reports to the next prompt's
context. Pass that consumer to `observe`. Turn counting requires a stable
`prompt_id`, `request_id` or `event_id` supplied by the host. The base hook payload
does not promise one: without it the hook delivers change reports only and
explicitly states that automatic turns mode is unavailable. It never invents a
UUID/user turn from a hook invocation. A conversation-owning host can instead
pass its real ID through `conversation_turn` or the SDK. Other clients can use all standard
tools/resources and the explicit turn entry point; automatic no-tool-turn
injection depends on the host providing a real prompt hook. No private client
state is patched.

## Failures and lifecycle

One request means one operation. A timed-out or disconnected write is never
replayed automatically. Engine instance IDs distinguish restarts; `engine_connect`
explicitly binds the requesting session to the current instance. Other clients
retain their own binding. Source failure retires the current
plugin's registrations and child service; repairing the source constructs a new
service. The editor remains the owner of jobs, queries, scene state and captures.

Standalone stdio and HTTP processes watch their actual MCP and Automation SDK
Python source directories through filesystem events. Saving, adding or deleting
current source stops that process, releases its observations and reports the
restart requirement on stderr. It does not hot-execute replacement code, replay
a request or cancel an already submitted engine job. Restart the client connection
after repairing or intentionally reverting the source.

Build the package with `python package.py build dist/infernux.mcp.inxpkg`.
The package includes only `package/`; test sources and CI files are not installed.
