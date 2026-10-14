param(
    [ValidateSet('Tuna', 'Aliyun')][string]$Mirror = 'Tuna',
    [string]$Python = '',
    [switch]$UseProxy
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$index = if ($Mirror -eq 'Aliyun') { 'https://mirrors.aliyun.com/pypi/simple/' } else { 'https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/' }
$offlineRoot = Join-Path $projectRoot 'deployment\packages'
$offlinePython = Join-Path $offlineRoot 'python'
$bundledUv = Join-Path $offlineRoot 'tools\uv-0.12.18-win_amd64.exe'
$offlineRequirements = Join-Path $offlinePython 'requirements.txt'
$offlineReady = (Test-Path -LiteralPath $offlineRequirements) -and ((Get-ChildItem -LiteralPath $offlinePython -Filter '*.whl' -File -ErrorAction SilentlyContinue).Count -gt 0)
$names = @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','UV_DEFAULT_INDEX','UV_INDEX','UV_EXTRA_INDEX_URL','UV_INDEX_URL','PIP_INDEX_URL','PIP_EXTRA_INDEX_URL','PIP_CONFIG_FILE','PIP_DISABLE_PIP_VERSION_CHECK','UV_PROJECT_ENVIRONMENT','UV_HTTP_TIMEOUT','UV_LINK_MODE','UV_PYTHON_DOWNLOADS')
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
Push-Location $projectRoot
try {
    if (-not $UseProxy) {
        foreach ($name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY')) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        $env:NO_PROXY = '*'
    }
    foreach ($name in @('UV_INDEX','UV_EXTRA_INDEX_URL','UV_INDEX_URL','PIP_EXTRA_INDEX_URL')) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    $env:UV_DEFAULT_INDEX = $index
    $env:PIP_INDEX_URL = $index
    $env:PIP_CONFIG_FILE = 'NUL'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:UV_PROJECT_ENVIRONMENT = '.venv-dev'
    $env:UV_HTTP_TIMEOUT = '300'
    $env:UV_LINK_MODE = 'copy'
    $env:UV_PYTHON_DOWNLOADS = 'never'
    $envPython = Join-Path $projectRoot '.venv-dev\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $envPython)) {
        if ($Python) { & $Python -m venv .venv-dev } else { & py -3.12 -m venv .venv-dev }
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 required. Pass -Python with its path.' }
    }
    'import sys; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32' | & $envPython -
    if ($LASTEXITCODE -ne 0) { throw 'Invalid project Python.' }
    $uv = Join-Path $projectRoot '.venv-dev\Scripts\uv.exe'
    if (-not (Test-Path -LiteralPath $uv)) {
        if ($offlineReady -and (Test-Path -LiteralPath $bundledUv)) {
            Copy-Item -LiteralPath $bundledUv -Destination $uv -Force
        } else {
            & $envPython -m ensurepip
            if ($LASTEXITCODE -ne 0) { throw 'Could not bootstrap project pip.' }
            & $envPython -m pip --isolated install --index-url $index 'uv==0.12.18'
            if ($LASTEXITCODE -ne 0) { throw "uv download failed at $index. No foreign fallback." }
        }
    }
    if ($offlineReady) {
        Write-Host "Project environment: $envPython; source: repository offline cache; proxy enabled: $UseProxy"
        & $uv pip sync $offlineRequirements --offline --no-index --find-links $offlinePython --python $envPython
        if ($LASTEXITCODE -ne 0) { throw 'Offline dependency installation failed; cache is incomplete or invalid.' }
        & $uv pip install --offline --no-index --find-links $offlinePython --no-deps --no-build-isolation --python $envPython -e .
        if ($LASTEXITCODE -ne 0) { throw 'Offline project installation failed; cache or local build metadata is invalid.' }
    } else {
        Write-Host "Project environment: $envPython; mirror: $index; proxy enabled: $UseProxy"
        & $uv lock --default-index $index
        if ($LASTEXITCODE -ne 0) { throw 'Lock failed; no foreign fallback.' }
        & $uv sync --locked --default-index $index --python $envPython
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; no foreign fallback.' }
    }
    $legacyLauncher = Join-Path $projectRoot '.venv-dev\Scripts\lab-control-demo.exe'
    if (Test-Path -LiteralPath $legacyLauncher) {
        Remove-Item -LiteralPath $legacyLauncher -ErrorAction Stop
    }
    & $uv pip check --python $envPython
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    Pop-Location
}
