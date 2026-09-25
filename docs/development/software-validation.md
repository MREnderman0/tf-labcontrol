# 软件验证与夹具

公共测试目录按 unit、integration、hardware 分层；独立型号的专属测试及夹具留在包内 tests/fixtures，旧型号夹具暂保留在顶层 fixtures/protocols。夹具来源由型号说明标注，手册编写的夹具不冒充实机录制。厂商 DLL 使用类型化函数替身，不需真实硬件。

## 覆盖内容

- 领域：参数类型、单位/范围、确认、未知操作、精确型号/后缀和选件。
- 传输：TCP 分包/粘包、IEEE 块截断/长度、字节序、VICP 头、多包、共享 VISA 生命周期。
- 型号：各注册 SCPI 型号至少一条独立命令夹具；AFG/SDG 任意波形编码、SMA 列表、GPD 状态、RIGOL 缩放、复数轨迹、TSERVE MHz/ADEV。
- SDK：DLL 缺失、错误码、PM100D/TLPMX 通道 ABI 与 reset=0、Pico block/streaming、缓冲区释放及 Stop 失败。
- 应用：20 台模拟设备全部开始持续采集后连续扫描 3 次，保持原会话并持续产出批次；原子别名租约、连接竞争、取消、provider 失败、增量合并、SQLite、中文配置/数据和进程互斥。
- UI/CLI：独立演示、固定类别、模式不隐藏设备、行/选择保持、显式资源、只读报告和采集导出。

## 执行

    uv run ruff check src tests scripts examples
    uv run ruff format --check src tests scripts examples
    uv run mypy
    uv run pytest -q
    uv run python scripts/generate_device_docs.py --check
    uv run python scripts/check_docs.py
    uv build

pytest 使用临时目录，Qt 为 offscreen。hardware 默认跳过；启用条件见[实机指南](../testing/hardware-testing.md)。软件测试不能证明仪器固件、接线、驱动选件或所有高级命令组合正确。矩阵中的 protocol_fixture_tested 只标记实际独立命令夹具覆盖的操作，不能把通用参数测试写成所有命令已通过实测。

## 历史与当前结果

[历史软件验证快照](../archive/software-validation-2026-09-23.md)保留原数字。当前结果以本次检查输出为准，不在指南中维护易过时的通过数量。软件测试与实机证据分别记录。
