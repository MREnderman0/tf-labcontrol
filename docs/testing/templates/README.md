# 设备接入模板

模板只提供结构，不包含真实身份或自动执行指令。

- [型号 README 模板](model-readme.md)：复制到新增型号包，替换占位文字
- [硬件测试计划](hardware-plan.md)：复制到型号包 hardware，实际身份填写在运行配置
- [批次记录](record.md)：复制到运行目录批次
- [资产测试状态](asset-status.md)：全部设备共用的现行说明结构，补测更新对应表格

结构化 summary.json 使用[记录规范](../records.md)中的版本化摘要，由型号包转换器生成；原始 results.json 保留型号所需细节，不为了统一格式丢弃通信和恢复证据。
