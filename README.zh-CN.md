# Infernux MCP

这是 [Infernux](https://github.com/ChenlizheMe/Infernux) 游戏引擎的官方 MCP 插件。它让 AI 编程 Agent 能通过稳定、可发现的接口查看项目、修改场景、运行游戏、注入输入，并直接读取编辑器和 Player 的实际渲染结果。

[English](README.md) · [Infernux 引擎](https://github.com/ChenlizheMe/Infernux) · [插件模板](https://github.com/InfernuxEngine/infernux_plugin_template) · [发布制品](https://github.com/InfernuxEngine/infernux_mcp/releases)

![Infernux MCP 如何连接 AI Agent 与编辑器](package/plugin_pages/media/system_overview.png)

## Agent 可以做什么

- 查看和修改场景物体、组件、DataAsset、材质、粒子与相机
- 进入播放模式，执行播放、暂停、单帧和停止
- 通过引擎事件队列发送键盘与指针输入
- 获取 Scene、Game 和独立 Player 真正渲染出的画面
- 构建并控制 Debug Player
- 先发现 operation schema，再按准确参数操作编辑器

| 包标识 | 版本 | 适配引擎 | 默认地址 |
| --- | --- | --- | --- |
| `infernux/mcp` | 0.1.2 | Infernux 0.4.x | `http://127.0.0.1:9713/mcp` |

## 安装与连接

Infernux 新项目默认带有这个官方插件。已有项目可以在编辑器的**插件**窗口中安装或更新；不需要 Agent 操作时，也可以在同一位置禁用或卸载。

把支持 MCP 的客户端连接到 `http://127.0.0.1:9713/mcp` 即可。服务只接受本机连接。如需更换端口，请在启动编辑器前设置 `INFERNUX_MCP_PORT`。

Agent 应先调用 `host_session_status`，再读取 `operation_schema_list`。通过 `operation_schema_search` 找到所需能力、读取准确 schema，最后用 `operation_command_execute` 执行。构建等耗时任务使用 `operation_job_submit`，一组顺序明确的操作可以交给 `operation_batch_execute`，减少通信往返。

```json
{
  "tool": "operation_schema_search",
  "arguments": {"query": "scene object create", "limit": 10}
}
```

## 可重复的客户端工作流

Python 客户端新增 `session` 模式：每行输入一个 JSON 请求，复用同一个已初始化的 MCP 连接，完成发现、依赖前一步结果的编辑和状态查询。`call` 支持 `--args-file path.json`（`-` 代表标准输入），避免多层 shell 引号。`--timings` 把客户端分阶段耗时写入 stderr，stdout 保持机器可读 JSON。操作失败、批次中部分步骤失败，以及任务失败或取消时，退出码为非零。

[客户端工作流与耗时诊断](package/plugin_pages/operations.zh-CN.md#python-客户端)说明了协议、超时边界和只读基准测试。现有 gateway API、能力白名单与确认要求保持不变，不会自动重试命令。源码改进将在后续打标签发布时进入新安装包，当前发布版本号不变。

## Compute 性能诊断（041）

在操作目录中搜索 `runtime.compute`。`statistics` 读取引擎已有的上传/回读字节数、派发与提交次数、CPU 等待时间；`reset_statistics` 开始新的计数区间。需要 GPU 时间戳时显式启用 `profiling`，测量完关闭；切换时可能等待在途任务。GPU 耗时只代表最近完成的一次提交，不是整帧耗时；完整帧的百分位仍通过 `runtime.performance` 获取。这些操作需要配套的 041 引擎宿主。

## 仓库说明

可安装插件位于 `package/`。仓库根目录的说明、独立打包器和 GitHub Actions 只负责开发与发布，不会进入插件包。推送 `v<version>` 标签后，CI 会构建、校验并发布 `.inxpkg` 与 release manifest。

## 许可证

[MIT](LICENSE)。
