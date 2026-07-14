[CmdletBinding()]
param(
    [string]$ListenAddress = "0.0.0.0",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
$EnvPath = Join-Path $RootDir ".env"

if (Test-Path -LiteralPath $EnvPath) {
    foreach ($line in [IO.File]::ReadAllLines($EnvPath, [Text.Encoding]::UTF8)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) { continue }
        $name, $value = $trimmed.Split("=", 2)
        [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim().Trim('"'), "Process")
    }
}

$placeholders = @("", "change-this-shared-token", "paste-generated-token-here", "changeme", "change-me")
if ($ListenAddress -notin @("127.0.0.1", "localhost", "::1") -and $env:VIBE_STICK_BRIDGE_TOKEN -in $placeholders) {
    throw "VIBE_STICK_BRIDGE_TOKEN is required when the bridge listens outside loopback. Run .\scripts\setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $RootDir "bridge\src"
Push-Location (Join-Path $RootDir "bridge")
try {
    & python -m vibe_stick --host $ListenAddress --port $Port
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
