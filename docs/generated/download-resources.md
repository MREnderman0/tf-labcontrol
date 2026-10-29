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

### Git for Windows 2.56.0.windows.4

源码版本管理、独立克隆及历史 Git LFS 对象维护；冻结程序运行不依赖 Git。

[国内镜像](https://mirrors.huaweicloud.com/git-for-windows/v2.56.0.windows.4/Git-2.56.0.4-64-bit.exe)、[官方直连](https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/Git-2.56.0.4-64-bit.exe)、[版本说明](https://github.com/git-for-windows/git/releases/tag/v2.56.0.windows.4)。

大小：66850032 字节；SHA256：`4961a4a8cc778d8b6e00a5819e5fe4d078b754555aff7aedc2d77a5d74c2de80`。

签名：Valid (Authenticode; Johannes Schindelin)；发布者：Johannes Schindelin；许可：GPL-2.0; bundled components retain their respective licenses。

验证状态：国内镜像下载与官方 SHA256 一致，安装包签名有效；Windows 11 x64 安装返回 0，Git 与 Git LFS 版本读取通过；详细证据另存维护报告。

## 锁定的 Python 材料

由 `scripts/generate_python_materials.py` 核对锁文件、Windows x64 wheel 与已安装版本后登记。哈希用于下载验证；目标电脑、局域网与实机验收仍分别留档。wheel 不适用 Authenticode 签名。

| 包 / 版本 | 用途 | 国内来源 | 大小（字节） | SHA256 | 许可 |
| --- | --- | --- | --- | --- | --- |
| altgraph 0.17.5 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/a9/ba/000a1996d4308bc65120167c21241a3b205464a2e0b58deda26ae8ac21d1/altgraph-0.17.5-py2.py3-none-any.whl) | 21228 | `f3a22400bce1b0c701683820ac4f3b159cd301acab067c51c653e06961600597` | MIT |
| annotated-types 0.8.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/99/91/8acff4f5e50511b911bbccb72b8628a49c68ce14148cd9f6431094859a90/annotated_types-0.8.0-py3-none-any.whl) | 13427 | `f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0` | MIT |
| anyio 4.15.1 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/12/b8/4bd346e22b28902df4d651910f5242c28d84e4a5c2435ca5c3f797ed7e2e/anyio-4.15.1-py3-none-any.whl) | 132079 | `6152fdbbf9a77fdec97731721bebf7c4c44f7c29b424b0065826173efc7ed101` | MIT |
| ast-serialize 0.11.2 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/23/bf/b419c3205ce1143ba7c69baef4f0ba43c14d8712113bf34f9e0d27d609be/ast_serialize-0.11.2-cp39-abi3-win_amd64.whl) | 1165434 | `b9065dd23131a23b41f5bab3bf4e9b3c350a3fe8e36e8200eded9b729fcea484` | MIT |
| attrs 26.1.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/64/b4/17d4b0b2a2dc85a6df63d1157e028ed19f90d4cd97c36717afef2bc2f395/attrs-26.1.0-py3-none-any.whl) | 67548 | `c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309` | MIT |
| cffi 2.1.1 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/d9/79/615cc094e2fb508cade7de88d3b4f6c4ec2bab695c97bce9153dc65aadf5/cffi-2.1.1-cp312-cp312-win_amd64.whl) | 185919 | `f53e442b08449d42821fa4a4fba000095af9f62742a500f978a9f557ec44339a` | MIT-0 |
| click 8.5.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/58/50/6c0d534c5f134586a8e1ba4e330569e32f057e33372ae556463212fb4cd3/click-8.5.0-py3-none-any.whl) | 125251 | `255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360` | BSD-3-Clause |
| colorama 0.4.6 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/d1/d6/3965ed04c63042e047cb6a3e6ed1a63a35087b6a609aa3a15ed8ac56c221/colorama-0.4.6-py2.py3-none-any.whl) | 25335 | `4f1d9991f5acc0ca119f9d443620b77f9d6b33703e51011c16baf57afb285fc6` | pending |
| cryptography 47.0.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/b2/dc/1b901990b174786569029f67542b3edf72ac068b6c3c8683c17e6a2f5363/cryptography-47.0.0-cp311-abi3-win_amd64.whl) | 3775309 | `f5c3296dab66202f1b18a91fa266be93d6aa0c2806ea3d67762c69f60adc71aa` | Apache-2.0 OR BSD-3-Clause |
| h11 0.16.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/04/4b/29cac41a4d98d144bf5f6d33995617b185d14b22401f75ca86f384e87ff1/h11-0.16.0-py3-none-any.whl) | 37515 | `63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86` | MIT |
| httpcore2 2.16.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/af/c2/ce417c397afa5181f0e3c6ff01a4d01639414e1adef08730d97571297151/httpcore2-2.16.0-py3-none-any.whl) | 85070 | `5c4358319f2602d984ad93182c9fee5160f22e9da7babdda870a5e7c4e2dc34f` | BSD-3-Clause |
| httpx2 2.16.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/7b/3d/2e4bc4fd1ef9770871669adb348d612a6e4786b303a23f9935b81c48dd60/httpx2-2.16.0-py3-none-any.whl) | 97880 | `fd2c5d7909e8d86fe92d53020c310ca0e4e431b838c207c5754b9bbf0308d49a` | BSD-3-Clause |
| idna 3.20 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/58/a2/bb081bab032533a855d44de1d56f8e8426114ff1ba5d1f07a438a0a654f8/idna-3.20-py3-none-any.whl) | 69583 | `ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c` | BSD-3-Clause |
| iniconfig 2.3.0 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/cb/b1/3846dd7f199d53cb17f49cba7e651e9ce294d8497c8c150530ed11865bb8/iniconfig-2.3.0-py3-none-any.whl) | 7484 | `f631c04d2c48c52b84d0d0549c99ff3859c98df65b3101406327ecc7d53fbf12` | MIT |
| jsonschema 4.26.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/69/90/f63fb5873511e014207a475e2bb4e8b2e570d655b00ac19a9a0ca0a385ee/jsonschema-4.26.0-py3-none-any.whl) | 90630 | `d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce` | MIT |
| jsonschema-specifications 2025.9.1 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/41/45/1a4ed80516f02155c51f51e8cedb3c1902296743db0bbc66608a0db2814f/jsonschema_specifications-2025.9.1-py3-none-any.whl) | 18437 | `98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe` | MIT |
| librt 0.15.0 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/38/d0/2ae99c83be86ce23f925ac1aeeedc777e97f427c4a8d190c70d0a16e9a87/librt-0.15.0-cp312-cp312-win_amd64.whl) | 126974 | `73b30cfa976659b3917c8f6153bdb0591c6a9ec6583599fd24a689b690622022` | MIT |
| mcp 2.3.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/20/d7/4a2878f128df729db3ca04e71bfce26405cc2e0c9ebe19c592feeb4c61b9/mcp-2.3.0-py3-none-any.whl) | 368664 | `dd0c44c089d16453e8ae31a3877a0054d7a2314caaa81f5e0541b9b1734b2377` | MIT |
| mcp-types 2.3.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/ce/48/32583066b155bb5d4507e7887dc88ee23118cc4de8e2a12870c49719e2a8/mcp_types-2.3.0-py3-none-any.whl) | 69108 | `968efdbdaedfab06adae40d378a34395f1090c5921d4be3c9cde283aaf76d91d` | MIT |
| mypy 2.3.1 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/a3/52/cb31e084bc0314a1e384bdd677a4b80e55af04ccac077545e2238b9d320a/mypy-2.3.1-cp312-cp312-win_amd64.whl) | 11226359 | `4c5095a327483591c94e0c8d3ef9e50d4ab1369b541eae007c1f23bc2a41f6bb` | MIT |
| mypy-extensions 1.1.0 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/79/7b/2c79738432f5c924bef5071f933bcc9efd0473bac3b4aa584a6f7c1c8df8/mypy_extensions-1.1.0-py3-none-any.whl) | 4963 | `1be4cccdb0f2482337c4743e60421de3a356cd97508abadd57d47403e94f5505` | MIT |
| numpy 2.5.3 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/3c/a1/accf6d4f0c80c5d9ba9735d6b1550e444180599f34dec69ca01360f717ad/numpy-2.5.3-cp312-cp312-win_amd64.whl) | 12567828 | `0a59a421a32580a009e8a1751345bf829631b990dc1794b80514ab722b435def` | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| opentelemetry-api 1.45.1 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/1e/41/f7dcf80b81ee8e71c1a2b59f14208bc723edbd89ed027a73b175abf6348e/opentelemetry_api-1.45.1-py3-none-any.whl) | 60256 | `b31553efa588ae44bc306f863c785c5333a9ecc091248c6ee68b4b6c87fdedfb` | Apache-2.0 |
| packaging 26.3 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/63/34/ba1c580383c9eada3711951fef0795c80b829a078d72188184bcab9dd527/packaging-26.3-py3-none-any.whl) | 129956 | `d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c` | Apache-2.0 OR BSD-2-Clause |
| pathspec 1.1.1 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/f1/d9/7fb5aa316bc299258e68c73ba3bddbc499654a07f151cba08f6153988714/pathspec-1.1.1-py3-none-any.whl) | 57328 | `a00ce642f577bf7f473932318056212bc4f8bfdf53128c78bbd5af0b9b20b189` | pending |
| pefile 2024.8.26 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/54/16/12b82f791c7f50ddec566873d5bdd245baa1491bac11d15ffb98aecc8f8b/pefile-2024.8.26-py3-none-any.whl) | 74766 | `76f8b485dcd3b1bb8166f1128d395fa3d87af26360c2358fb75b80019b957c6f` | MIT |
| pluggy 1.6.0 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/54/20/4d324d65cc6d9205fabedc306948156824eb9f0ee1633355a8f7ec5c66bf/pluggy-1.6.0-py3-none-any.whl) | 20538 | `e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746` | MIT |
| pycparser 3.12 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/b6/ae/315bbeee126a769cbde43cf4908acb4b40408d8bf8bbf334dd7368d8dadf/pycparser-3.12-py3-none-any.whl) | 62600 | `a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e` | BSD-3-Clause |
| pydantic 2.14.1 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/35/ea/a56b9fe5066f3537b7882f77e9c5dfb26d8c2eefdaed9b5fc73d57b4dc22/pydantic-2.14.1-py3-none-any.whl) | 488106 | `9195d967ec791692a04438115466764fb8b9a27b31f14a760437694f40d6b454` | MIT |
| pydantic-core 2.50.1 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/c5/17/8bbd530b8659e9d963f5b16f6cb8e159f4cd174b4d6304e48969ab12dba8/pydantic_core-2.50.1-cp312-cp312-win_amd64.whl) | 2172522 | `132529c83901437ff642f585216831bf5fd7a91df66829907e155192ead62498` | MIT |
| pygments 2.21.0 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/71/46/17f022dd3e953bf20a04a028a21ec746d942f8d2af30fa0f124fa0e6a684/pygments-2.21.0-py3-none-any.whl) | 1250147 | `2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9` | BSD-2-Clause |
| pyinstaller 6.22.3 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/24/83/c5422204772a27aef643280fdc887b7ef55679989096b21dceb1b9625fc4/pyinstaller-6.22.3-py3-none-win_amd64.whl) | 1454907 | `500bd58c7bf7e584a8435adccbd763a0b918d5c12b08d74ff50fd79b2915458b` | GPLv2-or-later with a special exception which allows to use PyInstaller to build and distribute non-free programs (including commercial ones) |
| pyinstaller-hooks-contrib 2026.7 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/0a/67/350377af7b50416344ab8792756d414eef7629c618a73e9a0b13bb1552d9/pyinstaller_hooks_contrib-2026.7-py3-none-any.whl) | 459445 | `24257a04c7a5a7a034cf28e39dcee20fbeeb9f043076729480f2e1b69904408a` | pending |
| pyjwt 2.15.1 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/50/ca/44de4e75f8aadc457f0634be3b542815078ded46dca30efb960edeecad6e/pyjwt-2.15.1-py3-none-any.whl) | 33860 | `42d59d631f7768a1028a64c7ff581a9bf7519804daf91fc5b6c56e30eec5e193` | MIT |
| pyqtgraph 0.14.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/32/36/4c242f81fdcbfa4fb62a5645f6af79191f4097a0577bd5460c24f19cc4ef/pyqtgraph-0.14.0-py3-none-any.whl) | 1924755 | `7abb7c3e17362add64f8711b474dffac5e7b0e9245abdf992e9a44119b7aa4f5` | MIT |
| pyserial 3.5 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/07/bc/587a445451b253b285629263eb51c2d8e9bcea4fc97826266d186f96f558/pyserial-3.5-py2.py3-none-any.whl) | 90585 | `c4451db6ba391ca6ca299fb3ec7bae67a5c55dde170964c7a14ceefec02f2cf0` | BSD |
| pyside6 6.11.2 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/94/fe/4aea9d30300fff4cda9bb729c1363b9e1095a8544e0d6b57065465e16599/pyside6-6.11.2-cp310-abi3-win_amd64.whl) | 578382 | `3201d67e3c10be2eaedd3910ff0f02351eca7e88c95a291cde5e7f2f55ef207f` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| pyside6-addons 6.11.2 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/fb/c1/08bf6594101b42fc88834009f1dac59a983c5b6805c62c5081f077d09b4a/pyside6_addons-6.11.2-cp310-abi3-win_amd64.whl) | 168208836 | `f449ea4431da20e7b86752cca8d166f93434516fe417f981c27e5f8e1b554407` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| pyside6-essentials 6.11.2 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/dc/09/d2662684aee4d3f027ff9b1e965f22301bc28ffc5b7f167658508e4475dc/pyside6_essentials-6.11.2-cp310-abi3-win_amd64.whl) | 76913043 | `c8a29def77032773a30879f7f24415b5395ad08592d147c170824ef4c735dfc1` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| pytest 9.1.1 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/24/25/1de2678b631f5a49215c6c96fff41ba892b0a34df68d6d80292b1b48aa7f/pytest-9.1.1-py3-none-any.whl) | 386536 | `37a86b45efb9a47a61a36449063e8e18d0cab3161329fc099eb21783169c4f0c` | MIT |
| pytest-qt 4.5.0 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/cc/d0/8339b888ad64a3d4e508fed8245a402b503846e1972c10ad60955883dcbb/pytest_qt-4.5.0-py3-none-any.whl) | 37214 | `ed21ea9b861247f7d18090a26bfbda8fb51d7a8a7b6f776157426ff2ccf26eff` | MIT |
| python-multipart 0.0.32 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/e1/04/e8135ebd1ad02c56ec633277529b2602ff99ff634be76cdba5744cf554fd/python_multipart-0.0.32-py3-none-any.whl) | 30042 | `ff6d3f776f16878c894e52e107296ffc890e913c611b1a4ec6c44e2821fe2e23` | Apache-2.0 |
| pyvisa 1.16.2 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/e4/a9/776da4f397003cca093659524c3526590522f81b642936838428c11274e6/pyvisa-1.16.2-py3-none-any.whl) | 181413 | `54f034adafd3e8d1858d57cdafec64e920444f4b84b31c9fd17487fbad0a197a` | The MIT License |
| pywin32 312 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/03/d9/77040d3b43df3f3be32ea289433d660d2727f5ba327bc73be835127d9d60/pywin32-312-cp312-cp312-win_amd64.whl) | 6914841 | `b457f6d628a47e8a7346ce22acb7e1a46a4a78b52e1d17e1af56871bd19a93bc` | PSF |
| pywin32-ctypes 0.2.3 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/de/3d/8161f7711c017e01ac9f008dfddd9410dff3674334c233bde66e7ba65bbf/pywin32_ctypes-0.2.3-py3-none-any.whl) | 30756 | `8a1513379d709975552d202d942d9837758905c8d01eb82b8bcc30918929e7b8` | BSD-3-Clause |
| referencing 0.37.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/2c/58/ca301544e1fa93ed4f80d724bf5b194f6e4b945841c5bfd555878eea9fcb/referencing-0.37.0-py3-none-any.whl) | 26766 | `381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231` | MIT |
| rpds-py 2026.9.1 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/1c/84/1700cc748d0eaa747486e4e82882d2575224e6e9a3148df583058850c629/rpds_py-2026.9.1-cp312-cp312-win_amd64.whl) | 222923 | `5ce8943f79c2210f7abcc28e86367b03b28d95027fd01c46d2472373ae70c86f` | MIT |
| ruff 0.16.8 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/1e/5d/a509c07d714b6da88f2c518b4637cf6f1d46b074be8f0f1e5fb9ff5126fe/ruff-0.16.8-py3-none-win_amd64.whl) | 10586899 | `a6b85621fd3c81e31fc5f5add09c9c078b430db3595ca632efafdec9e64ebfaa` | MIT |
| setuptools 84.0.0 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/95/9c/c510029fc6ef33a6275cd2c5d3cecd6613dfd6aa401d57c54f1c18852ccf/setuptools-84.0.0-py3-none-any.whl) | 818216 | `51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670` | MIT |
| shiboken6 6.11.2 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/ce/6b/fee1b4814317e4664e318644c9c007676c65a71e5b2b9d3ac4121a94dc0a/shiboken6-6.11.2-cp310-abi3-win_amd64.whl) | 1226578 | `6ab0eba1c904455df621f9a6df3ca2bb896bab8670572d2bc4e37804ae91f19a` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| sse-starlette 3.6.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/cf/49/35053078cdfcf639237e8bee6dc6f34f1b3a3cc8d01800a1e213e1660ff6/sse_starlette-3.6.0-py3-none-any.whl) | 17985 | `e4dfa4d237d2712cb2b4d4918eda9b457cd6b44bbecdc838af75de836565bafd` | BSD-3-Clause |
| starlette 1.10.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/88/2e/947cfe02def9371c07cd5bf5b6b61c829e9aecb9904db02a66cfafbc1eec/starlette-1.10.0-py3-none-any.whl) | 80571 | `478c0ab3b4ced6e82719849c118fa406f0278a3499c1a898cd15cd31bdc38ce4` | BSD-3-Clause |
| truststore 0.10.5 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/51/e9/3a7820be2bb0fe53b6bc9c3be26d3d1158004e4c3ab953aa6840b955b1e9/truststore-0.10.5-py3-none-any.whl) | 19017 | `9aaaedaefaf06d8b206278cf8b5012bc897f485a874503501e12d776df78951c` | MIT |
| types-pyserial 3.5.0.20260712 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/ab/66/00bb637724c659494a41687bf06ac613a860720f7183109fca62986d4f69/types_pyserial-3.5.0.20260712-py3-none-any.whl) | 25323 | `ee4f34b7432a0608bb016c88cb5b1ad3b960df4784289de56dda471503ceb53d` | Apache-2.0 |
| typing-extensions 4.16.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/typing_extensions-4.16.0-py3-none-any.whl) | 45571 | `481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8` | PSF-2.0 |
| typing-inspection 0.4.4 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/67/81/4add07e5172b7ac40d8ed5ff580409a7801a4fe26d529bdd915401dabfbe/typing_inspection-0.4.4-py3-none-any.whl) | 14750 | `65b8397ba37ccbce054456aaccddfc91e6e3083c92824df348d96ca832f3f147` | MIT |
| uv 0.12.18 | development/build | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/86/b3/5f33b9bad8cb875ed547ef8aa403d423a2610f23f619f687308761dd02e1/uv-0.12.18-py3-none-win_amd64.whl) | 17986064 | `0a3c4e415515bd4c24c7516523d9042a2957ce716423e193f8b2be9fb8773340` | MIT OR Apache-2.0 |
| uvicorn 0.54.0 | desktop runtime and Agent integration | [下载](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/38/0c/b54a4fdd7f90a3af8b02ebc9ce6712c2c208b7926a2f7bad95c33ebbe943/uvicorn-0.54.0-py3-none-any.whl) | 87427 | `505bdb0f318731d45f1f712071fc781a8981f6847a31c902c9f5e652d4f67faf` | BSD-3-Clause |
