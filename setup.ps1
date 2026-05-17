$ErrorActionPreference = "Stop"

$ExampleEnv = ".env.example"
$TargetEnv = ".env"

Write-Host "Setting up code execution gateway configuration..."
Write-Host ""

function New-ApiSecret {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }

    return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function Write-Utf8NoBomLines {
    param(
        [string]$Path,
        [string[]]$Lines
    )

    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText((Resolve-Path -LiteralPath $Path), (($Lines -join [Environment]::NewLine) + [Environment]::NewLine), $encoding)
}

function Sync-EnvWithExample {
    param(
        [string]$ExampleFile,
        [string]$TargetFile
    )

    $lines = @(Get-Content -LiteralPath $TargetFile)
    $targetKeys = @{}
    foreach ($line in $lines) {
        if ($line -match "^\s*(#|$)") {
            continue
        }

        if ($line -notlike "*=*") {
            continue
        }

        $key = (($line -split "=", 2)[0].Trim() -split "\s+", 2)[0]
        if ($key) {
            $targetKeys[$key] = $true
        }
    }

    $added = 0
    foreach ($line in Get-Content -LiteralPath $ExampleFile) {
        if ($line -match "^\s*(#|$)") {
            continue
        }

        if ($line -notlike "*=*") {
            continue
        }

        $key = (($line -split "=", 2)[0].Trim() -split "\s+", 2)[0]
        if (-not $key) {
            continue
        }

        if (-not $targetKeys.ContainsKey($key)) {
            $lines += $line
            $targetKeys[$key] = $true
            $added += 1
        }
    }

    if ($added -gt 0) {
        Write-Utf8NoBomLines -Path $TargetFile -Lines $lines
        Write-Host "Added $added new key(s) from $ExampleFile into $TargetFile"
    }
    else {
        Write-Host "$TargetFile already contains all keys from $ExampleFile"
    }
}

function Ensure-ApiKeys {
    param([string]$EnvFile)

    $lines = @(Get-Content -LiteralPath $EnvFile)
    $apiKeyIndex = -1
    $currentValue = ""

    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^API_KEYS=(.*)$") {
            $apiKeyIndex = $i
            $currentValue = $Matches[1].Trim().Trim('"').Trim("'")
            break
        }
    }

    if ($apiKeyIndex -lt 0) {
        $lines += "API_KEYS="
        $apiKeyIndex = $lines.Count - 1
    }

    $secretPart = $currentValue
    if ($secretPart.Contains(":")) {
        $secretPart = ($secretPart -split ":", 2)[1]
    }

    $normalizedValue = $currentValue.ToLowerInvariant()
    $placeholderValues = @(
        "",
        "changeme",
        "default",
        "local:changeme",
        "local:default",
        "replace-with-a-long-random-secret",
        "local:replace-with-a-long-random-secret"
    )

    if (($placeholderValues -notcontains $normalizedValue) -and ($secretPart.Length -ge 32)) {
        Write-Host "API_KEYS already configured"
        return
    }

    $apiSecret = New-ApiSecret
    if (-not $apiSecret) {
        throw "Failed to generate API_KEYS"
    }

    $lines[$apiKeyIndex] = "API_KEYS=local:$apiSecret"
    Write-Utf8NoBomLines -Path $EnvFile -Lines $lines
    Write-Host "Generated a local API_KEYS secret in $EnvFile"
}

if (-not (Test-Path -LiteralPath $ExampleEnv -PathType Leaf)) {
    throw "Missing $ExampleEnv; cannot create setup configuration."
}

if (-not (Test-Path -LiteralPath $TargetEnv -PathType Leaf)) {
    Copy-Item -LiteralPath $ExampleEnv -Destination $TargetEnv
    Write-Host "Created $TargetEnv from $ExampleEnv"
}
else {
    Write-Host "$TargetEnv already exists; syncing new keys from $ExampleEnv"
    Sync-EnvWithExample -ExampleFile $ExampleEnv -TargetFile $TargetEnv
}

Ensure-ApiKeys -EnvFile $TargetEnv

Write-Host ""
Write-Host "Setup complete."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Review .env if you want to adjust ports, CORS, limits, or production hardening."
Write-Host "  2. Start the gateway: docker compose --profile local-docker --profile build build; docker compose --profile local-docker up -d"
Write-Host "  3. Check status: docker compose --profile local-docker ps"
