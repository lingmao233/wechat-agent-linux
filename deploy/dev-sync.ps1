<#
.SYNOPSIS
    把改过的代码/人设同步到 VM，并在 VM 上重启控制台。

.DESCRIPTION
    只同步 src/、personas/、scripts/ 三类内容，永远不会覆盖 VM 上的 .env。
    同步完成后通过 SSH 调用 scripts/restart-desktop.sh，控制台以 nohup 方式
    在后台重启（不占用终端），可选 --StartAgent 直接点一次「启动」。

    一次性前置条件（在 VM 里执行一次）：
        sudo apt install -y openssh-server
        sudo systemctl enable --now ssh

.PARAMETER SshKey
    Windows 上的私钥路径，例如 $env:USERPROFILE\.ssh\id_ed25519。
    不填则使用默认密钥或交互式密码。

.EXAMPLE
    .\deploy\dev-sync.ps1

.EXAMPLE
    .\deploy\dev-sync.ps1 -StartAgent

.EXAMPLE
    .\deploy\dev-sync.ps1 -Paths personas -NoRestart
#>
[CmdletBinding()]
param(
    [string]$VmHost = '192.168.1.50',
    [string]$VmUser = 'wx',
    [int]$Port = 22,
    [string]$SshKey = '',
    [string]$RemoteDir = '~/wechat-agent-linux',
    [string[]]$Paths = @('src/wechat_agent', 'personas', 'scripts'),
    [switch]$NoRestart,
    [switch]$StartAgent,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$watch = [System.Diagnostics.Stopwatch]::StartNew()

function Write-Step([string]$message) {
    Write-Host "==> $message" -ForegroundColor Cyan
}

foreach ($tool in 'ssh', 'scp') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "找不到 $tool。Windows 设置 → 应用 → 可选功能 → 添加「OpenSSH 客户端」后重试。"
    }
}

$sshArgs = @('-p', "$Port")
$scpArgs = @('-P', "$Port")
if ($SshKey) {
    $sshArgs += @('-i', $SshKey)
    $scpArgs += @('-i', $SshKey)
}
$target = "$VmUser@$VmHost"

Write-Host "目标: $target ($RemoteDir)" -ForegroundColor DarkGray

foreach ($relative in $Paths) {
    $local = Join-Path $root $relative
    if (-not (Test-Path -LiteralPath $local)) {
        Write-Warning "跳过不存在的路径: $relative"
        continue
    }
    # 不把 __pycache__ 传过去：既慢，又会让 VM 上的旧字节码和新源码混在一起。
    Get-ChildItem -LiteralPath $local -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    $parent = Split-Path -Parent $relative
    $remoteRoot = if ($parent) { "$RemoteDir/$($parent -replace '\\', '/')/" } else { "$RemoteDir/" }
    $arguments = $scpArgs + @('-r', $local, "${target}:$remoteRoot")

    if ($DryRun) {
        Write-Step "scp -r $relative -> ${target}:$remoteRoot"
        continue
    }
    Write-Step "同步 $relative"
    & scp @arguments
    if ($LASTEXITCODE -ne 0) { throw "scp 失败（$relative），退出码 $LASTEXITCODE" }
}

if ($NoRestart) {
    Write-Host "已跳过重启（-NoRestart）。" -ForegroundColor Yellow
}
else {
    $remoteCommand = "cd $RemoteDir && bash scripts/restart-desktop.sh"
    if ($StartAgent) { $remoteCommand += ' --start-agent' }
    if ($DryRun) {
        Write-Step "ssh $remoteCommand"
    }
    else {
        Write-Step '重启 VM 上的控制台'
        & ssh @sshArgs $target $remoteCommand
        if ($LASTEXITCODE -ne 0) { throw "远程重启失败，退出码 $LASTEXITCODE（看上面的输出）" }
    }
}

$watch.Stop()
Write-Host ("完成，用时 {0:N1} 秒" -f $watch.Elapsed.TotalSeconds) -ForegroundColor Green
