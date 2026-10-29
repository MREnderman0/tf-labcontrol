# Agent 协作实验

在“系统设置 → Agent 协作实验”中，为本机或实验网内的 Agent 开放当前桌面软件的设备服务。新安装默认关闭。设备发现、适配器选择、资产核对和授权只由本机界面完成；远程工具不提供扫描、原始命令、任意文件访问或扩大授权的入口。

## 首次连接

1. 按[控制与监测](control-and-monitoring.md)完成设备发现及资产核对。确认完整型号、序列号与稳定资产 ID；不能读取序列号时，本机核对铭牌与地址映射。
2. 启用 Agent 接入并应用设置。本机默认 `http://127.0.0.1:8765/mcp`。实验网选择已配置在本机物理网卡上的 IPv4 地址，启用 HTTPS，默认端口 8766。不会绑定所有网卡或自动修改网卡地址。
3. 为每个协作者创建独立客户端凭据，保存创建时唯一一次显示的值。服务器只保存校验值；凭据不写入连接文件、配置示例或审计日志。
4. 选择客户端，授予实验范围：设备、通道、操作、允许修改的参数能力、数值范围和有效期（默认两小时）。输出/触发和采集格式初始化单独勾选。同一参数能力的字段一起授权，留空数值边界时仍执行型号自身的范围校验。
5. 导出连接 JSON 和 Codex TOML 配置片段。局域网 JSON 内包含服务器公共证书，不含私钥。复制连接器所在的完整发行目录到 Agent 电脑，调整 TOML 中的本机路径；不要只复制单个 EXE。
6. 在启动 Agent 的进程环境中设置 `LAB_CONTROL_AGENT_TOKEN` 为该客户端凭据。配置片段只引用变量名。先运行连接检查，再把片段合入 Agent 的 MCP 配置。

发行连接器与 GUI 共用 `launcher.main`，但在导入 Qt、创建 Runtime 或加载硬件前进入连接器分支。`TF-LabControl.exe` 保持无控制台；`TF-LabControl-Agent.exe` 提供标准输入输出。GUI 仍由唯一 `run.bat` 或正式安装入口启动。

```powershell
# 已设置当前进程的 LAB_CONTROL_AGENT_TOKEN；下列文件名不包含凭据
.\TF-LabControl-Agent.exe --agent-connector --connection .\lab-control-connection.json --check
# 源码开发环境中的同一分发入口
.venv-dev\Scripts\python.exe -m lab_control --agent-connector --connection .\lab-control-connection.json --check
```

本机也可在 Codex 中直接配置 Streamable HTTP：

```toml
[mcp_servers.lab_control]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "LAB_CONTROL_AGENT_TOKEN"
```

局域网优先使用导出的 stdio 连接器配置。连接器只信任 JSON 中的服务器证书，并校验目的 IP/服务器名称，禁用代理和重定向；不会修改系统证书信任。证书或 IP 更新后，重新导出并分发连接 JSON。证书有效期一年，剩余不足七天时下次启动重新生成；正在运行时不静默更换。

Codex 的工具审批独立于软件内的实验授权。默认导出保留客户端原有审批策略；若要按本机已授予的实验范围连续调用，可仅在该 MCP 服务配置中设置 `default_tools_approval_mode = "approve"`。服务器仍逐次执行范围、有效期、输出权限和版本冲突校验。选项依据 [Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

服务器同时校验 Bearer、Host、Origin，单请求上限 1 MiB，每个监听最多 32 个在途 HTTP 请求。这里是预先分发凭据的私有实验服务，不提供 OAuth 登录或公开互联网发现。

## 一次协作实验

建议让 Agent 执行以下流程，并明确本次实验的设备、范围与结束条件：

1. `get_server_info` 获取自己的授权；`list_devices` 选定稳定资产 ID。
2. `connect_device` 连接或复用会话。默认不进行改变采集格式的初始化；需要时显式请求 `configure_acquisition` 并具备相应授权。已存在的只读会话需明确断开后重新连接才能升级初始化权限。
3. `get_capabilities` 获取型号能力、通道、单位、范围、数据源及采集默认动作；`get_device_state` 获取缓存状态；`read_parameters` 读回需要的设置。型号声明需要确认的采集同样要求输出/触发权限，不能用 `initiate=false` 绕过。
4. `preview_change` 生成五分钟有效的单项修改预览，然后 `apply_change` 执行。预览绑定会话和配置版本；GUI 或其他协作者改变配置后，返回 `configuration_conflict`，重新读取与规划。
5. `acquire_once` 单次采集并保存，或 `start_monitoring` / `start_recording` 交由软件持续调度。`get_latest_measurement` 返回质量、真实时间、帧序号、配置版本、年龄和有限摘要；大数组保存在数据目录。
6. `list_tasks` / `get_task_status` 跟踪实际结果。指定 `stop_task` 停止后等待二进制排空与 CSV 转换；`list_artifacts` / `get_artifact` 获取结果引用。

所有有副作用的请求须携带客户端范围内唯一的 `request_id`。同 ID 同参数返回原任务；同 ID 不同参数拒绝。超时、部分完成或状态未知时，查询原任务，不能换一个 ID 自动重发。网络请求取消不等于撤回设备命令；已接受 I/O 持续跟踪到结果。

设备 worker 对 GUI 与多个客户端串行执行。设备已有监测/记录时，新启动返回 `task_conflict`，可通过任务列表找到当前任务（本地界面任务 ID 以 `gui-` 开头）。任何具备该设备和通道停止授权的协作者都可明确停止它；创建者只用于追溯。需要改变数据源或从监测转为记录时，明确停止原任务、等待收尾，再启动新任务。

状态包括 `accepted`、`running`、`stopping`、`binary_saved`、`converting`、`completed`、`partial`、`failed` 与重启后的 `interrupted`。`phase_history` 保存观察到的阶段。短暂阶段可能在相邻查询间完成，不能据此认为没有保存。记录参数锁直到二进制收尾才释放；CSV 转换不占用仪器。沿用公共[采集和记录规则](../architecture/live-acquisition.md)，无效帧不保存，慢磁盘满缓冲明确暂停并排空。

## 文件下载

`get_artifact` 返回受鉴权的下载路径，仍须同一客户端凭据和当前有效的设备/通道结果授权。文件 ID 是登记引用，不接受服务器文件路径。可用连接器独立下载：

```powershell
.\TF-LabControl-Agent.exe --agent-connector --connection .\lab-control-connection.json --download <产物ID> --grant <当前授权ID> --output .\experiment.csv
```

已有目标文件拒绝覆盖；下载失败保留 `.partial`，不能当作完整结果。授权包含多个通道的文件要求调用者拥有其中所有通道的权限。软件记录成功、设备读回成功和物理输出测量是不同结论，Agent 必须分别报告。

## 撤销、退出和留档

客户端断线时实验继续；关闭接入仅停止网络访问，已接受任务继续到完成或授权到期。授权到期/撤销或客户端撤销会停止相应任务，完成在途读取和记录收尾，不停止其他独立任务。重启保留客户端与授权模板，但执行授权一律失效；本机可“按所选范围重新授权”。重启不恢复任务、不重放命令，未确认结果保留为中断状态。

关闭软件先停止接入和任务，再等待保存、释放会话与进程锁。停止、撤销和退出均沿用现有输出行为，不自动关闭或恢复设备输出；未知状态不能解释为关闭。驱动内部阻塞仍受现有 I/O 超时和驱动退出能力约束。

运行目录中的数据库保存任务去重、授权模板和产物索引；现有审计记录调用方、授权、请求、设备身份、修改前状态、读回与最终结果。必要审计失败时拒绝新写入，停止/清理仍可执行；审计或索引失败会保留错误，不能解释成完整成功。实际地址、序列号、证书私钥、凭据和原始通信记录均不提交源码。

## 实验网防火墙

本机面板的“实验网防火墙”仅管理当前程序、指定本机 IP/网卡、TCP 端口、LocalSubnet 远端和 Private/Domain 网络的应用规则。用户选择添加或撤销后独立提权，GUI 保持普通权限。活动扫描/设备会话阻止维护；写入前保存规则状态，逐项读回，失败尝试恢复并记录结果。规则不匹配时拒绝覆盖其他配置。

此入口不实现 DHCP 或网卡自动配置；相关现行边界仍见[部署规范](../development/desktop-deployment.md)。第二台电脑连接、UAC、防火墙真实读回与干净 Windows 10/11 验收须单独完成。

## 开发验证与实现入口

```powershell
# 仅使用隔离 Mock，验证官方 SDK、HTTPS、stdio、旧协议及下载
.venv-dev\Scripts\python.exe scripts/check_agent.py
# 指定已安装 Codex CLI，进行真实 Agent 工具调用；不更改用户 Codex 配置
.venv-dev\Scripts\python.exe scripts/check_agent.py --codex <codex.exe路径>
# 完整非硬件检查、目录构建、源码外冻结 worker 与连接器
.venv-dev\Scripts\python.exe scripts/check_project.py --desktop
```

实现入口是 [Agent 门面](../../src/lab_control/application/agents/service.py)、[授权策略](../../src/lab_control/application/agents/policy.py)、[MCP 传输](../../src/lab_control/agent/server.py)、[工具定义](../../src/lab_control/agent/tools.py)、[连接器](../../src/lab_control/agent/connector.py)和[本地面板](../../src/lab_control/ui/pages/agent_collaboration.py)。网络线程不操作 Qt，不创建第二个硬件 Runtime，设备工作仍走原会话、租约与记录流水线。

协议使用固定的 [官方 Python MCP SDK 2.3.0](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)，旧协议由 SDK 兼容层处理；Codex 配置依据[官方 MCP 说明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。材料维护见[生成下载资源页](../generated/download-resources.md)。本次软件证据和待验收项见[协作实验验证记录](../reviews/agent-collaboration-2026-10-29.md)。
