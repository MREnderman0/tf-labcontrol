# 53100A USB 接入与服务配置验证（2026-09-28）

## 交付行为

独立 [53100A 型号模块](../../src/lab_control/instruments/devices/microchip_53100a/README.md)通过设备目录注册，保留原适配器标识及外部 TSERVE 配置。首页仍使用已确认的深色琥珀分类卡片；本轮没有新增正式启动入口。

扫描只查 PnP，缺驱动也进入相噪仪设备池。显式连接取得公共锁，实际读回所选 USB 身份，以独立序列号绑定自有隐藏 TSERVE，验证监听进程；断开只清理自有服务。输入/参考频率写后实际读回，支持恢复 auto；超时关闭连接、不重放命令。

## 官方资料核对与身份解法

[厂家手册](https://ww1.microchip.com/downloads/aemDocuments/documents/FTD/ProductDocuments/UserGuides/53100A-Phase-Noise-Analyzer-Users-Guide-DS50002991.pdf)第 8 章和随包 `tserve.cpp` 表明，空闲服务不提供 `show serial` 硬件序列号；`show title` 也不能代替。正式 TimeLab 1.780A 报告的 TSERVE 版本为 1.78。

厂家随包 `timelab.cpp` 的 `USBDEVICE::serial` 从 USB 序列号字符串取冒号/空格前部分。根据 [Windows USB 描述符接口](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/usbioctl/ns-usbioctl-_usb_descriptor_request)，项目只对选定父集线器端口发标准 GET_DESCRIPTOR，先核对端口驱动键和 VID/PID，再比对实际厂家、完整型号及规范化序列号。普通权限实测成功，不启动采集，也不调用设置备用接口、自检或复位。

身份来源明确为 `usb_descriptor`，不从铭牌配置或系统枚举缓存构造“设备读回”。空闲 TSERVE 直接硬件身份仍单列受阻，固件未知，`hardware_acquisition_open=false`。外部 TSERVE 必须提供实际 `show serial`，不能使用本机后备路径。

## 实机分项结论

| 项目 | 结果与证据边界 |
| --- | --- |
| 驱动 | 官方安装包验签/哈希通过，驱动绑定后错误码从 28 变为 0；驱动 3.4.7.1 |
| USB 身份 | 标准描述符实际读回厂家、型号及序列号，通过 |
| 兼容层连接与重连 | 新只读批次通过，使用自有空闲服务；未打开硬件采集 |
| 服务频率配置 | 经用户重新授权的新控制批次中，两项分别设置 10 MHz、实际读回、核对另一项不变并恢复 auto，通过 |
| 退出 | 新批次正常退出自有服务；没有遗留 TSERVE |
| 首次控制异常 | 提示符末尾空格漏读导致超时，已停止写入并修复；旧批次恢复未确认，保留原日志和补充纠正说明 |
| 相噪、AM、ADEV、物理性能 | 仅 USB 和电源，无 DUT/参考信号，全部未测试；不构成整机验收 |

每批 `record.md`、`results.json`、`summary.json`、通信与恢复证据都在正式运行目录 `hardware-tests`。公共维护器更新索引、资产状态表及离线总览；真实地址、序列号和原始厂商资料不进入源码。

## 软件和部署验证

| 检查 | 本轮结果 |
| --- | --- |
| 全项目 pytest | 2021 passed，1 skipped |
| Ruff / Mypy | 通过；Mypy 检查 228 个源文件 |
| 生成一致性及文档链接/锚点/导航 | 通过 |
| 组件登记与安装缓存验签/哈希 | 开发检查通过；本机维护缓存保留同一已验证安装包 |
| 源码部署自检 / CSV worker | 通过，未访问硬件 |
| 实际目录式 EXE / 冻结 worker | 构建通过；移到源码之外的中文路径后自检通过，损坏输入保留 |
| Wheel / 移除型号回归 | 离线构建通过；独立导入及逐型号移除检查通过 |
| 严格发布 | 保持阻断；分发授权、干净 Windows 10/11 离线交付与逐设备验收尚未完成 |

相关回归包括：缺驱动发现与合并、扫描无服务启动、USB 端口/身份不符拒绝、外部占用、服务启动超时和退出异常、旧配置、真实频率读回、auto 恢复、超时不重发及无效帧不显示/保存。部署自检是软件验证，不提升硬件测量结论。
