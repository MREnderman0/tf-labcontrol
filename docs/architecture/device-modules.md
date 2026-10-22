# 设备兼容模块维护规范

新增型号按“一个型号一个完整目录”维护，已迁移型号继续在包内扩展。当前注册模块以 `devices/catalog.py` 为准；对应说明和能力见自动生成的[型号导航](../devices/README.md)与[能力矩阵](../generated/capability-matrix.md)。

## 目录、职责和唯一来源

模块位于 `src/lab_control/instruments/devices/<厂家_型号>/`。每个模块必须包含：

- `adapter.py`：精确身份、能力/范围、型号协议、编码/解析及关联校验；较大模块可在包内拆分文件。
- `__init__.py`：导出纯元数据 `DEFINITION`，绑定 `Profile`、适配器类型、文档元数据与包目录；导入不得加载驱动或连接设备。
- `README.md`：能力、协议依据、接口/驱动、单位/限制、依赖、测试命令、已验证和未测项目、已知问题。
- `tests/` 与 `tests/fixtures/`：型号专属测试和脱敏样本；`capabilities-before.json` 是本次迁移基线，不能为掩盖回归随意更新。后续有依据的功能变更可新增 `capabilities-current.json` 作为当前契约，并在包内说明差异和证据；必须保留迁移前基线。
- `hardware/`：仅显式运行的实机入口及其说明；真实地址、序列号和原始通信仍保存到运行目录。

通用 UI/CLI、会话、身份与占用锁、传输分帧、存储留在公共层。真正通用的协议辅助函数可放 `instruments/common/`；公共层不反向导入型号包，型号包不交叉导入，不导入旧的混合型号集合。范围和型号差异不能塞回公共类。

采集错误分类、暂态上限及刷新/保存调度遵循[监测一致性规则](live-acquisition.md)。型号包验证回包并提供配置差异；公共层统一决定无效帧丢弃、任务暂停和连接状态，不复制各型号的任务恢复循环。

内部 `DeviceDefinition` 将能力、适配器构造和文档关联起来。唯一注册入口是 `devices/catalog.py` 的 `DEVICE_MODULES`；重复适配标识立即报错。`registry.PROFILES`、`match`、`describe`、`create_adapter` 对现有调用方保持兼容。协议测试通过 `build_scpi_adapter(config, transport)` 注入 MockTransport，使用同一型号工厂，不用通用类代替型号实现。

非 SCPI 型号可声明可选 `factory`、`capability_schema` 和只读 `discovery_factory`。公共运行时只调用已注册元数据，不写入型号命令或启动型号服务；发现入口只返回候选、观察来源和驱动错误。服务生命周期与身份边界留在型号包，工厂仅在显式连接且公共资源锁取得后运行。连接配置的可选 `usb_instance_id` 也参与资产占用锁；旧配置缺少新增字段时保留原路线。

`discovery_connection_only_transports` 声明哪些传输在发现阶段只保留候选、必须显式连接后才核验身份；公共发现不会为这些候选构造适配器、加载连接 SDK 或发送设置。它与 `identity_requires_adapter` 分开：后者只选择允许探测时的身份协议，不表示禁止探测。Pico 的 `pico`、PM100D 的 `tlpmx/pm100d`、53100A 的 `tserve` 分别在包内声明；未列出的传输沿用原发现策略，不因此新增支持承诺。CoreMorrow 串口仍在公共租约内执行型号只读身份查询。枚举回调自身的驱动要求仍按型号声明，与连接和身份探测分开。

型号可注册 `readonly_check(config, folder, *, duration, root, input_condition)`，由显式 CLI `verify-readonly` 分发，源码与冻结程序使用同一入口。公共 CLI 要求唯一资源及型号，不扫描或猜测目标；型号入口负责调用正式会话、留档和返回阶段结果，不能直接绕过公共占用锁。未注册入口的型号明确拒绝。实机入口保持延迟导入，加载注册表不得连接设备。

需要先认证的已验证 LAN 主地址，可声明只读 `discovery_identity(transport, config)`。公共发现取得资源锁并打开传输后，只有库存主地址仍归属该身份、未撤销且无冲突时才选择此回调；回调只执行型号握手和读取本次真实身份，不初始化采集、不写测量设置。传入的是新发现配置，不携带旧型号、序列号、选件或限值；返回的身份重新通过公共型号匹配和地址归属核验，地址复用不能继承旧资产设置。未登记的型号没有回调，公共层不猜认证命令。

## 新增与扩展

1. 创建独立包、精确的适配标识和型号匹配，提供有来源的能力与保守边界。
2. 在唯一目录注册一条模块路径；完成包内文档、协议样本、越界/错误/通道隔离测试。没有专属能力时不得伪装支持。
3. 执行型号测试、全项目回归、Ruff、Mypy、文档生成/检查及构建导入检查。公共接口集成测试留在顶层 tests；专属协议测试留在型号包。
4. 需要实机验证时遵循[实机留档规范](../testing/records.md)，只访问明确配置的资产、遵守真实进程独占锁和设备独占锁。同一运行时内不同设备可以并行；不同真实 GUI/CLI/Python 进程互斥，未知别名探测仍需避让活动会话。每次通过、失败和受阻均创建批次，软件通过不自动修改实机验收标记。

型号 README 和注册元数据是文档维护源。`scripts/generate_device_docs.py` 从它们生成型号卡片、所有类别导航和能力矩阵；不要直接编辑派生卡片。源包内相对链接由生成器换算到派生文档位置。

新增型号从[接入流程与模板](../testing/onboarding.md)开始。包内 `hardware/records.py` 提供离线格式解释，统一摘要的文件与校验由公共存储层处理；型号分支不得放进公共记录工具。记录导入及文档生成不能实例化运行库或连接硬件。各接口、固件和具体项目的实测结论以运行目录为准，不把型号级兼容字段当资产验收状态。

只读复核入口支持 `--asset-name '<已有资产目录名称>'`，用于将带斜杠等字符的完整身份串关联到已有资产目录。该参数只影响留档路径，不改变配置里的完整序列号或身份校验。

## 移除型号

1. 从 `DEVICE_MODULES` 撤销该型号的唯一注册项，再删除其完整包目录。不要删除公共协议工具或其他型号。
2. 运行 `scripts/generate_device_docs.py`，更新导航/矩阵，并根据生成文件清单清理该型号的派生卡片。生成器只清理清单中归其管理且内容未被人工修改的文件；人工修改冲突会明确报错。
3. 运行回归与文档检查，确认其他型号可扫描、连接和显示；已移除型号的旧配置明确提示适配器不可用，在打开资源之前拒绝连接。

用户配置、历史库存、波形备份、硬件记录和系统驱动不随源码模块删除。历史资产仍可显示和查看记录，但已移除适配器不能继续控制；重新添加同一适配标识可再次使用保留配置。不存在运行中热卸载或自动删除用户数据。

## 验证命令

```powershell
.venv-dev\Scripts\python.exe -m pytest -q
.venv-dev\Scripts\python.exe -m ruff check src tests scripts
.venv-dev\Scripts\python.exe -m mypy
.venv-dev\Scripts\python.exe scripts/generate_device_docs.py
.venv-dev\Scripts\python.exe scripts/generate_device_docs.py --check
.venv-dev\Scripts\python.exe scripts/check_docs.py
.venv-dev\Scripts\uv.exe build --offline --python .venv-dev\Scripts\python.exe --no-python-downloads --out-dir dist
.venv-dev\Scripts\python.exe scripts/check_device_wheel.py dist/tf_labcontrol_shell-0.3.0-py3-none-any.whl
```

pytest 已包含型号包内测试。构建必须包含兼容实现、README 和脱敏样本，且安装后的注册、能力读取不依赖源码工作目录、SDK 或硬件。自动化的移除测试用临时注册表/隔离副本执行，不删除工作目录或现场数据。
