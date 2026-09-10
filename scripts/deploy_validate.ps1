# PowerShell Deployment Automation Validator (O.13)
# Ejecuta la suite de verificación de despliegue determinista con código de salida no cero ante fallos.

$ErrorActionPreference = "Stop"

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  O.13 DEPLOYMENT AUTOMATION - POWERSHELL VALIDATOR (O.13)      " -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

# 1. Verificar Dockerfile y .dockerignore
Write-Host "[1/4] Verificando Dockerfile y .dockerignore..." -ForegroundColor Yellow
$Dockerfile = Join-Path $ProjectRoot "Dockerfile"
$Dockerignore = Join-Path $ProjectRoot ".dockerignore"

if (-not (Test-Path $Dockerfile)) {
    Write-Error "Dockerfile no existe en $ProjectRoot"
    exit 1
}

if (-not (Test-Path $Dockerignore)) {
    Write-Error ".dockerignore no existe en $ProjectRoot"
    exit 1
}

$DockerContent = Get-Content $Dockerfile -Raw
if (-not ($DockerContent -match "USER appuser" -or $DockerContent -match "10001")) {
    Write-Error "Dockerfile debe usar usuario no-root appuser/10001"
    exit 1
}

Write-Host "[OK] Dockerfile y .dockerignore validados correctamente." -ForegroundColor Green

# 2. Ejecutar validación con scripts/deploy_validate.py
Write-Host "[2/4] Ejecutando validación integral de configuración y app probes..." -ForegroundColor Yellow
$DeployValidatePy = Join-Path $ScriptDir "deploy_validate.py"

python $DeployValidatePy
if ($LASTEXITCODE -ne 0) {
    Write-Error "scripts/deploy_validate.py fallo con codigo $LASTEXITCODE"
    exit $LASTEXITCODE
}
Write-Host "[OK] deploy_validate.py ejecuto con exito." -ForegroundColor Green

# 3. Ejecutar suite de pruebas pytest de O.13
Write-Host "[3/4] Ejecutando tests unitarios y de integracion O.13..." -ForegroundColor Yellow
python -m pytest tests/unit/test_o13_deployment_automation_unit.py tests/integration/test_o13_deployment_automation_integration.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "La suite de pruebas O.13 fallo con codigo $LASTEXITCODE"
    exit $LASTEXITCODE
}
Write-Host "[OK] Suite de pruebas O.13 aprobada al 100%." -ForegroundColor Green

Write-Host "[4/4] Validacion completa finalizada." -ForegroundColor Cyan
Write-Host ">>> O.13 DEPLOYMENT AUTOMATION VALIDATION COMPLETED SUCCESSFULLY <<<" -ForegroundColor Green
exit 0
