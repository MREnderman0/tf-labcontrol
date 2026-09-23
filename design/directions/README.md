# TF-LabControl 视觉方向初稿

这些文件只用于选择 UI 视觉方向，不替换当前 PySide6 壳子，也不连接真实设备。

## 三个方向

1. `01-midnight-instrument.html`：仪器室 / 工程控制台
   - 深色、紧凑、状态优先，接近设备控制台和工程软件。
   - 适合高频手动操作、批量设备管理、告警和日志。
   - 代价是视觉更严肃，长时间阅读需要控制对比度。
2. `02-lab-notebook.html`：实验记录本 / 科学编辑风
   - 浅色、纸张感、清晰分隔，适合实验记录和数据处理。
   - 适合老师评审、报表、配置预设和长时间使用。
   - 代价是夜间环境和大屏监测的氛围感较弱。
3. `03-signal-observatory.html`：信号观测站 / 监测中心风
   - 深色分层、强调波形和告警，适合实时监测和多设备状态。
   - 适合后续接入示波器波形、频谱轨迹和阈值告警。
   - 代价是需要严格控制高亮色数量，否则容易变成“霓虹科技风”。

## 参考依据

- [Tabler](https://github.com/tabler/tabler) / [在线预览](https://preview.tabler.io/)：紧凑后台信息层级。
- [shadcn/ui Dashboard](https://ui.shadcn.com/examples/dashboard)：间距、控件状态和模块化布局。
- [Grafana](https://github.com/grafana/grafana) / [Status history](https://grafana.com/docs/grafana/latest/panels-visualizations/visualizations/status-history/)：监测状态和告警表达。

请先选 `1 / 2 / 3`，也可以说“以 1 为主、借用 3 的趋势区”。选定后再重构 PySide6 的 QSS、卡片、导航和详情抽屉。
