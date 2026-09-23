# Python 开源组件和设备资料

## 直接采用

| 项目 | 用途 | 许可/边界 |
| --- | --- | --- |
| PySide6 | Qt Widgets、worker 和信号槽 | 发布前核对 LGPL/GPL/商业许可 |
| PyVISA | VISA 资源、SCPI 查询和二进制读取 | 不包含 NI-VISA Runtime |
| PySerial | 非 VISA 的 COM/RS-232/RS-485 | 设备帧由型号适配器负责 |
| NumPy/SciPy | 数组、FFT、统计、滤波和拟合 | 仅处理领域数据 |
| pyqtgraph | 波形、频谱和趋势图 | UI 只接收降采样数据 |
| h5py | 波形、频谱和长时序 | 单独存储 worker 写入 |
| structlog/logging | 诊断日志和审计事件 | 审计字段由应用层生成 |
| pytest/pytest-qt | 适配器 Mock、解析和 UI 测试 | Mock 通过不等于实机通过 |
| PyInstaller | Windows one-folder 打包 | 不静默打包厂商驱动或 SDK |

## 厂商运行库

VISA Runtime、USB/GPIB/串口驱动、PicoSDK、Thorlabs Optical Power Monitor、RIGOL Ultra Sigma、R&S VISA/工具和 LeCroy 远程组件都属于部署前置条件。每个型号卡片记录所需版本和安装验证方式。

## 资料入口

- [PyVISA 通信](https://pyvisa.readthedocs.io/en/latest/introduction/communication.html)
- [PyVISA 二进制值](https://pyvisa.readthedocs.io/en/latest/introduction/rvalues.html)
- [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)
- [RIGOL DG5000 Pro 编程指南](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/waveform-generators/DG5000Pro_ProgrammingGuide_EN.pdf?t=1761022282157)
- [RIGOL DSA800 编程指南](https://int.rigol.com/Images/DSA800_ProgrammingGuide_EN_tcm7-4361.pdf)
- [Siglent SDG6000X 编程指南](https://int.siglent.com/u_file/download/25_12_09/SDG6000X_ProgrammingGuide_EN06A.pdf)
- [PicoSDK 与 6404E API 对照](https://www.picotech.com/library/knowledge-bases/oscilloscopes/picosdk-and-picoscope-models)
- [R&S RTM3000 手册](https://www.rohde-schwarz.com/us/manual/rtm3000/)
- [R&S RTO6 手册](https://www.rohde-schwarz.com/us/manual/rs-rto6-user-manual_78701-1091264.html)
- [R&S SMA100B 手册](https://www.rohde-schwarz.com/us/manual/r-s-sma100b-user-manual-manuals_78701-434880.html)
- [WaveRunner 9404M 远程控制资料](https://www.teledynelecroy.com/oscilloscope/waverunner-9000-oscilloscopes/waverunner-9404m)
- [Keysight 3458A 用户指南](https://www.keysight.com/us/en/assets/9018-01343/user-manuals/9018-01343.pdf)
- [Thorlabs Optical Power Monitor 手册](https://www.thorlabs.com/software/MUC/OPM/v6.0/TL_OPM_V6.0.pdf)
- [R&S ZNB 手册](https://www.rohde-schwarz.com/au/manual/rs-znb-user-manual_78701-29151.html)

所有厂商资料链接只作为命令和驱动来源。目标固件上的实机响应决定“可控”状态。
