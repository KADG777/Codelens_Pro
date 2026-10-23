param(
    [switch]$CheckOnly,
    [ValidateRange(1024, 65535)]
    [int]$Port = 8503,
    [switch]$Headless
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONIOENCODING = 'utf-8'

try {
    Set-Location -LiteralPath $PSScriptRoot
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'app.py') -PathType Leaf)) {
        throw '项目文件不完整：请先完整解压源码包，再运行 start.cmd。'
    }
    $flowPythonCandidates = @(
        (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
        (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe')
    )
    $flowPython = $null
    foreach ($flowCandidate in $flowPythonCandidates) {
        if (Test-Path -LiteralPath $flowCandidate -PathType Leaf) {
            $flowPython = $flowCandidate
            break
        }
    }
    if (-not $flowPython) {
        throw '尚未安装运行环境。请先双击 install.cmd，安装完成后再启动。'
    }
    & $flowPython -c "import streamlit, langgraph; from langgraph.checkpoint.sqlite import SqliteSaver; from workflow import WorkflowRunService"
    if ($LASTEXITCODE -ne 0) {
        throw '运行依赖不完整。请重新运行 install.cmd 完成安装。'
    }
    if ($CheckOnly) {
        Write-Host 'Launch check OK'
        Write-Host ('Python: ' + $flowPython)
        exit 0
    }
    Write-Host ('正在启动 CodeLens Flow：http://127.0.0.1:' + $Port)
    Write-Host '关闭此窗口可停止本地服务。若端口已被占用，请先打开已有页面，或用 -Port 8504 启动。'
    $flowArguments = @(
        '-m', 'streamlit', 'run', 'app.py',
        '--server.address', '127.0.0.1', '--server.port', $Port.ToString(),
        '--browser.gatherUsageStats', 'false'
    )
    if ($Headless) {
        $flowArguments += @('--server.headless', 'true')
    }
    & $flowPython @flowArguments
    exit $LASTEXITCODE
}
catch {
    Write-Host ('启动未完成：' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
