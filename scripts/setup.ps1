[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
$EnvPath = Join-Path $RootDir ".env"
$EnvExamplePath = Join-Path $RootDir ".env.example"
$SecretsPath = Join-Path $RootDir "firmware\sticks3\include\vibe_stick_secrets.h"
$SecretsExamplePath = Join-Path $RootDir "firmware\sticks3\include\vibe_stick_secrets.example.h"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Read-Text([string]$Path) {
    return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
}

function Write-Text([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, $script:Utf8NoBom)
}

function Get-EnvValue([string]$Name) {
    $match = [regex]::Match((Read-Text $script:EnvPath), "(?m)^$([regex]::Escape($Name))=(.*)$")
    if ($match.Success) { return $match.Groups[1].Value.Trim().Trim('"') }
    return ""
}

function Get-SecretValue([string]$Name) {
    $pattern = '(?m)^#define\s+' + [regex]::Escape($Name) + '\s+"([^"]*)"'
    $match = [regex]::Match((Read-Text $script:SecretsPath), $pattern)
    if ($match.Success) { return $match.Groups[1].Value }
    return ""
}

function Set-EnvValue([string]$Name, [string]$Value) {
    $text = Read-Text $script:EnvPath
    $pattern = "(?m)^$([regex]::Escape($Name))=.*$"
    $replacement = "$Name=$Value"
    if ([regex]::IsMatch($text, $pattern)) {
        $text = [regex]::Replace($text, $pattern, { param($match) $replacement }, 1)
    } else {
        $text = $text.TrimEnd() + [Environment]::NewLine + $replacement + [Environment]::NewLine
    }
    Write-Text $script:EnvPath $text
}

function Set-SecretValue([string]$Name, [string]$Value) {
    $text = Read-Text $script:SecretsPath
    $pattern = '(?m)^#define\s+' + [regex]::Escape($Name) + '\s+"[^"]*"'
    $replacement = "#define $Name `"$Value`""
    $text = [regex]::Replace($text, $pattern, { param($match) $replacement }, 1)
    Write-Text $script:SecretsPath $text
}

function Test-Placeholder([string]$Value) {
    return [string]::IsNullOrWhiteSpace($Value) -or $Value -in @(
        "change-this-shared-token", "paste-generated-token-here", "changeme", "change-me", "your-token"
    )
}

if (-not (Test-Path -LiteralPath $EnvPath)) {
    Copy-Item -LiteralPath $EnvExamplePath -Destination $EnvPath
    Write-Host "Created .env from .env.example."
} else {
    Write-Host "Kept existing .env."
}

if (-not (Test-Path -LiteralPath $SecretsPath)) {
    Copy-Item -LiteralPath $SecretsExamplePath -Destination $SecretsPath
    Write-Host "Created firmware secrets from the example."
} else {
    Write-Host "Kept existing firmware secrets."
}

$envToken = Get-EnvValue "VIBE_STICK_BRIDGE_TOKEN"
$secretToken = Get-SecretValue "VIBE_STICK_BRIDGE_TOKEN"
if (-not (Test-Placeholder $envToken)) {
    if (Test-Placeholder $secretToken) {
        Set-SecretValue "VIBE_STICK_BRIDGE_TOKEN" $envToken
        Write-Host "Copied the .env bridge token into firmware secrets."
    } elseif ($envToken -ne $secretToken) {
        Write-Warning "The .env and firmware bridge tokens differ; existing values were preserved."
    }
} elseif (-not (Test-Placeholder $secretToken)) {
    Set-EnvValue "VIBE_STICK_BRIDGE_TOKEN" $secretToken
    Write-Host "Copied the firmware bridge token into .env."
} else {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    Set-EnvValue "VIBE_STICK_BRIDGE_TOKEN" $token
    Set-SecretValue "VIBE_STICK_BRIDGE_TOKEN" $token
    Write-Host "Generated one shared bridge token."
}

$lanAddress = Get-NetIPConfiguration -ErrorAction SilentlyContinue |
    Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address.IPAddress -notlike "169.254.*" } |
    ForEach-Object { $_.IPv4Address.IPAddress } |
    Select-Object -First 1
$bridgeHost = Get-SecretValue "VIBE_STICK_BRIDGE_HOST"
if ($lanAddress -and ($bridgeHost -in @("", "127.0.0.1", "0.0.0.0", "192.168.1.10", "192.168.0.10", "10.0.0.10", "YOUR_MAC_IP", "your-mac-ip"))) {
    Set-SecretValue "VIBE_STICK_BRIDGE_HOST" $lanAddress
    Write-Host "Set firmware bridge host to detected Windows LAN IP: $lanAddress"
} elseif ($lanAddress) {
    Write-Host "Kept firmware bridge host $bridgeHost (detected LAN IP: $lanAddress)."
} else {
    Write-Warning "Could not detect a LAN IPv4 address; set VIBE_STICK_BRIDGE_HOST manually."
}

Write-Host ""
Write-Host "Next: edit Wi-Fi values in firmware\sticks3\include\vibe_stick_secrets.h and ASR values in .env."
Write-Host "Run .\scripts\dev.ps1 to start the Windows bridge."
