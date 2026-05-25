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
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Run discord-claude-assistant at user logon." `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "registered and started scheduled task: $TaskName"
Write-Host "log: $LogPath"
