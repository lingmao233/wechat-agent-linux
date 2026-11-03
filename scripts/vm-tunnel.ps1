# Windows host helper: keep an SSH tunnel to the VM's console alive.
#
# The console on the VM binds 127.0.0.1:8765 on purpose, so the host reaches it
# through this tunnel instead of exposing it on the VM's network. The DSH
# plugin's `remote_url` then points at http://127.0.0.1:8765.
#
# Start it detached (survives the terminal that launches it):
#
#   Start-Process pwsh -WindowStyle Hidden -ArgumentList `
#     '-NoProfile','-ExecutionPolicy','Bypass','-File',"$PWD\scripts\vm-tunnel.ps1"
#
# Stop it:
#
#   Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
#     Where-Object { $_.CommandLine -like '*8765:127.0.0.1:8765*' } |
#     ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
#   Get-CimInstance Win32_Process -Filter "Name='pwsh.exe'" |
#     Where-Object { $_.CommandLine -like '*vm-tunnel.ps1*' } |
#     ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
#
# Reconnect: ssh is re-run every few seconds, so a VM reboot or a dropped Wi-Fi
# link heals by itself. ExitOnForwardFailure makes a busy local port surface as
# an exit instead of a silently dead tunnel.

param(
    [string] $SshTarget = 'wx@192.168.1.50',
    [string] $KeyPath = (Join-Path $HOME '.ssh\id_ed25519'),
    [int] $LocalPort = 8765,
    [int] $RemotePort = 8765,
    [int] $RetrySeconds = 5
)

$ErrorActionPreference = 'Continue'

# One instance per user is enough; a second copy would only fight over the port.
$mutex = New-Object System.Threading.Mutex($false, 'Local\WechatAgentVmTunnel')
if (-not $mutex.WaitOne(0)) {
    Write-Host 'vm-tunnel: another instance already owns the tunnel; exiting.'
    exit 0
}

while ($true) {
    Write-Host "vm-tunnel: connecting $LocalPort -> $SshTarget`:$RemotePort"
    ssh -i $KeyPath -o BatchMode=yes -o ExitOnForwardFailure=yes `
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
        -N -L "${LocalPort}:127.0.0.1:${RemotePort}" $SshTarget
    Write-Host "vm-tunnel: ssh exited ($LASTEXITCODE); retrying in ${RetrySeconds}s"
    Start-Sleep -Seconds $RetrySeconds
}
