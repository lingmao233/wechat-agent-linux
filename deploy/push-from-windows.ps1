# wechat-agent-linux · 从 Windows 一键推送到 Xubuntu 虚拟机
#
# 做三件事：打包当前源码（不含 .env/state/.venv）→ scp 到 VM → ssh 里跑 bootstrap。
#
#   .\deploy\push-from-windows.ps1 -VmHost 192.168.1.50 -User wx
#   .\deploy\push-from-windows.ps1 -VmHost 192.168.1.50 -User wx -Phase key
#   .\deploy\push-from-windows.ps1 -VmHost 192.168.1.50 -User wx -Tunnel   # 建 8765 隧道看控制台
#
# 需要：Windows 自带 OpenSSH 客户端（ssh/scp）、VM 已装 openssh-server 且能 ping 通。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$VmHost,
    [string]$User = 'wx',
    [int]$Port = 22,
    [string]$SshKey,
    [string]$WechatDeb,
    [string]$EnvFile,
    [string]$Phase = 'base',
    [string]$PipIndex,
    [switch]$SkipUpload,
    [switch]$Tunnel
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$OutDir = Join-Path $PSScriptRoot 'out'
$Tarball = Join-Path $OutDir 'wechat-agent-linux-src.tar.gz'

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "    $Text" -ForegroundColor DarkGray }
function Fail([string]$Text) { Write-Host "`n[错误] $Text" -ForegroundColor Red; exit 1 }

foreach ($tool in 'ssh', 'scp', 'tar') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "找不到 $tool；请先启用 Windows 可选功能 OpenSSH Client" }
}

if (-not $EnvFile) { $EnvFile = Join-Path $RepoRoot '.env' }
if (-not (Test-Path $RepoRoot 'scripts\deploy-xubuntu.sh')) { Fail "当前目录不像仓库根目录：$RepoRoot" }

$sshArgs = @('-p', $Port, '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10')
$scpArgs = @('-P', $Port, '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10')
if ($SshKey) { $sshArgs += @('-i', $SshKey); $scpArgs += @('-i', $SshKey) }
$Remote = "$User@$VmHost"

# ------------------------------------------------------------------ 打包
if (-not $SkipUpload) {
    Write-Step "打包源码（排除 .env / .venv / state / 缓存）"
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    if (Test-Path $Tarball) { Remove-Item $Tarball -Force }

    Push-Location $RepoRoot
    try {
        tar --exclude=.git --exclude=.venv --exclude=.pytools --exclude=.playwright-mcp `
            --exclude=.pytest_cache --exclude=state --exclude=artifacts --exclude=dist `
            --exclude=logs --exclude=__pycache__ --exclude='*.pyc' --exclude=.env `
            --exclude='.env.bak.*' --exclude=deploy/out `
            -czf $Tarball .
        if ($LASTEXITCODE -ne 0) { Fail "tar 打包失败" }
    }
    finally { Pop-Location }

    $sizeMb = [math]::Round((Get-Item $Tarball).Length / 1MB, 2)
    Write-Note "包: $Tarball ($sizeMb MB)"

    # -------------------------------------------------------------- 上传
    Write-Step "上传到 $Remote"
    & scp @scpArgs $Tarball "${Remote}:~/wechat-agent-linux-src.tar.gz"
    if ($LASTEXITCODE -ne 0) { Fail "scp 失败：确认 VM 已开 ssh、IP/端口/用户名正确、防火墙放行" }
    Write-Note "源码包已上传"

    if (Test-Path $EnvFile) {
        & scp @scpArgs $EnvFile "${Remote}:~/.env.from-windows"
        if ($LASTEXITCODE -ne 0) { Fail "配置上传失败" }
        Write-Note "配置已上传为 ~/.env.from-windows（导入后 VM 内权限会被收紧为 600）"
    }
    else {
        Write-Note "[提示] 本地没有 $EnvFile；VM 里将用 .env.linux.example 模板，需要手工填密钥"
    }

    if ($WechatDeb) {
        if (-not (Test-Path $WechatDeb)) { Fail "找不到微信 deb: $WechatDeb" }
        Write-Step "上传微信 deb（较大，耐心等）"
        & scp @scpArgs $WechatDeb "${Remote}:~/WeChatLinux.deb"
        if ($LASTEXITCODE -ne 0) { Fail "deb 上传失败" }
        Write-Note "已上传为 ~/WeChatLinux.deb"
    }
}

# ------------------------------------------------------------------ 远端执行
$BootstrapArgs = "--phase $Phase"
if (-not $SkipUpload) {
    if (Test-Path $EnvFile) { $BootstrapArgs += ' --env-from ~/.env.from-windows' }
    if ($WechatDeb) { $BootstrapArgs += ' --wechat-deb ~/WeChatLinux.deb' }
}
if ($PipIndex) { $BootstrapArgs += " --pip-index $PipIndex" }

$RemoteCmd = "mkdir -p ~/wechat-agent-linux && tar -xzf ~/wechat-agent-linux-src.tar.gz -C ~/wechat-agent-linux && cd ~/wechat-agent-linux && bash deploy/bootstrap-xubuntu.sh $BootstrapArgs"

Write-Step "在 VM 内执行 bootstrap（$Phase）"
Write-Note $RemoteCmd
& ssh @sshArgs -t $Remote $RemoteCmd
$rc = $LASTEXITCODE

if ($Tunnel) {
    Write-Step "建立控制台隧道 127.0.0.1:8765"
    Write-Note "浏览器打开 http://127.0.0.1:8765 ，关掉这个窗口即断开"
    & ssh @sshArgs -N -L 8765:127.0.0.1:8765 $Remote
}

Write-Step "完成"
if ($rc -ne 0) { Write-Host "    bootstrap 退出码 $rc，看上面的 [错误] / [FAIL] 行" -ForegroundColor Yellow }
Write-Note "扫码登录微信等图形步骤必须在 VM 桌面终端里做，SSH 里做不了"
Write-Note "VM 内后续命令："
Write-Note "  cd ~/wechat-agent-linux && export WECHAT_PYTHON=`$HOME/.venvs/wechat-agent-linux/bin/python"
Write-Note "  bash deploy/bootstrap-xubuntu.sh --phase key"
Write-Note "  bash deploy/bootstrap-xubuntu.sh --phase check"
exit $rc
