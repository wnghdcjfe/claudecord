$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

function Invoke-HostPython {
    param([string[]]$PythonArgs)

    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 @PythonArgs
        return
    }

    & python @PythonArgs
}

Invoke-HostPython -PythonArgs @("-m", "venv", ".venv")

$PythonBin = Join-Path $RootDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonBin)) {
    throw "가상환경 Python을 찾을 수 없습니다: $PythonBin"
}

& $PythonBin -m pip install -e .
