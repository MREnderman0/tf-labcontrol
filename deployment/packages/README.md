# 项目离线依赖缓存

这个目录保存已经核验过、可以随仓库分发的 Windows 10/11 x64 安装材料和 Python 依赖。新 clone 后，`scripts/setup-dev.ps1` 会优先使用这里的缓存；只有缓存不完整时才按 `docs/development/environment.md` 使用国内镜像下载。

## 目录

- `installers/`：Python 3.12.10、R&S VISA 7.2.6、PicoSDK 11.1.0.481、Thorlabs OPM 7.0.5320.947、TimeLab 1.780A、Tftpd64 Service 4.74 的官方安装包。它们的来源、大小、SHA256 和签名状态记录在 `manifest.json`，组件用途和安装边界仍以 `deployment/components.json` 为准。
- `python/`：由 `uv.lock` 导出的锁定依赖清单和 Windows x64 / CPython 3.12 wheelhouse。下载时使用清华 PyPI 镜像，禁止代理和国外 PyPI 回退。
- `tools/`：项目环境使用的 `uv 0.12.18` 启动文件，以及 Tftpd64 4.74 原始便携归档、解压文件和所附许可证。这里的 INI 保持厂商原始内容，现场网卡、地址池及租约须另存本机运行目录。
- `manifest.json`：缓存文件清单及校验信息。

## 使用

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1
```

脚本在缓存完整时执行 `uv pip sync deployment/packages/python/requirements.txt --offline --no-index --find-links deployment/packages/python`，再离线安装本项目，并运行依赖一致性检查。构建组也已放入 wheelhouse；需要完整构建依赖时，可以在已创建的环境中执行：

```powershell
$env:UV_PROJECT_ENVIRONMENT = '.venv-dev'
.venv-dev\Scripts\uv.exe pip sync deployment/packages/python/requirements.txt --offline --no-index --find-links deployment/packages/python --python .venv-dev\Scripts\python.exe
.venv-dev\Scripts\uv.exe pip install --offline --no-index --find-links deployment/packages/python --no-deps --no-build-isolation --python .venv-dev\Scripts\python.exe -e .
```

`.tools/installers` 仍是下载脚本的临时工作区。下载脚本验证成功后会把材料复制到本目录；部署检查会优先从本目录查找。未完整下载的 PicoSDK `.partial` 文件、临时网络探测文件和未核验的 Thorlabs 材料不会被纳入缓存。

大文件使用 Git LFS 管理。提交并推送本目录后，其他机器需要安装 Git LFS 并执行 `git lfs pull`，否则只会得到指针文件。该缓存不等同于正式发行验收：安装参数、再分发许可、干净系统验证和真实设备验收仍按部署台账保留待完成状态。
