$ErrorActionPreference = "Stop"

function Import-DotEnv {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    foreach ($Line in Get-Content $Path) {
        $Trimmed = $Line.Trim()
        if (-not $Trimmed -or $Trimmed.StartsWith("#") -or -not $Trimmed.Contains("=")) {
            continue
        }

        $Name, $Value = $Trimmed.Split("=", 2)
        $Name = $Name.Trim()
        $Value = $Value.Trim().Trim('"').Trim("'")
        if ($Name) {
            [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
        }
    }
}

$RootDir = Split-Path -Parent $PSScriptRoot
Import-DotEnv (Join-Path $RootDir ".env")

$ClaudeBin = $env:CLAUDE_BIN
if ([string]::IsNullOrWhiteSpace($ClaudeBin)) {
    $ClaudeCommand = Get-Command claude -ErrorAction SilentlyContinue
    if (-not $ClaudeCommand) {
        throw "claude CLI를 찾을 수 없습니다. PATH를 확인하거나 .env에 CLAUDE_BIN을 설정하세요."
    }
    $ClaudeBin = $ClaudeCommand.Source
}

& $ClaudeBin --version
& $ClaudeBin -p "ping" --setting-sources local --output-format json
