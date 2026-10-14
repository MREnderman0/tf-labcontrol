<!-- Generated from an independent device package; edit its README.md. -->

维护源：[包内 README](../../../src/lab_control/instruments/devices/coremorrow_xmt/README.md)。修改维护源后重新生成。

# 芯明天 XMT 串口协议（完整型号待核）

注册标识为 `coremorrow.xmt`。这是一个独立的、保守的协议兼容包，依据本机芯明天厂家软件 25.09.26 的串口 DLL 头文件和 COM6 只读回包建立。它不把 USB 桥的 VID/PID、桥序列号或回包占位字节当成设备型号/序列号，因此当前身份结论是“芯明天 XMT 协议兼容控制器，完整型号待核”，不能据此确认是 E09。

当前 COM6 资产的用户现场铭牌核对结果为 `E00.C3-13`。该人工观察只更新资产台账，不改变本包对 XMT 协议族的保守匹配；串口回包仍未提供可核验的仪器序列号。

## 能力与协议

已验证帧格式为 `AA address total_length B3 B4 payload XOR`。长度包含整帧，末字节为前面所有字节的逐字节 XOR。当前包只实现两个只读能力：

- `power_config`：`B3=80, B4=0`，解析通道数、传感器类型、带宽代码及 DA/AD 分辨率，并保留完整脱敏原始 payload。
- `raw_channel_data`：默认 `B3=5, B4=0`，按通道返回原始 4 字节值；字节缩放和物理单位仍待厂家按完整型号确认。

所有设置、校准、输出、波形和带单位采集都拒绝执行。`channels=<n>`（1～8）和 `data_command=5|6` 可在现场配置中显式选择；默认按 COM6 已读到的 3 通道处理。

## 连接与依赖

支持 `transport=serial` 的 COM 端口和 `transport=visa` 的 `ASRL...::INSTR`。串口使用 8N1、无流控、无终止符，波特率由配置指定；COM6 在 9600 和 115200 下均得到相同配置帧。运行时只依赖项目已有的 pyserial/PyVISA 和 `serial-interface` 组件；32 位厂家 DLL 仅作为现场证据，不加载进项目 x64 进程，也不随源码分发。

## 测试与限制

型号专属测试：

```powershell
.venv-dev\Scripts\python.exe -m pytest src/lab_control/instruments/devices/coremorrow_xmt/tests -q
```

实机只读入口位于 `hardware/readonly.py`，必须显式指定一个 COM 端口和人工资产目录。每批保存原始通信、结构化结果和派生摘要；参数写入、恢复和物理输出测量均保留为未测试，不能据软件回包宣称整机验收通过。

硬件计划与留档入口见 [hardware/README.md](../../../src/lab_control/instruments/devices/coremorrow_xmt/hardware/README.md)。

待补资料：厂家确认的完整型号（包括是否为 E09）、仪器序列号/固件读取方法、B3=5/6 的单位和缩放、停止位书面说明、可恢复的参数范围与控制流程、外部物理输出测量。补齐后应在本包扩展能力并新增独立硬件批次，不把型号分支放入公共层。

## 注册能力（自动生成）

范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `power_config` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `raw_channel_data` | 无 | 通道 | 读 | 无需 | 软件已实现；实测按资产查证 |
