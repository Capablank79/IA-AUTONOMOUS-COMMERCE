$ErrorActionPreference = "Stop"

# ============================================================
# AI AUTONOMOUS COMMERCE
# START SCRIPT
# ============================================================

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " AI AUTONOMOUS COMMERCE" -ForegroundColor Cyan
Write-Host " SERVICE STARTUP" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ============================================================
# CONFIGURATION
# ============================================================

$OAuthHost = "127.0.0.1"
$OAuthPort = 8000

$EnvFile = Join-Path $ProjectRoot ".env"

# ============================================================
# LOAD .ENV
# ============================================================

if (-not (Test-Path $EnvFile)) {
    Write-Host "[ERROR] No existe .env:" -ForegroundColor Red
    Write-Host "        $EnvFile" -ForegroundColor Red
    exit 1
}

Write-Host "[OK] .env encontrado" -ForegroundColor Green

Get-Content $EnvFile | ForEach-Object {

    $line = $_.Trim()

    # Ignorar comentarios y líneas vacías
    if (
        $line -and
        -not $line.StartsWith("#") -and
        $line -match '^([^=]+)=(.*)$'
    ) {
        $key = $matches[1].Trim()
        $value = $matches[2].Trim()

        # Quitar comillas externas si existen
        if (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        [Environment]::SetEnvironmentVariable(
            $key,
            $value,
            "Process"
        )
    }
}

# ============================================================
# CLOUDFLARE TOKEN
# ============================================================

$TKN = $env:CLOUDFLARE_TUNNEL_TOKEN

if ([string]::IsNullOrWhiteSpace($TKN)) {
    Write-Host ""
    Write-Host "[ERROR] CLOUDFLARE_TUNNEL_TOKEN no esta configurado." -ForegroundColor Red
    Write-Host ""
    Write-Host "Debe existir en .env como:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "CLOUDFLARE_TUNNEL_TOKEN=TU_TOKEN" -ForegroundColor White
    Write-Host ""
    exit 1
}

Write-Host "[OK] Cloudflare Tunnel token encontrado" -ForegroundColor Green

# ============================================================
# CHECK CLOUDFLARED
# ============================================================

try {
    $cloudflaredVersion = cloudflared --version 2>&1
    Write-Host "[OK] cloudflared disponible" -ForegroundColor Green
}
catch {
    Write-Host "[ERROR] cloudflared no esta disponible en PATH." -ForegroundColor Red
    exit 1
}

# ============================================================
# CHECK PYTHON
# ============================================================

try {
    $pythonVersion = python --version 2>&1
    Write-Host "[OK] Python disponible: $pythonVersion" -ForegroundColor Green
}
catch {
    Write-Host "[ERROR] Python no esta disponible en PATH." -ForegroundColor Red
    exit 1
}

# ============================================================
# START OAUTH SERVER
# ============================================================

Write-Host ""
Write-Host "[1/2] Iniciando OAuth Server..." -ForegroundColor Yellow

$OAuthCommand = @"
Set-Location '$ProjectRoot'
python -m uvicorn oauth.server:app --host $OAuthHost --port $OAuthPort
"@

Start-Process powershell.exe `
    -ArgumentList @(
        "-NoExit",
        "-Command",
        $OAuthCommand
    )

Start-Sleep -Seconds 3

# ============================================================
# LOCAL HEALTH CHECK
# ============================================================

Write-Host "[CHECK] Verificando OAuth Server..." -ForegroundColor Yellow

$OAuthHealthy = $false

for ($i = 1; $i -le 10; $i++) {

    try {

        $response = Invoke-WebRequest `
            -Uri "http://127.0.0.1:$OAuthPort/health" `
            -UseBasicParsing `
            -TimeoutSec 2

        if ($response.StatusCode -eq 200) {
            $OAuthHealthy = $true
            break
        }

    }
    catch {
        Start-Sleep -Seconds 1
    }
}

if ($OAuthHealthy) {
    Write-Host "[OK] OAuth Server responde en http://127.0.0.1:$OAuthPort" -ForegroundColor Green
}
else {
    Write-Host "[ERROR] OAuth Server no responde." -ForegroundColor Red
    Write-Host "        Revisa la ventana del OAuth Server." -ForegroundColor Yellow
    exit 1
}

# ============================================================
# START CLOUDFLARE TUNNEL
# ============================================================

Write-Host ""
Write-Host "[2/2] Iniciando Cloudflare Tunnel..." -ForegroundColor Yellow

$CloudflareCommand = @"
cloudflared tunnel --no-autoupdate run --token '$TKN'
"@

Start-Process powershell.exe `
    -ArgumentList @(
        "-NoExit",
        "-Command",
        $CloudflareCommand
    )

Start-Sleep -Seconds 5

# ============================================================
# FINAL STATUS
# ============================================================

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " SERVICES STARTED" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""

Write-Host "OAuth Server" -ForegroundColor Cyan
Write-Host "  Local:    http://127.0.0.1:$OAuthPort"
Write-Host "  Health:   http://127.0.0.1:$OAuthPort/health"
Write-Host ""

Write-Host "Cloudflare Tunnel" -ForegroundColor Cyan
Write-Host "  Tunnel:   ml-oauth"
Write-Host "  Public:   https://auth.exesoft.cl"
Write-Host ""

Write-Host "Mercado Libre OAuth Callback" -ForegroundColor Cyan
Write-Host "  https://auth.exesoft.cl/oauth/mercadolibre/callback"
Write-Host ""

Write-Host "============================================================" -ForegroundColor Green
Write-Host " Startup complete." -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""