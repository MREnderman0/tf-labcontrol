param(
    [ValidateSet('Tuna', 'Aliyun')][string]$Mirror = 'Tuna',
    [string]$Python = '',
    [switch]$UseProxy,
    [switch]$Build,
    [switch]$Offline,
    [string]$Cache = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$index = if ($Mirror -eq 'Aliyun') { 'https://mirrors.aliyun.com/pypi/simple/' } else { 'https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/' }
$inventory = Get-Content -LiteralPath (Join-Path $projectRoot 'deployment/packages/manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$userCache = if ($Cache) { [IO.Path]::GetFullPath($Cache) } else { Join-Path $env:LOCALAPPDATA 'TF-LabControl\maintenance\packages' }
$cacheRoots = @($userCache, (Join-Path $projectRoot 'deployment/packages'))
function Test-Material($Path, $Entry) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ((Get-Item -LiteralPath $Path).Length -ne $Entry.size) { return $false }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -eq $Entry.sha256
}
function Find-Material($Entry) {
    $relative = $Entry.path -replace '^deployment/packages/', ''
    foreach ($folder in $cacheRoots) {
        $candidate = Join-Path $folder $relative
        if (Test-Material $candidate $Entry) { return $candidate }
    }
    return ''
}
$names = @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','UV_DEFAULT_INDEX','UV_INDEX','UV_EXTRA_INDEX_URL','UV_INDEX_URL','PIP_INDEX_URL','PIP_EXTRA_INDEX_URL','PIP_CONFIG_FILE','PIP_DISABLE_PIP_VERSION_CHECK','UV_PROJECT_ENVIRONMENT','UV_HTTP_TIMEOUT','UV_LINK_MODE','UV_PYTHON_DOWNLOADS','UV_FIND_LINKS','PSModulePath')
$saved = @{}
$names += @('UV_CACHE_DIR', 'PIP_CACHE_DIR')
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
Push-Location $projectRoot
try {
    if ($PSVersionTable.PSEdition -ne 'Core') {
        $env:PSModulePath = (Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\Modules') + ';' + (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules')
    }
    if (-not $UseProxy) {
        foreach ($name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY')) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        $env:NO_PROXY = '*'
    }
    foreach ($name in @('UV_INDEX','UV_EXTRA_INDEX_URL','UV_INDEX_URL','PIP_EXTRA_INDEX_URL','UV_FIND_LINKS')) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    $env:UV_DEFAULT_INDEX = $index
    $env:PIP_INDEX_URL = $index
    $env:PIP_CONFIG_FILE = 'NUL'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:UV_PROJECT_ENVIRONMENT = '.venv-dev'
    $env:UV_HTTP_TIMEOUT = '300'
    $env:UV_LINK_MODE = 'copy'
    $env:UV_PYTHON_DOWNLOADS = 'never'
    $env:UV_CACHE_DIR = Join-Path $userCache 'python-uv'
    $env:PIP_CACHE_DIR = Join-Path $userCache 'python-pip'
    $envPython = Join-Path $projectRoot '.venv-dev\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $envPython)) {
        $interpreter = $Python
        if (-not $interpreter) {
            $launcher = Get-Command py -ErrorAction SilentlyContinue
            if ($launcher) {
                $interpreter = & $launcher.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -ne 0) { $interpreter = '' }
            }
        }
        if (-not $interpreter) {
            $entry = $inventory.files | Where-Object { $_.path -match '/python-3\.12\.10-amd64\.exe$' } | Select-Object -First 1
            $installer = Find-Material $entry
            if (-not $installer) {
                if ($Offline) { throw 'Python 3.12 x64 is missing and offline installer is unavailable.' }
                $installer = Join-Path $userCache 'installers\python-3.12.10-amd64.exe'
                New-Item -ItemType Directory -Force -Path (Split-Path $installer) | Out-Null
                $partial = $installer + '.' + [guid]::NewGuid().ToString('N') + '.partial'
                try {
                    $curlArgs = @('--fail','--location','--proto','=https','--proto-redir','=https','--connect-timeout','15','--max-time','600','--output',$partial)
                    if (-not $UseProxy) { $curlArgs += @('--noproxy','*') }
                    & curl.exe @curlArgs $entry.source
                    if ($LASTEXITCODE -ne 0 -or -not (Test-Material $partial $entry)) { throw 'Pinned Python download/hash verification failed.' }
                    $signature = Get-AuthenticodeSignature -LiteralPath $partial
                    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') { throw 'Python installer signature verification failed.' }
                    Move-Item -LiteralPath $partial -Destination $installer -Force
                } finally {
                    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial }
                }
            }
            $signature = Get-AuthenticodeSignature -LiteralPath $installer
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') { throw 'Python installer signature verification failed.' }
            Write-Host "Verified Python installer: $installer"
            Write-Host "Source: $($entry.source_page)"
            Write-Host 'Open this installer to install Python 3.12 x64, then rerun run.bat (or pass -Python). No installer was launched automatically.'
            exit 2
        }
        & $interpreter -c 'import sys; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32'
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 required.' }
        & $interpreter -m venv .venv-dev
        if ($LASTEXITCODE -ne 0) { throw 'Could not create project environment.' }
    }
    & $envPython -c 'import sys; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32'
    if ($LASTEXITCODE -ne 0) { throw 'Invalid project Python.' }
    $offlinePython = ''
    $wheelEntries = @($inventory.files | Where-Object { $_.kind -eq 'python-wheel' })
    foreach ($folder in $cacheRoots) {
        $complete = $wheelEntries.Count -gt 0
        foreach ($entry in $wheelEntries) {
            if (-not (Test-Material (Join-Path $folder ($entry.path -replace '^deployment/packages/', '')) $entry)) { $complete = $false; break }
        }
        if ($complete) { $offlinePython = Join-Path $folder 'python'; break }
    }
    if ($Offline -and -not $offlinePython) { throw 'Offline wheelhouse is incomplete, corrupt, or contains LFS pointers.' }
    $uv = Join-Path $projectRoot '.venv-dev\Scripts\uv.exe'
    if (-not (Test-Path -LiteralPath $uv)) {
        $uvEntry = $inventory.files | Where-Object { $_.path -match '/tools/uv-.*\.exe$' } | Select-Object -First 1
        $bundledUv = Find-Material $uvEntry
        if ($bundledUv) {
            Copy-Item -LiteralPath $bundledUv -Destination $uv
        } else {
            & $envPython -m ensurepip
            if ($LASTEXITCODE -ne 0) { throw 'Could not bootstrap pip.' }
            $pipArgs = @('-m','pip','--isolated','install','uv==0.12.18')
            if ($offlinePython) { $pipArgs += @('--no-index','--find-links',$offlinePython) } else { $pipArgs += @('--index-url',$index) }
            & $envPython @pipArgs
            if ($LASTEXITCODE -ne 0) { throw 'uv preparation failed; no foreign fallback.' }
        }
    }
    $uvVersion = & $uv --version
    if ($LASTEXITCODE -ne 0 -or $uvVersion -notmatch '^uv 0\.12\.18(?:\s|$)') { throw 'Project uv must be version 0.12.18; cache may contain an LFS pointer.' }
    # Windows cannot replace the executable currently performing the sync.
    $bootstrapUv = Join-Path $projectRoot '.tools/setup/uv-0.12.18.exe'
    New-Item -ItemType Directory -Force -Path (Split-Path $bootstrapUv) | Out-Null
    Copy-Item -LiteralPath $uv -Destination $bootstrapUv -Force
    $uv = $bootstrapUv
    # Setuptools is a locked build-group dependency used to install the local project.
    $setuptools = ('import tomllib; p=tomllib.load(open("uv.lock","rb")); print(next(x["version"] for x in p["package"] if x["name"]=="setuptools"))' | & $envPython -)
    if ($LASTEXITCODE -ne 0) { throw 'Locked setuptools version missing.' }
    $sourceArgs = if ($offlinePython) { @('--offline','--no-index','--find-links',$offlinePython) } else { @('--default-index',$index) }
    $exportPath = Join-Path $projectRoot '.tools/setup/requirements.txt'
    New-Item -ItemType Directory -Force -Path (Split-Path $exportPath) | Out-Null
    # Export with the lock's original index; changing mirrors must not relock.
    $env:UV_DEFAULT_INDEX = $inventory.policy.python_index
    $exportArgs = @('export','--locked','--offline','--no-emit-project','--no-annotate','--format','requirements.txt','--output-file',$exportPath)
    if ($Build) { $exportArgs += @('--group','build') }
    & $uv @exportArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Lock file is stale; update it explicitly before setup.' }
    $env:UV_DEFAULT_INDEX = $index
    Write-Host "Project environment: $envPython; domestic mirror: $index; verified cache: $offlinePython"
    & $uv pip sync $exportPath --require-hashes --python $envPython @sourceArgs
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed; no foreign fallback.' }
    & $uv pip install "setuptools==$setuptools" --python $envPython @sourceArgs
    if ($LASTEXITCODE -ne 0) { throw 'Build backend preparation failed; no foreign fallback.' }
    & $uv pip install --no-deps --no-build-isolation --python $envPython @sourceArgs -e .
    if ($LASTEXITCODE -ne 0) { throw 'Local project installation failed.' }
    $legacyLauncher = Join-Path $projectRoot '.venv-dev\Scripts\lab-control-demo.exe'
    if (Test-Path -LiteralPath $legacyLauncher) { Remove-Item -LiteralPath $legacyLauncher }
    & $uv pip check --python $envPython
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    Pop-Location
}
