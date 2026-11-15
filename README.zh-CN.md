# Infernux MCP

需要包含公共 Automation Host 的引擎构建；正式发布时再确定最低发行版本。

独立运行的标准 MCP 服务。场景、资源、输入、截图、构建和 Player 控制由引擎公共 Host
提供；本插件负责 MCP 连接、工具发现、资源映射、查询汇报和常用流程。
关闭本插件后，引擎的公共自动化能力仍然存在。

## 安装和连接

在编辑器插件管理器安装并启用 `infernux/mcp`。插件启动独立进程，自动选择空闲的
本机端口。连接地址记录在项目的 `Cache/Automation/mcp.json`，引擎公共接口在旁边的
`engine.json`。没有固定默认端口，也没有权限模式和工具白名单。

也可以使用标准 stdio：

```powershell
python -m infernux_mcp --project E:/Games/MyGame
```

引擎环境自带轻量 Automation SDK。单独的服务环境需要安装 `package/requirements.txt`，
将 `package/editor` 加入 `PYTHONPATH`，并安装引擎源码 `python/` 子项目中的轻量 SDK。
导入服务和 SDK 不会加载原生引擎，也不要求显卡。

为 Claude Code、Codex、DSH、WorkBuddy 生成对应格式的配置：

```powershell
python -m infernux_mcp.connect --client claude --project E:/Games/MyGame --output E:/Games/MyGame/.mcp.json
python -m infernux_mcp.connect --client codex --project E:/Games/MyGame --output <实际的config.toml>
python -m infernux_mcp.connect --client dsh --project E:/Games/MyGame --output <实际的plugins.yaml>
python -m infernux_mcp.connect --client workbuddy --project E:/Games/MyGame --output E:/Connectors/Infernux/mcp.json
```

不填 `--output` 就只打印配置。Claude、Codex、DSH 只替换指定名称的服务，保留其它服务和设置的值。
WorkBuddy 输出是独立 Infernux Connector 的 `mcp.json`；其[官方 Connector 格式](https://open.workbuddy.cn/en/docs/connector)
只取该文件里的第一个服务。目标已有其它服务时会拒绝且不改写文件，请为 Infernux 使用独立 Connector 文件；
只有 Infernux 自身服务的文件可以正常更新。
合并 TOML 或 YAML 会规范化格式且不保留注释；DSH 的 `!!js` 等标签及其内容原样保留，不执行表达式。不猜测客户端的
私人目录，也不读取聊天数据库。各客户端格式和官方参考链接见英文说明。

## 使用流程

先调用 `help` 和 `engine_status`，再用 `operation_search` 找能力、`operation_schema`
读参数，通过 `operation_call` 调用。`operation_batch` 按顺序执行，出错即停，已经发生的
修改不会自动撤销。长任务使用 `operation_submit` 和 `operation_job`；取消会返回实际
是否成功，不假称已经运行的操作被撤销。

`load_and_capture` 会等待场景真实加载完成，再等待引擎渲染截图完成，支持 MCP 后台任务
和超时。截图来自引擎渲染目标，不是系统桌面。工具也能通过公共 Host 控制输入、构建
和管理 Debug Player。

`infernux.plugin.*` 提供安装、兼容发行查询、覆盖更新、启停、卸载和重载；
`infernux.theme.list/select/refresh` 提供主题公共入口。它们与 GUI、轻量 SDK CLI
调用同一引擎服务，不复制安装器。更新未指定版本时选最高兼容发行，也可指定
`release_tag` 或本地 `source`；返回实际激活错误或重启要求。

资源入口是 `infernux://catalog`、`infernux://operation/{operation}`、
`infernux://query/{query_id}`。引擎插件启停会更新实际能力目录。

## 查询和多轮汇报

`observe` 将一个查询绑定到场景、资源、运行状态、日志、截图完成或自定义信号。
有信号才重新计算，不每帧查询。`observation` 可读取或等待变化；用 `stop_observing`
释放订阅。
报告次数耗尽会释放其受管查询，provider 停用或被替换时旧查询停止。每个 MCP 协议
会话分别拥有自己的订阅；关闭会话时清理，不影响其它客户端，也不取消已经运行的
引擎 job。空闲连接自动续租；服务被意外终止后，观察租约到期清理，不残留永久观察。

`reports` 指定汇报次数。`changes` 和 `adaptive` 在结果变化时汇报；`turns` 按真实用户
对话轮次汇报。同一轮的多个工具调用不会重复扣次数。使用稳定的 `consumer` 标记会话。
管理对话的 harness 可以每轮调用 `conversation_turn` 或 SDK 的 `reports`，再把结果放入
该轮上下文。MCP 通知本身无法强制模型开始新一轮推理。

Claude Code 可以配置公开的 UserPromptSubmit hook：

```powershell
python -m infernux_mcp.connect --client claude --project E:/Games/MyGame --hook-output E:/Games/MyGame/.claude/settings.json
```

hook 将 `infernux_consumer` 和查询结果带到用户下一轮输入中。创建 `observe` 时传入这个
consumer。按轮计数需要宿主提供稳定的 `prompt_id`、`request_id` 或 `event_id`；基础
hook 格式未承诺提供该 ID。缺少时只递送变化报告，并如实说明不支持自动 turns 计数，
不会把 hook 调用次数或随机 UUID 当用户轮。管理真实对话的宿主也可通过
`conversation_turn` 或 SDK 传入自己的真实轮 ID。其它客户端均可使用标准工具、资源和显式轮次入口；完全没有工具调用的轮次
能否自动注入结果，取决于客户端是否提供真实输入 hook，插件不会修改其私有状态。

## 生命周期和错误

网络断开或写操作超时后不自动重发。编辑器重启后使用 `engine_connect` 显式重连，
只改变发起该请求的会话绑定，其它客户端保持各自实例。
脚本失败会停用当前贡献和服务，修复源码后重新构造，不保留旧实现继续运行。
资源和普通插件错误留在编辑器内报告，不把失败伪装成成功。

独立 stdio 和 HTTP 进程通过文件事件监视其实际 MCP 与轻量 SDK 的 Python 源码目录。
保存、新增或删除当前源码会停止该进程、释放观察，并在 stderr 说明需要重启。
它不会热执行替换代码、重发请求或取消已提交的引擎 job；修复或主动撤销源码后重启连接。

使用 `python package.py build dist/infernux.mcp.inxpkg` 打包。安装内容只有 `package/`，
测试、CI 和外层脚本不会进入游戏工程。
