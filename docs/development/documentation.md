<a id="文档维护与本次迁移"></a>
# 文档维护与兼容入口

## 唯一维护来源

使用指南写现行行为；需求写目标与实现边界；review/archive 保存特定时间的证据。型号协议、范围、固件问题、验证脚本及离线转换在型号包维护。派生卡片明确链接维护源，不手工改生成结果。

| 信息 | 唯一维护来源 | 其他入口的职责 |
| --- | --- | --- |
| 启动、控制和保存行为 | guides 与对应 architecture 规则 | 根 README 及 docs/README 按任务导航，不复制完整流程 |
| 开发环境、检查和桌面交付 | development/environment、software-validation、desktop-deployment | 使用现有项目环境；依赖准备和正式发布单独执行 |
| 型号能力、范围、硬件步骤 | 型号包 README、元数据、hardware/README | devices 与 generated 页面由生成器派生 |
| 下载资源 | components.json、开发材料 manifest、uv.lock | 下载页只由生成器刷新 |
| 实机状态与批次 | 运行目录硬件记录及版本化摘要 | 源码仅保存规则和脱敏模板，不维护现场资产结论 |
| 历史验收与设计 | reviews、archive、design 中的原批次证据 | 标注历史身份并链接现行维护源，保留原结论和截图 |

全部设备的现行说明采用固定章节和表格；新增测试更新对应的能力、接口、验证范围与已知问题，不在文末逐批追加段落。型号说明脱敏，资产现行页和原始批次分开维护，结构及生成入口见[记录规范](../testing/records.md)与[资产模板](../testing/templates/asset-status.md)。旧原始批次不可因整理当前结论而改写。

所有现行 Markdown 应能从根 README 经文档总入口到达。旧页带 `docs: redirect` 标记并链接新页；历史页带 `docs: historical`；模板带 `docs: template`。真实身份、地址、日志与测试数据禁止提交。

## 新增、修改和删除

新增设备按[接入流程](../testing/onboarding.md)，包内 README 使用[型号模板](../testing/templates/model-readme.md)。所有类别导航由注册元数据和现有待补卡片生成；型号不完整时保留待补卡片，不注册猜测适配器。

删除独立模块按[模块规范](../architecture/device-modules.md)撤销注册、删除包并重新生成。生成器只清理自己拥有且未被手工修改的卡片；历史实机记录不随源码删除。

## 检查入口

```powershell
.venv-dev\Scripts\python.exe scripts/check_project.py --only docs
# 修改维护源后，明确刷新对应派生页，再重复上述检查
.venv-dev\Scripts\python.exe scripts/generate_device_docs.py
.venv-dev\Scripts\python.exe scripts/generate_download_docs.py
```

检查覆盖 UTF-8、本地文件与标题锚点、导航可达性、重复注册/卡片、型号维护源和四类模板结构。Git 工作区检查全部已跟踪和新增未忽略 Markdown，包括 deployment、design；本地缓存和第三方载荷不加入导航检查。无 Git 元数据时，检查器仍读取现有的约定文档目录，缺失的链接文件照常报错，不因此视为验证通过。

完整文档验证须使用包含全部维护资料的源码仓库。精简 sdist 保留构建所需源码及文档，但不包含全部历史截图、设计资料与协作文件；解包后的文档检查可能因这些省略项失败。源码包或 wheel 构建成功不能作为完整文档验证通过的证据，发行包不为通过文档检查而扩充历史图片。

reviews/archive 报告及模板必须有对应标记；redirect 必须指向一个本地规范 Markdown，不允许跳转链或循环，保留的旧锚点必须在目标存在。现行命令禁止裸 `uv run/build/sync/pip/lock/export`，使用已有项目环境；历史证据不改写旧命令。旧文件与旧标题锚点保留后，现行导航应直接链接规范目标。

命令检查只读取源码 AST，不导入目标脚本、不运行命令或 `--help`。检查现行 shell 代码块及缩进命令中的 `python scripts/...py` / `python examples/...py`（含项目解释器路径）的脚本存在性；对本文件直接构建且参数声明完整的 argparse，检查未知选项及可静态确认的字面量 choices。正式 `run.bat`、`lab-control`、`TF-LabControl.exe` 和 `python -m lab_control` 的 GUI/CLI 参数按对应源码检查；旧 run_* 启动器、演示启动器和绕过模块分发的启动方式会报错。

动态参数名、导入的 parser 工厂及继承 parser 无法静态列全选项时，不猜测未知参数；变量、占位符和非 shell 示例不执行求值。当前不验证参数值类型、必填值数量、子命令位置、配置/设备内容，也不解析 PowerShell 安装脚本、任意第三方模块或行内命令。这些边界须由对应软件测试覆盖，不能把文档检查通过当作命令已实际执行。

检查是离线结构门禁，不能证明说明与实现完全一致、外部链接仍可访问、实机通过或软件已满足发布条件。行为变更须同时核对相应指南、架构、需求和测试，不靠关键词替换推断已实现。外部厂家资料有依据更新时另行核实；生成/检查不得创建 VISA 运行库或连接设备。

<a id="本次迁移清单"></a>
## 兼容入口索引

正文在规范位置维护，旧位置仅保留跳转；移动时重算相对链接并保留旧标题锚点。设计基线和截图目录不随文档整理移动。

具体入口映射及历史整理情况见[文档入口整理记录（2026-10-22）](../reviews/documentation-maintenance-2026-10-22.md)。本页只维护长期规则，不维护批次迁移表。

## 已知记录格式边界

已接入型号在各自模块提供离线转换器；未知格式、缺少身份/固件/范围/版本、空批次或损坏 JSON 保留索引并列为待核对。摘要只引用实际存在的阶段结果，不从目录名、输出关闭说明或上传目录推断控制、恢复或物理性能通过。
