# 项目文档

从要做的事情进入。型号协议、范围、专属测试和说明以型号包为维护源；具体资产的实测证据只保存在运行目录。

## 使用软件

- [安装与启动](../README.md)
- [控制与监测](guides/control-and-monitoring.md)、[输入范围规则](guides/input-ranges.md)
- [相噪定时采集与 ADEV/MDEV](requirements/realtime-monitoring.md#相噪仪定时采集)：采样率、时长、自动保存及实测边界
- [光功率计全通道实时监测](guides/control-and-monitoring.md#光功率计全通道监测)：通道筛选、W 显示与自动连续记录
- [连接与实机操作](testing/hardware-testing.md)、[实验网发现](architecture/lan-discovery.md)
- [实验网络部署与换电脑](guides/lab-network-deployment.md)：通用 DHCP 地址池及本机配置准备
- [配置示例](../config/examples/README.md)、[Python 示例](../examples/README.md)

## 接入新设备

- [新设备接入流程](testing/onboarding.md)、[模板](testing/templates/README.md)
- [型号导航](devices/README.md)、[原始设备清单对应](device-research-index.md)
- [能力矩阵](generated/capability-matrix.md)及 [JSON](generated/capability-matrix.json)
- [设备模块维护规范](architecture/device-modules.md)

## 查看测试记录

- [频谱仪紧凑监测与标记](ui-spectrum-review.md)
- 光学光谱仪实时监测页面与补测以型号包 README 及运行目录批次为准
- [电源四宫格操作与验收](ui-power-supply-review.md)

- [实机记录与离线台账](testing/records.md)：逐台、逐接口、逐项目及待核对项
- [软件验证](development/software-validation.md)、[界面验收记录](reviews/README.md)
- [历史档案](archive/README.md)：历史数字和设想不代表当前状态

## 开发维护

- [开发环境](development/environment.md)、[依赖与资料](development/references.md)
- [桌面部署与环境修复](development/desktop-deployment.md)：组件台账、离线材料、EXE 构建和设置内检查
- [文档维护约定及迁移清单](development/documentation.md)
- [系统架构](architecture/system-architecture.md)、[技术栈](architecture/technology-stack.md)、[前端架构](architecture/frontend-framework.md)
- [兼容接口](protocols/device-compatibility.md)、[传输协议](protocols/visa-scpi.md)、[数据模型](data/data-model.md)
- [需求与实现边界](requirements/overview.md)
- [协作要求](../AGENTS.md)、[确认的设计基线](../direction-approved.md)
