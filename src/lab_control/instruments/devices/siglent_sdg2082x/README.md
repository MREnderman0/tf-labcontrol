# SIGLENT SDG2082X

注册标识：`siglent.sdg2082x`。本目录集中保存双通道基础兼容层、文档、测试和硬件复核入口，不复用 SDG6032X 的型号能力或范围。

## 能力与协议

`adapter.py` 提供 SINE/SQUARE/RAMP/PULSE/DC、频率、幅度、偏置和输出控制；负载、调制、扫频、突发与耦合状态只读。

- 正弦最高 80 MHz、方波/脉冲 25 MHz、斜坡 1 MHz；DC 不提供频率和幅度设置。
- 幅度统一为 Vpp。按 HiZ/50 Ω 负载及 20 MHz 分界校验幅度上限，并检查幅度与偏置的联合限制；其他未核实负载拒绝参数写入。
- 通道跟踪或耦合打开时拒绝独立参数修改；输出 OFF 仍可执行。
- 复合响应解析兼容有/无通道头；状态读取考虑 DC 和噪声响应中缺少的周期参数，不伪造零值。

依据：[编程手册 PG02-E05A](https://siglentna.com/wp-content/uploads/dlm_uploads/2023/01/SDG_Programming-Guide_PG02-E05A-12.pdf)、[SDG2000X EN02H 数据表](https://siglentna.com/wp-content/uploads/dlm_uploads/2025/11/SDG2000X_DataSheet_EN02H.pdf)。

## 连接与驱动

USB USBTMC 或 LAN VXI-11，优先 PyVISA；复用可用的 x64 VISA 和 USBTMC 驱动。使用单设备配置指定完整型号、实际资源和预期序列号，不根据系列名宽泛匹配。

## 文件与公共依赖

`__init__.py` 声明注册/文档元数据；`adapter.py` 保存范围、解析和写入约束；`tests/` 保存专属测试，`tests/fixtures/` 保存能力基线和脱敏协议样本；`hardware/readonly.py` 提供只读复核。

公共依赖仅包括领域类型、SCPI 基类和 `common/siglent.py` 的复合字段解析；该解析器不含型号名或型号范围。模块不依赖其他型号包或旧信号源集合。

## 测试入口与实机状态

```powershell
.venv-dev\Scripts\python.exe -m pytest src/lab_control/instruments/devices/siglent_sdg2082x/tests -q
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg2082x.hardware.readonly --config '<运行目录内单设备配置>'
```

已测资产具备 USB 扫描、连接、双通道基础读写、恢复和重连证据，输出关闭。完整原始测试脚本及通信数据保留在资产历史批次中；不将含真实身份的脚本复制进源码。

高级功能写入、任意波形、实际波形和输出精度未验收，也不因其他 SIGLENT 型号支持而自动开放。

## 扩展与移除

遵循[设备模块规范](../../../../../docs/architecture/device-modules.md)。新增操作须在本包提供型号依据、校验及测试；删除注册项和本包后重新生成文档。保留所有现场历史记录。
