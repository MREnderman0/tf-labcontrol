# 53100A（Microchip 候选）

## 1. 型号与可信度

注册 ID：`microchip.53100a`。型号精确匹配，当前为软件实现、待实机验证；附件简写和身份别名见本卡片说明。软件连接成功不等于验收通过。

## 2. 电脑物理连接

仪器由已配置 TimeLab/TSERVE 服务控制；电脑连接其命令端口 1299 / 数据端口 1298。接口和选件以设备背板、固件与现场盘点为准。

## 3. Windows 前置条件

厂家 TimeLab/TSERVE；本应用不安装或启动服务。安装步骤见[实机测试指南](../../hardware-testing.md)；DLL 放在厂商安装位置，通过绝对路径加载，不复制进源码。

## 4. 资源与枚举

连接模板见[config/examples](../../../config/examples/README.md)。VISA 使用扫描返回的完整资源；串口填写确认的 COM/波特率；Pico 使用 `PICO::序列号`；PM100D 使用 SDK 枚举的资源；53100A 手工填写已部署服务地址。扫描只枚举 SDK，不打开 SDK 仪器。

## 5. 官方命令/API 依据

[厂家资料](https://ww1.microchip.com/downloads/aemDocuments/documents/FTD/ProductDocuments/UserGuides/53100A-Phase-Noise-Analyzer-Users-Guide-DS50002991.pdf)；版本：SDK headers/examples or 53100A DS50002991G。逐操作命令定位、参数和模板见[自动生成能力清单](../../generated/capability-matrix.json)。测试样本是按手册编写的夹具，尚非实机抓包。

## 6. 当前统一能力

逻辑类别 `phase_noise_analyzer`；通道 `(1,)`。`None` 表示该侧未在通用参数层给出数值边界；适配器仍执行关联参数与通道校验。未列出的操作返回 `unsupported`。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `start` | 无 | 整机 | /写 | 需要 | 已实现；待实机 |
| `stop` | 无 | 整机 | /写 | 需要 | 已实现；待实机 |
| `input_frequency` | value: number Hz [1, 200000000.0] | 整机 | /写 | 无需 | 已实现；待实机 |
| `reference_frequency` | value: number Hz [1, 200000000.0] | 整机 | /写 | 无需 | 已实现；待实机 |
| `phase_noise` | 无 | 整机 | 读 | 无需 | 已实现；待实机 |
| `am_noise` | 无 | 整机 | 读 | 无需 | 已实现；待实机 |
| `stability` | 无 | 整机 | 读 | 无需 | 已实现；待实机 |
| `data_stream` | 无 | 整机 | 读 | 无需 | 缺选件/配置 |

## 7. 控制、读回与采集

页面入口：**实时监测 / 相噪仪**。参数由当前注册能力生成；常用、高级、状态与诊断分组。只读采集能力通过监测页的单次/开始操作使用；缺选件项显示原因且禁用。详细操作见[控制与监测指南](../../control-and-monitoring.md)。

采集路线：`phase_noise/am_noise/stability/streaming`。`apply` 返回成功、拒绝、部分执行或不确定；只有提供读回命令的操作才有硬件读回，不能将 SDK 已接收设置描述成测量值。`status` 显式读取，扫描和总览定时刷新不会额外采集。

需现场确认厂家、型号、序列号和 TSERVE 控制权，并设置 identity_confirmed=true。频率公共单位 Hz，命令单位 MHz。相噪/AM/ADEV 经命令口读取；数据口仅接收已配置 P/F 格式，timestamp=0，stream_interval 与服务一致。

## 8. 限制和故障处理

单设备单 worker 串行执行。数值范围是软件保守边界，仍需核对负载、频率、探头和接线条件。输出/触发等动作要求明确确认；超时不重发、不自动回滚，先诊断再重连。断开软件连接不表示输出关闭。错误数据使用质量标记，不能以零值代替。

## 9. Python 落点

通过 `ApplicationRuntime` → `SessionManager.submit()` → 注册表 `microchip.53100a` 调用；SCPI 型号共用所属类别的实现和独立 `Profile`，不创建空壳同名类。源码见 [instruments](../../../src/lab_control/instruments/)；示例见 [examples](../../../examples/README.md)。

## 10. Mock 与实机验收

软件：身份精确匹配、参数越界、未知能力和控制夹具；SDK 使用函数替身验证参数/句柄/缓冲区。完整结果以 `pytest` 报告为准，不能从该表推断全部高级命令已逐项验证。

现场按[统一验收步骤](../../hardware-testing.md)记录完整身份、固件、驱动、资源、每个测试操作、请求、读回、原始错误、数据文件和恢复结果。先运行只读 `test`，再选择本卡片中一项设置和一次基础采集，最后测试断线重连。每项通过后将报告与具体能力关联；本轮所有能力的 `hardware_passed=false`。
