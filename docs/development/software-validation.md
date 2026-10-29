# 软件验证与夹具

公共测试目录按 unit、integration、hardware 分层；所有型号的专属测试及夹具集中在各型号包的 tests/fixtures，公共目录只维护跨型号契约与应用集成测试。夹具来源由型号说明标注，手册编写的夹具不冒充实机录制。厂商 DLL 使用类型化函数替身，不需真实硬件。

## 覆盖内容

- 领域：参数类型、单位/范围、确认、未知操作、精确型号/后缀和选件。
- 传输：TCP 分包/粘包、IEEE 块截断/长度、字节序、VICP 头、多包、共享 VISA 生命周期。
- 型号：各注册 SCPI 型号至少一条独立命令夹具；AFG/SDG 任意波形编码、SMA 列表、GPD 状态、RIGOL 缩放、复数轨迹、TSERVE MHz/ADEV。
- SDK：DLL 缺失、错误码、PM100D/TLPMX 通道 ABI 与 reset=0、Pico block/streaming、缓冲区释放及 Stop 失败。
- 应用：20 台模拟设备全部开始持续采集后连续扫描 3 次，保持原会话并持续产出批次；原子别名租约、连接竞争、取消、provider 失败、增量合并、SQLite、中文配置/数据和进程互斥。
- UI/CLI：正式页面与隔离 Mock 共用组件、固定类别、通用工作区及旧模式迁移、行/选择保持、显式资源、只读报告和采集导出。
- Agent：[协作实验验证](../guides/agent-collaboration.md#开发验证与实现入口)覆盖官方 SDK、HTTP/HTTPS、stdio、旧协议、授权/冲突/去重/撤销、共享记录和受鉴权下载；目录冻结自检同时运行 console 连接器完整 Mock 流程。

## 执行

在项目根目录使用已有 `.venv-dev`，环境准备按[开发环境](environment.md)显式执行。统一检查不安装、更新或下载依赖：

```powershell
.venv-dev\Scripts\python.exe scripts/check_project.py
# 仅检查文档；其他组为 lint、types、tests、deployment
.venv-dev\Scripts\python.exe scripts/check_project.py --only docs
# 查看将执行的命令，不运行检查
.venv-dev\Scripts\python.exe scripts/check_project.py --list
# 完整软件检查后，离线构建 wheel 和目录式 EXE 并验证冻结 worker
.venv-dev\Scripts\python.exe scripts/check_project.py --desktop
```

默认依次执行 Ruff 检查和格式检查、mypy、非硬件 pytest、型号/下载资源生成一致性、文档检查、开发模式组件登记。任一失败立即返回非零；不会自动格式化或刷新派生文件。检查环境固定 Qt offscreen，并移除遗留 `LAB_CONTROL_HARDWARE_*` 变量。

每次执行保存 JSON 报告到 `.tools/checks/<UTC时间-唯一ID>/report.json`，包含各命令、退出码、耗时和失败/中断状态；可用 `--report <新路径>` 指定位置，已有报告不覆盖。`--desktop` 只使用已安装构建工具和本地材料，按离线 wheel、wheel 隔离检查、联网模式目录包、冻结自检顺序执行；“联网模式”指发行包类型，构建检查本身不下载材料。产物写入本次独立检查目录，不覆盖 `dist` 或其他构建。

源码包/wheel 另行用现有构建后端离线生成，缺缓存或工具时明确失败；不得临时联网补依赖：

```powershell
.venv-dev\Scripts\uv.exe build --offline --no-build-isolation --python .venv-dev\Scripts\python.exe --no-python-downloads
.venv-dev\Scripts\python.exe scripts/check_device_wheel.py <生成的wheel路径>
```

修改部署代码另行执行[目录式构建与冻结验证](desktop-deployment.md#开发准备与构建)。开发组件检查允许显示 pending，不代表正式发布门禁通过；完整设备和干净 Windows 电脑验收仍需独立留档。

根目录 `conftest.py` 为公共测试及型号包内测试统一设置 offscreen 和 Qt 延迟删除清理；型号夹具仍留在本包。历史 pytest 硬件入口无条件跳过，环境变量不能启用它；真实设备使用[实机指南](../testing/hardware-testing.md)中的型号留档流程。软件测试不能证明仪器固件、接线、驱动选件或所有高级命令组合正确。矩阵中的 protocol_fixture_tested 只标记实际独立命令夹具覆盖的操作，不能把通用参数测试写成所有命令已通过实测。

## 历史与当前结果

[历史软件验证快照](../archive/software-validation-2026-09-23.md)保留原数字。当前结果以本次检查输出为准，不在指南中维护易过时的通过数量。软件测试与实机证据分别记录。
