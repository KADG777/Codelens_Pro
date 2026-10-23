param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath 'requirements-dev.txt' -PathType Leaf)) {
    throw '项目文件不完整，请完整解压源码包后再运行。'
}
if ($CheckOnly) {
    Write-Host 'Setup check OK'
    exit 0
}

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '无法创建 Python 环境，请先安装 Python 3.12。' }
}

& '.\.venv\Scripts\python.exe' -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) { throw '锁定依赖需要 Python 3.12 或更高版本，请使用 Python 3.12 重新创建环境。' }

& '.\.venv\Scripts\python.exe' -m pip install -r requirements-dev.txt -c requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装未完成，请检查网络后重新运行。' }
Write-Host '安装完成。双击 start.cmd 启动 CodeLens Flow。'
