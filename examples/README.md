# Python 示例

所有示例调用同一个 `ApplicationRuntime` / `SessionManager`，不另写 VISA 或 SDK 通信代码。配置必须显式提供，`--resource` 必须精确匹配一项；默认只读身份、能力和状态。

```powershell
uv run python examples/signal_generator.py --config config/examples/mock.toml --resource mock://signal --mock
uv run python examples/acquisition.py --config config/examples/mock.toml --resource mock://scope --mock --capture --kind waveform --points 64
```

真实配置替换为现场文件，去掉 `--mock`。写入必须加 `--write`；输出、触发、波形上传和启动采集等动作还需 `--confirm`。这些是命令行显式授权标志，不是示例自动确认。

| 示例 | 额外参数 | 功能 |
| --- | --- | --- |
| [signal_generator.py](signal_generator.py) | `--write --frequency 1000`；`--upload --write --confirm` | 设置信号源频率、上传 1024 点正弦（支持上传的型号）；不打开输出 |
| [power_supply.py](power_supply.py) | `--write --channel 1 --voltage 1 --current 0.05` | 先限流后设电压；不打开输出 |
| [acquisition.py](acquisition.py) | `--capture --kind waveform --initiate --confirm`；`--stream --count 10` | 波形、频谱、S 参数、标量的单次/批次采集与导出 |
| [pico.py](pico.py) | `--capture --write --confirm --kind streaming` | 显式设置 Pico 通道，block/streaming 批次 |
| [optical_power.py](optical_power.py) | `--write --wavelength 1550 --capture` | 波长设置、光功率采样 |
| [network_analyzer.py](network_analyzer.py) | `--capture --write --confirm` | 逐轨迹扫描 S11/S21/S12/S22、CSV/NPZ 与 Touchstone 导出 |

频谱仪用 `acquisition.py --kind spectrum`；3458A 用 `--kind scalar --points 1 --confirm`，默认读取已存储数据；加 `--initiate` 会显式配置触发与采样。53100A 用 `--kind scalar --quantity phase_noise`/`am_noise`/`stability`；数据口用 `--kind streaming` 且配置好格式/周期。

多步骤失败立即停止，已执行步骤不重发、不回滚。运行真实示例前退出真实 GUI/CLI，避免本软件进程互斥冲突。波形的 `points` 通常是 SDK/存储读数请求；台式示波器屏幕导出可能返回完整当前波形而不是恰好该点数。
