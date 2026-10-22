# Python 开源组件和设备资料

## 直接采用

| 项目 | 用途 | 许可/边界 |
| --- | --- | --- |
| PySide6 | Qt Widgets、worker 和信号槽 | 发布前核对 LGPL/GPL/商业许可 |
| PyVISA | VISA 资源、SCPI 查询和二进制读取 | 不包含 NI-VISA Runtime |
| PySerial | 非 VISA 的 COM/RS-232/RS-485 | 设备帧由型号适配器负责 |
| NumPy | 数组、FFT、统计、滤波和拟合 | 仅处理领域数据 |
| pyqtgraph | 波形、频谱和趋势图 | UI 只接收降采样数据 |
| logging | 诊断日志和审计事件 | 审计字段由应用层生成 |
| pytest/pytest-qt | 适配器 Mock、解析和 UI 测试 | Mock 通过不等于实机通过 |

## 厂商运行库

VISA Runtime、USB/GPIB/串口驱动、PicoSDK、Thorlabs Optical Power Monitor、RIGOL Ultra Sigma、R&S VISA/工具和 LeCroy 远程组件都属于部署前置条件。每个型号卡片记录所需版本和安装验证方式。

K+K FXE 的官方资料和离线软件由[实验网络部署与换电脑](../guides/lab-network-deployment.md)维护：设备使用静态 IP，USB 配置工具用于首次配置和换机，KK Library 通过 `<IP>[:<Port>]` 直连并读取报告。官方来源为[下载页](https://kplusk-messtechnik.de/downloads/index.html)、[Getting Started](https://kplusk-messtechnik.de/downloads/files/Getting_Started.pdf)、[KK Library manual](https://kplusk-messtechnik.de/downloads/files/Manual_KK-Library_MultiSource_Ver_21.1.0_25-06-03.pdf)和[FXE Firmware Upload manual](https://kplusk-messtechnik.de/downloads/files/KK_FirmwareUpload%20-%20users%20manual.pdf)。

## 资料入口与后续候选

型号手册由[各型号说明](../devices/README.md)维护；厂商驱动按[开发环境](environment.md)和[实机指南](../testing/hardware-testing.md)配置。Python 实际依赖以 pyproject.toml 与 uv.lock 为准。

开发与厂商材料按[缓存恢复说明](../../deployment/packages/README.md)维护；[K+K 缓存说明](../../deployment/packages/tools/kplusk/README.md)仅说明材料来源和恢复边界，不替代型号说明或实机记录。

[DSA815 上位机与通信实现调研](../../src/lab_control/instruments/devices/rigol_dsa815/communication-research.md)记录 UltraSpectrum/UltraSigma、Lab::Measurement、emc-toolkit 和 ngscopeclient/scopehal 的资料及固定源码版本。它们是通信设计参考，未作为新增运行依赖引入；复用要求见[采集规则](../architecture/live-acquisition.md)。

SciPy、h5py、structlog 属于后续候选或历史设想。PyInstaller 已用于[桌面测试版构建](desktop-deployment.md)，完整安装器和自动修复执行器尚未交付。软件 Mock 验证不替代目标固件与选件实测。
