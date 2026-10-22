<!-- docs: historical -->
# 系统性工程优化与文档治理验证（2026-10-22）

本报告记录本轮代码、型号包、文档和本地 Windows 构建验证。现行维护入口为[系统架构](../architecture/system-architecture.md)、[软件验证](../development/software-validation.md)和[文档维护](../development/documentation.md)。本轮未连接真实设备，也未安装、升级或下载依赖。

## 环境与范围

- 本机 Windows 11，版本 10.0.26200；项目既有 `.venv-dev`，Python 3.12.10 x64。
- 保留公开调用入口、adapter ID、配置兼容、现有线程模型和存储格式。旧纯类型及型号导入保留兼容导出。
- 保留已批准的深色琥珀分类卡片、设备池和唯一主题；未修改批准截图。
- 库存、用户配置、真实设备批次及人工历史证据未迁移或删除；测试使用 Mock、临时目录及源码外隔离目录。

## 实施结果

| 范围 | 结果与维护入口 |
| --- | --- |
| 故障收尾 | 严格的操作前审计仍阻止未留档写入；暂停、停止及清理使用有界失败审计。Future 完成、参数锁、队列引用和多项退出清理分别保护，主异常保留附加收尾信息。见 [采集规则](../architecture/live-acquisition.md)。 |
| 保存结果 | 引入 RecordingOutcome / RecordingError，区分 received、committed、converted 及已保存路径；二进制/CSV 失败报告 recording_failed，缓冲压力报告 recording_paused。CSV 转换不占仪器锁。 |
| 公共职责 | 连接与不可变帧契约下沉 domain；拆出 SessionOperations、RecordedAcquisitionRunner、MonitorScheduler、FrameExportService、二进制/CSV worker、Inventory/providers 和窗口协作模块。 |
| 界面缓存 | Controller 统一提供快照；周期渲染及导航使用缓存，显式刷新仍读取最新状态。窗口清理逐项执行。 |
| 型号注册 | 所有型号从 catalog 注册；SDK 使用独立身份元数据、工厂和纯能力入口，PROFILES 与 SDK_ADAPTERS 从定义派生。枚举、显式身份确认及采集呈现策略由型号声明。 |
| 工程检查 | 新增 check_project.py，固定既有环境、排除实机、禁止自动同步依赖，保存成功/失败/中断报告；桌面选项构建隔离 wheel 和独立目录式程序。 |
| 构建依赖 | 对运行及构建依赖的完整适用闭包核对实际安装版本；不一致时阻断，发行清单记录核验后的版本。 |
| 文档 | 收敛根 README、指南、架构与需求的职责；修正工作区、锁、扫描、入口、记录格式和交付状态；历史评审归位并保留旧入口/锚点。见 [文档整理记录](documentation-maintenance-2026-10-22.md)。 |
| 文档检查 | 增加历史/模板/跳转规则、资产状态章节、Git 可见文档、脚本存在性及静态参数检查。只解析源代码，不执行文档命令；动态 parser 等边界明确记录。 |

## 旧型号迁移

下列 12 个旧型号的实现、范围、采集协议、样本、说明和硬件计划已集中到各自型号包。协议基线保持不变；没有将软件夹具标为实机证据。完整包导航见[型号模块](../devices/README.md)。

| 批次 | 保留的 adapter ID |
| --- | --- |
| 信号源与电源 | siglent.sdg6032x、keithley.2231a、siglent.spd4306x、itech.it6322 |
| 示波器与分析仪 | rs.rtm3004、rs.rto64、rigol.ds1202ze、lecroy.9254、rs.znb3004、keysight.3458a |
| SDK | thorlabs.pm100d、pico.6404e |

LeCroy 9254 包名为 `lecroy_waverunner9254`。未具备已验证硬件脚本的型号保留明确受阻入口和硬件计划，不伪造验收能力。型号移除测试保留旧配置与库存，并验证清晰的不可用提示。

## 本批次验证结果

完整统一检查于 2026-10-22 15:51:41–15:59:51 UTC 执行，耗时 490.406 秒，12 个检查命令退出码均为 0。

| 检查 | 本批次结果 |
| --- | --- |
| Ruff / 格式 | 全部通过，678 个 Python 文件格式一致 |
| Mypy | 371 个源码文件通过 |
| 全量软件测试 | 2460 passed，1 hardware deselected；355.10 秒 |
| 最后补充的文档命令回归 | 16 passed；含全量收集之后补充的 3 种 CLI 入口校验，既有断言保留 |
| 生成一致性 | 型号矩阵/卡片与下载资源页面均通过，未手工改派生数据 |
| 文档结构检查 | 全套阶段 198 份通过；加入本报告后再次检查 199 份通过 |
| 组件登记 | 开发模式通过，13 个外部组件的已有发布待核实项保留 |
| wheel | 离线构建通过；源码外验证全部 27 个注册型号、零硬件注册及逐型号删除兼容通过 |
| 目录式 EXE | PyInstaller 6.22.3 / hooks 2026.7 构建通过；包类型 online，准备过程无下载，release_ready=false |
| 冻结程序 | 复制到源码外中文/空格目录并清理 PATH；GUI 设置页渲染、CSV worker、损坏记录保留和无硬件访问检查通过 |
| 界面复核 | Mock 在 1440×900 与 1000×650 屏幕边界、DPR 1 下生成当前/历史设备池共 4 张截图；横向滚动值均为 0，已人工查看两种尺寸的当前设备池 |

冻结 worker 实际提交 1 帧并转换 1 帧；冻结报告确认 `hardware_accessed=false`、`outside_source=true`、`path_sanitized=true`、`corrupt_worker_preserved_input=true`。

首轮全量检查曾发现 4 项测试失败：旧模拟时钟补丁路径 1 项、SDK 纳入独立注册后旧移除测试假设 3 项；均修复后重新全量通过。另一次格式门禁在运行测试前阻断，格式修正后继续。失败报告保留，不覆写为成功。

## 本机产物与复现

以下路径相对项目根，属于本批次本机产物，不进入 Git：

- 完整检查：`.tools/engineering-review-2026-10-22/verification.json`。
- 最终静态与文档复核：`.tools/engineering-review-2026-10-22/final-docs.json`。
- wheel：`.tools/checks/20261022T155141Z-0051c502/desktop/wheels/tf_labcontrol_shell-0.3.0-py3-none-any.whl`。
- 目录包：`.tools/checks/20261022T155141Z-0051c502/desktop/bundle/TF-LabControl/`。
- 冻结检查：`.tools/checks/20261022T155141Z-0051c502/desktop/frozen-smoke.json`。
- 界面截图与指标：`.tools/engineering-review-2026-10-22/ui/`。

复现命令为 `.venv-dev\Scripts\python.exe scripts/check_project.py --desktop`；执行会创建新报告及独立产物目录，不自动获取缺少的材料。检查命令及参数边界见[软件验证](../development/software-validation.md)。

## 验收边界

本批次证明本机的软件回归、打包与隔离启动，不代表干净 Windows 10/11 交付或逐台仪器验收。未执行真实设备枚举、连接、参数写入、恢复或物理测量。发布的分发依据、安装方法及目标机验证待核实项继续遵循[部署门禁](../development/desktop-deployment.md)；自动网络配置与正式应用安装器仍按现有未实现边界管理。
