Clear-Host

Write-Host ""
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "           JEWLZ CFD SERVER LAUNCHER" -ForegroundColor Cyan
Write-Host "        Quick Tunnel / Launch-Day Mode" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host ""

# ----------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------
$CFDPath       = "C:\Users\JOEL\Desktop\Jewlz tech\Jewlz Fluid Dynamic force Simulator\CFD App"
$ApiKey        = "JewlzCFD2026SecureKey"
$LocalBackend  = "http://localhost:8000"
$ContainerName = "jewlz-openfoam"

# Quick Tunnel mode creates a temporary trycloudflare.com URL.
# This avoids the backend.jewlztech.com DNS issue for now.
$TunnelLog     = Join-Path $env:TEMP "jewlz_cfd_cloudflared_tunnel.log"

# ----------------------------------------------------
# HELPERS
# ----------------------------------------------------
function Start-PowerShellWindow {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Command
    )

    # EncodedCommand avoids quote/path problems with spaces like "Jewlz tech"
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($Command)
    $encoded = [Convert]::ToBase64String($bytes)

    Start-Process powershell.exe -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", $encoded
    )
}

function Wait-ForLocalBackend {
    param(
        [int]$TimeoutSeconds = 60
    )

    $start = Get-Date

    while (((Get-Date) - $start).TotalSeconds -lt $TimeoutSeconds) {
        try {
            $response = curl.exe -s "$LocalBackend/health"
            if ($LASTEXITCODE -eq 0 -and $response -match '"status"\s*:\s*"ok"') {
                return $true
            }
        }
        catch {}

        Start-Sleep -Seconds 2
    }

    return $false
}

function Wait-ForQuickTunnelUrl {
    param(
        [int]$TimeoutSeconds = 90
    )

    $start = Get-Date
    $pattern = "https://[a-zA-Z0-9-]+\.trycloudflare\.com"

    while (((Get-Date) - $start).TotalSeconds -lt $TimeoutSeconds) {
        if (Test-Path -LiteralPath $TunnelLog) {
            $logText = Get-Content -LiteralPath $TunnelLog -Raw -ErrorAction SilentlyContinue
            $match = [regex]::Match($logText, $pattern)

            if ($match.Success) {
                return $match.Value
            }
        }

        Start-Sleep -Seconds 2
    }

    return $null
}

# ----------------------------------------------------
# VALIDATE CFD FOLDER
# ----------------------------------------------------
if (-not (Test-Path -LiteralPath $CFDPath)) {
    Write-Host "ERROR: CFD folder was not found:" -ForegroundColor Red
    Write-Host $CFDPath -ForegroundColor Yellow
    Pause
    exit 1
}

Set-Location -LiteralPath $CFDPath

# ----------------------------------------------------
# CLEAR OLD TUNNEL LOG
# ----------------------------------------------------
if (Test-Path -LiteralPath $TunnelLog) {
    Remove-Item -LiteralPath $TunnelLog -Force -ErrorAction SilentlyContinue
}

# ----------------------------------------------------
# START DOCKER DESKTOP
# ----------------------------------------------------
Write-Host "Starting Docker Desktop..."
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe" -ErrorAction SilentlyContinue

Write-Host "Waiting for Docker startup..."
Start-Sleep -Seconds 20

# ----------------------------------------------------
# START OPENFOAM CONTAINER
# ----------------------------------------------------
Write-Host "Starting OpenFOAM container..."
try {
    docker start $ContainerName | Out-Null
}
catch {
    Write-Host "WARNING: Could not start Docker/OpenFOAM container yet." -ForegroundColor Yellow
    Write-Host "Make sure Docker Desktop is fully running." -ForegroundColor Yellow
}

# ----------------------------------------------------
# LAUNCH BACKEND
# ----------------------------------------------------
Write-Host "Launching backend..."

$BackendCommand = @"
Set-Location -LiteralPath "$CFDPath"
`$env:JEWLZ_CFD_API_KEY = "$ApiKey"
python -m uvicorn backend_api:app --host 0.0.0.0 --port 8000
"@

Start-PowerShellWindow -Command $BackendCommand

Write-Host "Waiting for local backend health..."
$backendReady = Wait-ForLocalBackend -TimeoutSeconds 75

if (-not $backendReady) {
    Write-Host ""
    Write-Host "ERROR: Local backend did not become ready at $LocalBackend/health" -ForegroundColor Red
    Write-Host "Check the backend PowerShell window for errors." -ForegroundColor Yellow
    Pause
    exit 1
}

Write-Host "Local backend is ready." -ForegroundColor Green

# ----------------------------------------------------
# LAUNCH TEMPORARY CLOUDFLARE QUICK TUNNEL
# ----------------------------------------------------
Write-Host "Launching Cloudflare Quick Tunnel..."

$TunnelCommand = @"
cloudflared tunnel --url $LocalBackend 2>&1 | Tee-Object -FilePath "$TunnelLog"
"@

Start-PowerShellWindow -Command $TunnelCommand

Write-Host "Waiting for Cloudflare tunnel URL..."
$QuickTunnelUrl = Wait-ForQuickTunnelUrl -TimeoutSeconds 120

if ([string]::IsNullOrWhiteSpace($QuickTunnelUrl)) {
    Write-Host ""
    Write-Host "ERROR: Could not find a trycloudflare.com tunnel URL." -ForegroundColor Red
    Write-Host "Check the Cloudflare tunnel PowerShell window." -ForegroundColor Yellow
    Write-Host "Log file:" $TunnelLog
    Pause
    exit 1
}

# ----------------------------------------------------
# TEST QUICK TUNNEL HEALTH
# ----------------------------------------------------
Write-Host ""
Write-Host "Testing quick tunnel health..."
curl.exe "$QuickTunnelUrl/health"

# ----------------------------------------------------
# READY MESSAGE
# ----------------------------------------------------
Write-Host ""
Write-Host "====================================================" -ForegroundColor Green
Write-Host "             JEWLZ CFD SERVER READY" -ForegroundColor Green
Write-Host "====================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Temporary Backend URL for Streamlit:"
Write-Host $QuickTunnelUrl -ForegroundColor Yellow
Write-Host ""
Write-Host "Update Streamlit Secrets to:" -ForegroundColor Cyan
Write-Host "CFD_BACKEND_URL = `"$QuickTunnelUrl`""
Write-Host "CFD_BACKEND_API_KEY = `"$ApiKey`""
Write-Host ""
Write-Host "Important:" -ForegroundColor Yellow
Write-Host "This Quick Tunnel URL changes each time the tunnel is restarted."
Write-Host "Leave the backend and tunnel PowerShell windows running while customers use the app."
Write-Host ""
Write-Host "Local backend check:"
Write-Host "$LocalBackend/health"
Write-Host ""

Pause