# 本机材料缓存与恢复

Git 仅保留清单、说明、锁定依赖导出和许可；第三方安装包、wheels、归档及解压产物不再随 clone 传输，也不再需要 Git LFS。已有载荷保留在本目录并由忽略规则保护。此次不重写 Git 历史或删除远端 LFS 对象，因此已有 `.git` 不会立即缩小。

[manifest.json](manifest.json)保留迁移前每个载荷的路径、大小、SHA256、来源、签名状态；[组件清单](../components.json)登记用途、版本、依赖闭包、解压映射和安装器入口。厂家许可 PDF 继续留在 Git，分发依据尚未确认的项仍为 pending。

## 恢复运行材料

新材料默认放入 `%LOCALAPPDATA%\TF-LabControl\maintenance\packages`；仍读取本目录、旧 `.tools/installers`、发行目录和 ProgramData 维护缓存。文件须校验后复用；不完整下载或 LFS 指针不视为可用材料。

在首次运行/环境与修复中选择所需组件，或使用公共 CLI：

```powershell
.venv-dev\Scripts\python.exe -m lab_control --cli components list
.venv-dev\Scripts\python.exe -m lab_control --cli components prepare --component rs-visa
.venv-dev\Scripts\python.exe -m lab_control --cli components prepare --component kplusk-python-wrapper
.venv-dev\Scripts\python.exe -m lab_control --cli components verify --component rs-visa
```

K+K wrapper 自动包含库依赖，从已登记官方归档恢复 `tools/kplusk/runtime`。Thorlabs ZIP 校验后只解压指定成员。所有组件准备均不运行安装器或设备工具；GUI 的“打开安装向导”由用户明确点击。详细来源见[下载资源](../../docs/generated/download-resources.md)。

旧 Tftpd64 便携工具及其 INI/CHM 的原始归档、哈希和官方来源仍在 manifest 中；它们不是普通运行依赖，不自动部署或修改网络。原始 INI 不含现场配置，现场网络资料只保存在运行目录。

## 恢复开发材料

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1
# 单独导出开发 wheelhouse 与 uv，不放进最终运行包
.venv-dev\Scripts\python.exe scripts/prepare_dev_cache.py --output D:\LabDevCache
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1 -Offline -Build -Cache D:\LabDevCache
```

默认清华镜像，阿里云通过 `-Mirror Aliyun` 显式选择。开发准备按 `uv.lock` 安装运行与开发依赖，`-Build` 追加构建组；完整缓存可离线安装，安装不重写锁文件。Python 3.12 x64 缺失时准备清单中的锁定安装器并提供打开路径，安装后重新运行。开发导出不包含 Python 安装器；完全离线的新电脑须另外按 manifest 准备该安装器，或已安装 Python 3.12 x64。

[部署规范](../../docs/development/desktop-deployment.md)说明联网/离线构建及严格发布检查；材料完整不能代替安装验证、许可确认或真实设备验收。迁移路径及原始哈希另见[迁移清单](../payload-migration.json)。
