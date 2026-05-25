$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$PythonBin = Join-Path $RootDir ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonBin)) {
    throw "가상환경을 먼저 준비하세요: scripts\setup.ps1"
}

Set-Location $RootDir
& $PythonBin -m src.main
