param(
    [string]$TaskName = "discord-claude-assistant"
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$PythonBin = Join-Path $RootDir ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonBin)) {
    throw "가상환경을 먼저 준비하세요: scripts\setup.ps1"
}

$LogDir = Join-Path $env:LOCALAPPDATA "discord-claude-assistant\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogPath = Join-Path $LogDir "bot.log"

function ConvertTo-PowerShellSingleQuotedLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

$RootLiteral = ConvertTo-PowerShellSingleQuotedLiteral $RootDir
$PythonLiteral = ConvertTo-PowerShellSingleQuotedLiteral $PythonBin
$LogLiteral = ConvertTo-PowerShellSingleQuotedLiteral $LogPath
$Command = "& { Set-Location -LiteralPath $RootLiteral; & $PythonLiteral -m src.main *> $LogLiteral }"

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command $Command"
# Scope the trigger to the current user: an unscoped -AtLogOn fires for every
# user, and registering that needs an elevated shell (Access is denied).
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# -ExecutionTimeLimit 0 disables the default 72h limit, which would otherwise
# kill the long-running bot after three days.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# ScheduledTasks cmdlets ignore $ErrorActionPreference, so a failed
# registration would fall through to the success message without -ErrorAction.
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Run discord-claude-assistant at user logon." `
    -Force `
    -ErrorAction Stop | Out-Null

Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop

Write-Host "registered and started scheduled task: $TaskName"
Write-Host "log: $LogPath"
