# 依赖下载资源（自动生成）

由 `scripts/generate_download_docs.py` 根据[组件清单](../../deployment/components.json)和[开发材料清单](../../deployment/packages/manifest.json)生成；勿手工修改。

通常在首次运行或设置 → 环境与修复 → 准备设备组件中选择所需组件。程序按下列顺序直连下载，验证固定大小、SHA256 和适用的签名后才启用；不自动运行安装程序。没有已核实国内来源的厂商组件使用官方直连。材料已准备、组件已检测和设备验收分别显示；下载记录不能替代安装或设备验收。

固定链接可能失效或被厂商更新；大小/哈希不符时拒绝使用，不自动改清单或升级版本。待补的接口驱动继续待核对，不提供猜测下载地址。

## R&S VISA

组件 ID：`rs-visa`；版本：7.2.6。

VISA communication and R&S LAN discovery

1. [官方国内站 — RS_VISA_Setup_Win_7_2_6.exe](https://cdn.rohde-schwarz.com.cn/pws/dl_downloads/dl_application/application_notes/1dc02___rs_v/RS_VISA_Setup_Win_7_2_6.exe)

[来源说明](https://www.rohde-schwarz.com.cn/applications/rs-visa_56280-148812.html)

大小：63498136 字节；SHA256：`6107881d30efe3f959c2484ca05eab41f2281a36b4f1cbfc490e8fb876f19723`。

验证策略：`authenticode`；分发依据：`pending`；安装验证：`pending`；目标环境验证：`pending`。

## PicoSDK

组件 ID：`pico-sdk`；版本：11.1.0.481。

PicoScope 6404E SDK and USB driver

1. [官方直连 — PicoSDK_x64_11.1.0.481.exe](https://www.picotech.com/download/software/sr/PicoSDK_x64_11.1.0.481.exe)

[来源说明](https://www.picotech.com/downloads/_lightbox/pico-software-development-kit-64bit)

大小：129097376 字节；SHA256：`29a6177fc4ab67b57bab7d9b75c76c6dadf898d192e54531f0b177ff3b23cc3f`。

验证策略：`authenticode`；分发依据：`pending`；安装验证：`pending`；目标环境验证：`pending`。

## Thorlabs OPM / TLPMX

组件 ID：`thorlabs-opm`；版本：7.0.5320.947。

PM100D optical meter SDK and driver

1. [官方国内站 — Thorlabs.OPM.7.0.5320.947.zip](https://media.thorlabscms.cn/contentassets/012d23129bc54ff2bfdbf4c6d4a306fa/thorlabs.opticalparametermonitor.7.0.5320.947.zip)

[来源说明](https://www.thorlabschina.cn/software-pages/OPM)

大小：134596957 字节；SHA256：`d759948f643033b4951182999377f44a0763265ad977971078a75592a757154e`。

验证策略：`authenticode`；分发依据：`pending`；安装验证：`pending`；目标环境验证：`pending`。

## FTDI VCP

组件 ID：`ftdi-vcp`；版本：待核实。

GPD-4303S USB virtual serial driver

下载材料：待登记。

[来源说明](https://ftdichip.com/drivers/vcp-drivers/)

大小：待核实 字节；SHA256：`待核实`。

验证策略：`authenticode`；分发依据：`pending`；安装验证：`pending`；目标环境验证：`pending`。

## Keithley 2231A USB adapter

组件 ID：`keithley-usb`；版本：待核实。

2231A-001 adapter driver

下载材料：待登记。

[来源说明](https://www.tek.com/en/support)

大小：待核实 字节；SHA256：`待核实`。

验证策略：`authenticode`；分发依据：`pending`；安装验证：`pending`；目标环境验证：`pending`。

## Serial interface driver

组件 ID：`serial-interface`；版本：待核实。

Identify USB-to-serial hardware by VID/PID before selecting its official driver

下载材料：待登记。

来源说明：待核实。

大小：待核实 字节；SHA256：`待核实`。

验证策略：`authenticode`；分发依据：`pending`；安装验证：`pending`；目标环境验证：`pending`。

## GPIB interface driver

组件 ID：`gpib-interface`；版本：待核实。

GPIB controller-specific driver and VISA interoperability

下载材料：待登记。

来源说明：待核实。

大小：待核实 字节；SHA256：`待核实`。

验证策略：`authenticode`；分发依据：`pending`；安装验证：`pending`；目标环境验证：`pending`。

## K+K FXE Firmware Upload and Configuration Tool

组件 ID：`kplusk-config-tool`；版本：5.1.0 (2025-11-28)。

USB configuration of K+K FXE static IP, subnet, gateway and device configuration

1. [官方直连 — KK_FirmwareUpload_Win_Ver_5.1.0_25-11-28.zip](https://kplusk-messtechnik.de/downloads/files/KK_FirmwareUpload_Win_Ver_5.1.0_25-11-28.zip)

[来源说明](https://kplusk-messtechnik.de/downloads/index.html)

大小：1489237 字节；SHA256：`95AA33D15C8E13FD8B16075482222BCD9AC2D0D774670B3DAAD5CE4FD8F52D2C`。

验证策略：`official-sha256`；分发依据：`pending`；安装验证：`verified`；目标环境验证：`pending`。

该官方归档未签名，仅按固定哈希及指定解压成员验证，不标记为签名通过。

## K+K KK Library for Windows

组件 ID：`kplusk-library-win`；版本：21.2.2 (2025-11-28)。

K+K FXE Ethernet/USB protocol library and native development files

1. [官方直连 — KK-Library_Win_Ver_21.2.2_25-11-28.zip](https://kplusk-messtechnik.de/downloads/files/KK-Library_Win_Ver_21.2.2_25-11-28.zip)

[来源说明](https://kplusk-messtechnik.de/downloads/index.html)

大小：2962990 字节；SHA256：`7F457FA4039B3C4F0B6E16C096650419BEB33ED9115F5A548D3FB7A438EEDB2F`。

验证策略：`official-sha256`；分发依据：`pending`；安装验证：`verified`；目标环境验证：`pending`。

该官方归档未签名，仅按固定哈希及指定解压成员验证，不标记为签名通过。

## K+K Python Native Access Modules

组件 ID：`kplusk-python-wrapper`；版本：2.1。

Official Python wrapper and examples for KK Library

1. [官方直连 — PythonModules_2.1.zip](https://kplusk-messtechnik.de/downloads/files/PythonModules_2.1.zip)

[来源说明](https://kplusk-messtechnik.de/downloads/index.html)

大小：26394 字节；SHA256：`56C2BBFD51320F593DCC92429706D2B2DE0A91A715D38026102B123587646EB0`。

验证策略：`official-sha256`；分发依据：`pending`；安装验证：`verified`；目标环境验证：`pending`。

该官方归档未签名，仅按固定哈希及指定解压成员验证，不标记为签名通过。

## TimeLab / TSERVE64 / 53100A USB driver

组件 ID：`tserve`；版本：1.780A (2025-12-04); USB 3.4.7.1。

53100A command/data server

1. [官方直连 — timelab-1.780a.exe](https://www.miles.io/timelab/mchp/setup.exe)

[来源说明](https://www.microchip.com/en-us/product/53100A)

大小：51742336 字节；SHA256：`a4eac3cace9eb52f648ea5b25d0c8e5ae5d02874429e33fc3215b8b6e33dc538`。

验证策略：`authenticode`；分发依据：`pending`；安装验证：`verified`；目标环境验证：`pending`。

## Tftpd64 DHCP

组件 ID：`tftpd64`；版本：4.74。

Dedicated lab Ethernet DHCP service; other services disabled

1. [官方直连 — Tftpd64_Service_Installer_v4.74.exe](https://github.com/PJO2/tftpd64/releases/download/v4.74/Tftpd64_Service_Installer_v4.74.exe)

[来源说明](https://pjo2.github.io/tftpd64/)

大小：820872 字节；SHA256：`175818a8f1bd75aededde4137761a24f850e4e43567f66293f759448999a2f78`。

验证策略：`authenticode`；分发依据：`pending`；安装验证：`pending`；目标环境验证：`pending`。

## Microsoft VC++ x64 runtime

组件 ID：`msvc-runtime`；版本：14.51.36247.0。

Native Qt/SDK runtime dependencies

1. [官方直连 — vc_redist.x64.exe](https://aka.ms/vc14/vc_redist.x64.exe)

[来源说明](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)

大小：18731856 字节；SHA256：`843068991daaa1f73ad9f6239bce4d0f6a07a51f18c37ea2a867e9beca71295c`。

验证策略：`authenticode`；分发依据：`pending`；安装验证：`verified`；目标环境验证：`pending`。

## 开发环境材料

冻结程序包含 Python/Qt；以下资源只供源码开发准备。

- [清华 PyPI 镜像（默认）](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/)：版本及哈希由 `uv.lock` 锁定。
- [阿里云 PyPI 镜像（显式备用）](https://mirrors.aliyun.com/pypi/simple/)：使用 `setup-dev.ps1 -Mirror Aliyun`，无国外回退。
- [Python 3.12.10 x64 国内镜像](https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe)、[官方版本说明](https://www.python.org/downloads/release/python-31210/)：26964224 字节，SHA256 `67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb`，另核验 Python Software Foundation 签名。

恢复开发 wheelhouse 使用 `scripts/prepare_dev_cache.py --output <目录>`；安装和离线构建见[部署指南](../development/desktop-deployment.md)。
