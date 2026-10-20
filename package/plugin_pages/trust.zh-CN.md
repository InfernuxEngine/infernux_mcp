# 注意

这是本机开发接口，不是公网服务。只听 localhost。

![仅本机访问](media/trust_gates.png)

每次调用都会核对 operation、参数和项目权限（`ProjectSettings/mcp_capabilities.json`）。编辑前阅读 schema 的 `reversible` 与 `side_effects`：声明支持撤销的命令走编辑器自己的撤销栈；保存或重载文件、恢复检查点、构建 Player 不能统一通过撤销回滚。

截图来自引擎的 Scene / Game 画面，不会去抓桌面。

关编辑器、认准当前项目、跟 Debug Player 说话，各有一个短期口令。状态接口不会把口令原文吐出来。

HTTP 接口不做鉴权：MCP 由本机用户主导，默认授予全部能力。需要限制 Agent 时，把 `granted_capabilities` 改窄即可（例如 `["*.read"]`）。Host 不是 `127.0.0.1`/`localhost`，或带有非本机页面 Origin 的请求会被拒绝，防止外部网页借 DNS rebinding 访问这个端口。

开发权限属于可信访问，不是沙箱。编写项目 Python 或构建 Player 可以使用编辑器用户的权限执行代码。
