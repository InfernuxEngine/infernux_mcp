# 使用

这个包只露出很少几个 MCP 入口。真正能干的事都在 operation 目录里。

![调用 operation](media/agent_loop.png)

1. 搜一下要用的 operation。
2. 把它的参数说明读完，别猜字段名。
3. 按类型调用：查、改，或丢到后台跑。
4. 后面继续用它返回的 ID。资产是 GUID，场景对象以查询结果为准。

改不熟的组件字段前，先问 `infernux.scene.component.schema`。

`infernux.scene.open` 和 `infernux.scene.reload` 返回 `scheduled: true` 表示加载已排队。随后查询 `infernux.project.info`：`active_scene.last_load` 包含请求的 `path`、`status`（`pending`、`loading`、`loaded` 或 `failed`）以及具体 `error`。确认该路径已 `loaded` 后再编辑新场景，同时检查 `active_scene.document_state`：若为 `conflict`，表示加载期间磁盘再次变化，已加载的场景并非最新磁盘版本，需要用户处理外部冲突。这两个操作即使用 MCP 后台任务执行，任务完成也只代表排队完成；校验失败时原场景仍保持不变。

`active_scene.document_state` 会显示外部文件冲突状态。保存前通过编辑器的“从磁盘重新加载”“保留本地修改”或“另存为副本”处理冲突。重载与启动使用相同的严格场景格式校验，弹窗会显示解析错误；按错误提示修正文件后再加载。组件 `data` 不应包含 `__type_name__`、`__component_id__` 等运行时元数据。外部编辑器可以使用原子文件替换保存脚本和资产。

打 Player 这种慢活用 `operation_job_submit`，再用 `operation_job_status` 看进度，别死等第一次调用。

## Python 客户端

使用编辑器的同一个 Python 环境。在插件源码目录中，把 `package/editor` 加到 `PYTHONPATH`，保留原有条目；Bash 示例：`export PYTHONPATH="$PWD/package/editor${PYTHONPATH:+:$PYTHONPATH}"`。项目中已安装的插件对应路径为 `Packages/infernux/mcp/editor`。

```bash
python -m infernux_mcp.client --timings call host_session_status
python -m infernux_mcp.client call operation_schema_search --args-file search.json
python -m infernux_mcp.client --timeout 30 --timings session
```

全局选项放在 `call`、`session`、`list-tools` 或 `describe` 之前。多个编辑器使用 `--endpoint http://127.0.0.1:PORT/mcp` 选择地址。`search.json` 是 UTF-8 JSON 对象，例如 `{"query":"scene object create","limit":10}`。`--args-file -` 从标准输入读取，与 `--args` 互斥；无效 JSON 不会发出调用。

`session` 每行接收一个请求，同一个连接保持打开：

```json
{"id":"status","tool":"host_session_status","arguments":{}}
{"id":"schema","tool":"operation_schema_get","arguments":{"operation":"infernux.scene.object.create"}}
```

每次请求完成立即输出一行：`{"id":"status","result":{"ok":true,"data":{...}}}`。只接受 `id`、`tool`、`arguments`；`id` 可省略，也可为字符串、整数或 null。依赖前一步结果的命令应先读取响应，复用返回的物体 ID 与资源 GUID。EOF 关闭连接，空行忽略。无效记录或失败调用会输出错误，后续已提供的记录仍会处理。任一记录失败时最终退出码为 1，全部成功为 0，Ctrl+C 为 130。单次 `call` 遇到操作错误、批次中部分步骤失败，或任务失败/取消时也返回 1，stdout 保留服务端原始 envelope，不把操作结果里的任意字段误判为 gateway 错误。

复用连接仍遵守原有 schema、能力白名单、撤销路径、会话身份和确认要求，不代表获得额外授权。CLI 不会重新发出失败命令，也不会新建客户端来重试它。超时或断连不能证明命令已取消或回滚，重试前先查看项目或任务状态。长任务仍应通过 `operation_job_submit` 提交再查询。`--timeout` 限制 MCP 初始化和每次请求，不限制整个会话或等候下一行输入的空闲时间；传输清理可能增加总耗时。

## 耗时诊断

`--timings` 把 JSON 计时写入 stderr，不包含参数和结果内容。`client_create` 包含传输模块的延迟导入；`connect_initialize` 合并连接与 MCP 初始化；`request` 合并通信、编辑器主线程排队和执行；`serialize` 是本地 JSON 编码，`disconnect` 和 `total` 分别表示清理和 CLI 生命周期。`total` 不包含 Python 解释器/模块启动；比较独立进程时需要进程墙钟时间。客户端计时无法单独区分编辑器排队与执行，应结合错误码、控制台和引擎日志判断。输入、连接初始化和请求阶段的错误会分别标注。

编辑器已运行并持续 tick 后，可在插件源码目录执行：

```bash
python scripts/benchmark_client.py --endpoint http://127.0.0.1:9713/mcp --samples 3
python -m unittest discover -s test -p 'test_client*.py' -v
```

基准只读取会话状态、紧凑 schema 目录、运行状态及一个 20 次查询的批次，比较独立进程墙钟时间与复用连接的请求耗时，输出原始阶段与中位数。不修改项目能力设置，不重试失败调用。HTTP 测试需要 `package/requirements.txt`；纯客户端单元测试无需引擎或第三方依赖（`-p test_client.py`）。这些数据不是渲染性能，也不能代替场景载入/捕获完成的确认。无窗口宿主可以编辑和查看场景，但截图验证需要图形编辑器。
