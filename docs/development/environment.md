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

## 多设备优先 PyVISA

支持 VISA/SCPI 的 USB、LAN、GPIB 和串口设备优先 `transport="visa"`，通过 PyVISA 和已安装的 Windows x64 VISA 后端接入。LAN 优先仪器提供的 `TCPIP0::地址::inst0::INSTR` 或 HiSLIP 资源；仅有原始端口时也可使用 `TCPIP0::地址::5025::SOCKET`。串口可用 `ASRL…::INSTR`，波特率以设备配置为准。

同一身份的 VISA 别名只有在只读身份查询确认后才能作为优先主资源；同一物理设备的别名共用占用锁，不允许双开。明确手动选择的主资源不被扫描覆盖；已连接会话也不在后台切换。超时不自动换协议重放写入。

PicoScope 6404E 使用 PicoSDK，53100A 使用 TSERVE；不能把厂商私有协议伪装成 VISA。Thorlabs 应根据当前驱动和已实现适配器选择路线，不自动运行 DriverSwitcher，不将 DLL 可加载等同于设备验收。其他型号没有已核实的实机范围查询时使用型号/通道文档范围，待连接后逐台验收。

## 原生组件

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/download-vendors.ps1` 使用已确认的官方下载地址，支持 `-Vendor Visa`、`Pico`、`Thorlabs`，默认直连。下载暂存 `.tools/installers`，失败保留断点；有效已有安装包直接复用。来源由 `deployment/components.json` 统一维护，下载后核对锁定 SHA256、厂商签名并保存来源 JSON；`-VerifyOnly` 仅验证已有离线材料。当前开发阶段可使用厂商向导，后续安装维护组件负责自动安装。脚本不会自动安装、切换 USB 驱动或重启系统。

- R&S VISA：官方国内下载，系统安装提供 64 位 VISA 后端。
- PicoSDK：官方 x64 SDK，为 6404E 提供 `ps6000a.dll`。
- Thorlabs OPM/TLPMX：官方安装包，提供 `TLPMX_64.dll`；保留现有 USB 驱动选择。

厂商软件安装到系统目录，Python 依赖安装到项目环境。管理员权限、安装向导或重启待完成时记录实际状态，不自动重启。诊断会检查 Python 依赖、VISA 后端、SDK PE 位数、DLL 加载和必要导出函数；不会打开设备。SDK 可自动查找厂商标准目录，也可在连接配置里提供绝对 `sdk_path`。
