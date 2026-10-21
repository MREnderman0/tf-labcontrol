# 源码仓库瘦身与首次准备验收

批次：2026-10-19。范围为 Windows 开发机的软件验证、缓存恢复和目录式构建；本次未连接真实设备、安装驱动、修改网络或系统代理。原开发环境、有效旧构建、实机记录与历史 LFS 对象保留。

## 当前结论

Git 只保留开发材料与资源清单；57 个第三方载荷已移出索引并加入忽略规则，本机文件逐项重新计算 SHA256，全部与迁移前一致。[迁移清单](../../deployment/payload-migration.json)保留路径、大小、哈希和来源；[旧缓存清单](../../deployment/packages/manifest.json)及[组件清单](../../deployment/components.json)用于恢复。变更需提交并推送后才作用于远端的新 clone，本次不改写历史。

| 项目 | 实测结果 |
| --- | --- |
| 原跟踪实体大小 | 842,657,702 字节，803.62 MiB |
| 移出索引且本机保留 | 793,520,637 字节，756.76 MiB，57 个载荷 |
| 新源码树（含新增代码/测试/文档） | 约 47 MiB，减少约 94% |
| 联网目录式程序 | 166,096,453 字节，158.40 MiB；`dist/online-final/TF-LabControl` |
| 离线测试包 | 716,394,375 字节，683.21 MiB；`dist/offline-final/TF-LabControl` |
| 独立开发缓存 | 32 个载荷，350,130,932 字节；31 个 wheels 与 uv 工具 |
| 源码发行压缩包 | 约 1.62 MiB；不含厂商载荷或评审截图，保留已确认设计截图 |

离线包选择 VISA、PicoSDK、Thorlabs OPM、TSERVE、Tftpd64、VC++、K+K 配置工具和 wrapper；依赖闭包自动加入 K+K Library。未登记完整的 FTDI、Keithley USB、通用串口和 GPIB 驱动不在此测试包内，默认“全部组件”离线构建会报告缺项。上述包均为开发测试包，`release_ready=false`。自动审批拒绝清理/替换初步构建目录，故初步构建和安装验证临时副本也保留；最终构建使用上述带 `-final` 的路径。

## 验证证据

| 检查 | 结果与边界 |
| --- | --- |
| 干净源码联网准备 | 无 `.git`、LFS 实体和 wheelhouse，使用隔离缓存从清华镜像安装；CLI、模块导入及启动自检通过，`uv.lock` 不变 |
| 干净源码离线准备 | 使用完整旧缓存、`-Offline` 安装；CLI、模块导入及启动自检通过，锁文件不变 |
| LFS 指针和缺包 | 不完整指针缓存被开发脚本拒绝；锁文件不变，未尝试国外回退 |
| 显式构建依赖 | 普通准备不安装 PyInstaller；`-Build -Offline` 追加锁定构建工具，锁文件不变 |
| 官方直接下载 | 空缓存实际下载 K+K Library 和 Python wrapper，固定归档及成员哈希通过，明确报告 NotSigned |
| 组件与 GUI 回归 | 最终 54 项相关回归通过，覆盖下载中取消/重试、旧缓存根目录和按设备/接口推荐组件 |
| 全量软件回归 | 2,289 passed，1 skipped；随后新增的三项回归包含在最终专项回归中 |
| 冻结程序 | 联网和离线包均复制到源码外中文及空格路径；主题、型号、设置页及真实 CSV 子进程通过；损坏输入保留且明确报错 |
| 首次准备 | 可跳过；空库存不预选；只准备选择项；下载不启动安装；安装须点击；活动扫描/会话/采集/批量连接阻止安装；检测失败不标成成功 |
| 完整性与安全解压 | 覆盖大小/哈希/签名失败、404、并发准备、取消与重试、损坏缓存、路径穿越和未选择的危险归档成员；失败不替换原有效文件 |
| 源码打包 | 显式 prune 防止旧 setuptools 清单重新带入评审截图；归档检查无 EXE、DLL、wheel、ZIP 载荷 |
| 组件登记与文档 | 登记检查通过；设备文档、下载链接生成一致性及文档链接/锚点/导航检查通过 |

本机证据位于忽略目录 `.tools/repository-slimming`：`before.json`、`size-report.json`、`vendor-network-report.json`、`pointer-setup-report.json`、`explicit-build-setup-report.json`、`online-final-smoke.json`、`offline-final-smoke.json`、`full-tests.log` 及干净安装日志。界面离屏截图为 `first-preparation.png`，不代表人工实机验收。

## 仍待完成

严格联网发布检查按预期拒绝：来源/版本缺项、分发依据、安装验证和目标环境证据仍保留 pending；离线模式额外要求所选实际载荷完整。固定链接可能被厂商更新，哈希不匹配时必须由维护者核实，不能静默换版本。四种未明确硬件 ID 的接口驱动继续待登记。

修改的 Python 文件 Ruff 检查通过。仓库整体静态检查还有未修改文件的既有问题：`demo/instrument.py` 重复 rbw 键、`ceyear_6362d/tests/test_optical_settings.py` 未使用导入；Mypy 在 `signal_source.py`、`optical_spectrum.py`、`coremorrow_xmt/hardware/validate_control.py` 有四处既有错误。本次未顺带修改这些型号/界面逻辑。

干净 Windows 10/11、普通权限、拒绝提权、重复安装/恢复、重启、各接口驱动与逐设备物理验收仍须单独完成。当前首次准备与厂商向导不等于应用正式安装器；自动网络修复继续待实现。
