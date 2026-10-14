# 开发环境、下载与驱动约定

## 下载策略（项目长期约定）

用户要求：后续安装、更新或下载依赖，优先使用国内镜像，不消耗 VPN 流量。默认清华镜像，阿里云为显式备用；失败即报告，不自动回退国外 PyPI。不为了下载修改系统代理、VPN 或全局 pip/uv 配置。

`scripts/setup-dev.ps1` 仅对安装进程及其子进程清除 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY，设置 NO_PROXY=*，退出时恢复环境。只有明确传入 `-UseProxy` 才保留代理。厂商组件优先官方国内站，否则使用官方地址直连；不从不明镜像获取驱动。每次厂商下载应记录版本、URL、SHA256、签名、安装结果和 DLL 路径。

## 桌面交付要求

依赖与驱动在开发过程中登记并准备离线材料，最终用户无需自行寻找下载资源。组件台账、安装维护缓存、设置内“环境与修复”、目录式打包与当前实现边界见[桌面部署与环境修复](desktop-deployment.md)。安装与网络配置执行器后续接入；本页的手工开发环境操作不作为最终用户交付流程。

## 项目环境

使用 `.venv-dev` 的 Python 3.12 x64，依赖版本由 `uv.lock` 锁定。依赖同步使用锁定版本，新增依赖或换镜像时更新锁文件。uv 固定为 0.12.18，安装在项目环境，启动不依赖全局 PATH。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1
# 未安装 Python launcher 时，指定本机已有 Python 3.12 x64
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1 -Python C:\Python312\python.exe
# 仅在需要时显式选择国内备用源
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1 -Mirror Aliyun
.venv-dev\Scripts\lab-control.exe
.venv-dev\Scripts\python.exe -m lab_control
.venv-dev\Scripts\lab-control-cli.exe doctor
```

根目录只提供 `run.bat`，调用项目 Python 的模块入口；`lab-control` 与模块入口调用同一正式启动函数。已有真实库存、配置、草稿和记录继续使用，历史模拟数据留在 `simulation` 隔离目录；新空库存不自动填充。Mock 仅用于内部测试与 `scripts/review_signal_ui.py`、`scripts/review_parameter_popups.py` 开发截图，共用深色琥珀界面，无独立演示启动器。

启动不联网更新依赖；缺少环境或必要 Python 依赖时，批处理提示并调用上述国内镜像安装脚本。刷新项目安装前正常关闭本项目软件，避免 Windows 锁住旧可执行文件。安装脚本同步注册信息，并清理旧 `lab-control-demo.exe`。仅刷新本地项目且已有构建缓存时，可离线执行：

```powershell
.venv-dev\Scripts\uv.exe pip install --offline --no-deps --python .venv-dev\Scripts\python.exe --reinstall-package tf-labcontrol-shell -e .
```

检查可直接调用 `.venv-dev\Scripts\ruff.exe`、`mypy.exe`、`pytest.exe` 和 `python.exe`。如使用 uv，调用 `.venv-dev\Scripts\uv.exe`，并设置 `UV_PROJECT_ENVIRONMENT=.venv-dev`。下载政策同样适用于构建工具。

构建时也指定已有解释器并禁止自动下载 Python；在已按上述策略禁用代理的进程中运行：

```powershell
.venv-dev\Scripts\uv.exe build --python .venv-dev\Scripts\python.exe --no-python-downloads --default-index https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/
```

## 开发产物清理

清理前保存未提交修改并生成按路径、大小、哈希及理由列出的清单。缓存、旧构建包和与 Git 历史／当前源码内容相同的隔离副本可以清理；递归目标必须位于项目内，不跟随目录联接或符号链接。不要整目录删除 `.tools`：其中包含离线驱动、厂家资料、唯一实机证据和被文档引用的验收材料。安装环境、当前有效构建、正式库存及采集数据保留。

一次性迁移／提交整理脚本须确认任务已完成且没有入口、动态注册或文档依赖后再删除；无法确认用途或恢复来源的内容保留。旧卡片版窗口、主题及其依赖按 `direction-approved.md` 保留，不能仅以“不由当前入口导入”为依据删除。

## 多设备优先 PyVISA

支持 VISA/SCPI 的 USB、LAN、GPIB 和串口设备优先 `transport="visa"`，通过 PyVISA 和已安装的 Windows x64 VISA 后端接入。LAN 优先仪器提供的 `TCPIP0::地址::inst0::INSTR` 或 HiSLIP 资源；仅有原始端口时也可使用 `TCPIP0::地址::5025::SOCKET`。串口可用 `ASRL…::INSTR`，波特率以设备配置为准。

同一身份的 VISA 别名只有在只读身份查询确认后才能作为优先主资源；同一物理设备的别名共用占用锁，不允许双开。明确手动选择的主资源不被扫描覆盖；已连接会话也不在后台切换。超时不自动换协议重放写入。

PicoScope 6404E 使用 PicoSDK，53100A 使用 TSERVE；不能把厂商私有协议伪装成 VISA。Thorlabs 应根据当前驱动和已实现适配器选择路线，不自动运行 DriverSwitcher，不将 DLL 可加载等同于设备验收。其他型号没有已核实的实机范围查询时使用型号/通道文档范围，待连接后逐台验收。

## 原生组件

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/download-vendors.ps1` 使用已确认的官方下载地址，支持 `-Vendor Visa`、`Pico`、`Thorlabs`，默认直连。已核验的安装包优先保存在仓库 `deployment/packages/installers`，下载暂存 `.tools/installers`，失败保留断点；有效已有安装包直接复用。来源由 `deployment/components.json` 统一维护，下载后核对锁定 SHA256、厂商签名并保存来源 JSON；`-VerifyOnly` 仅验证已有离线材料。当前开发阶段可使用厂商向导，后续安装维护组件负责自动安装。脚本不会自动安装、切换 USB 驱动或重启系统。

项目内的 `deployment/packages` 是可随仓库复制的离线材料：`installers` 放已验证的 Windows 安装包，`python` 放从 `uv.lock` 导出的锁定清单和清华镜像 wheelhouse，`tools` 放项目使用的 uv。`setup-dev.ps1` 检测到缓存完整时自动执行离线同步，不访问网络；缓存不完整时才使用本节约定的国内镜像。大文件由 Git LFS 管理，clone 后需要执行 `git lfs pull`。

- R&S VISA：官方国内下载，系统安装提供 64 位 VISA 后端。
- PicoSDK：官方 x64 SDK，为 6404E 提供 `ps6000a.dll`。
- Thorlabs OPM/TLPMX：官方安装包，提供 `TLPMX_64.dll`；保留现有 USB 驱动选择。

厂商软件安装到系统目录，Python 依赖安装到项目环境。管理员权限、安装向导或重启待完成时记录实际状态，不自动重启。诊断会检查 Python 依赖、VISA 后端、SDK PE 位数、DLL 加载和必要导出函数；不会打开设备。SDK 可自动查找厂商标准目录，也可在连接配置里提供绝对 `sdk_path`。

### VC++ 运行库版本门禁

项目目标是 Windows 10/11 x64，组件清单固定 Microsoft v14 x64 安装包及 SHA256。当前登记版本为 `14.51.36247.0`，安装前读取 `HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64`：`Installed=1` 且版本不低于清单版本时直接跳过，不运行安装程序；检测到更高版本也跳过，禁止降级。只有缺失或版本较低时才校验安装包签名/哈希，并通过外部管理员 PowerShell 执行 `/install /quiet /norestart`，安装后再次读取注册表并记录结果。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\ensure_vc_runtime.ps1
```

脚本记录在 `D:\TF-LabControl-runtime\TF-LabControl\maintenance\vc-runtime\results.json`，旧记录自动保存在同目录 `history`；UAC 取消、安装退出码和需重启状态均保留。脚本不删除运行库、不回退版本、不自动重启。此门禁用于避免重复运行旧厂商安装程序造成 VC++ 运行库降级。版本选择依据 [Microsoft 最新支持的 Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)。
