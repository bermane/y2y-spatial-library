<#
    Y2Y Spatial Library - Windows first-time setup
    ------------------------------------------------
    Creates an isolated Python 3.12 virtual environment and installs the
    pipeline into it (editable), then runs smoke tests.

    Run this ONCE, from the repository root, in PowerShell:

        cd C:\path\to\y2y-spatial-library
        powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1

    Prerequisites (install first - see ONBOARDING_WINDOWS.md):
      * Python 3.12  (https://www.python.org/downloads/  - check "Add to PATH")
      * Git          (https://git-scm.com/download/win)  - only to clone/update
    ArcGIS Pro is NOT required to run the pipeline; it is used separately
    (its own Python), only for the manual VTPK build for vector-tile layers.
#>

$ErrorActionPreference = "Stop"

Write-Host "=== Y2Y Spatial Library - Windows setup ===" -ForegroundColor Cyan

# --- 1. Locate Python 3.12 --------------------------------------------------
# Prefer the 'py' launcher; fall back to 'python' on PATH.
$pyExe  = $null
$pyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 --version 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $pyExe  = "py"
        $pyArgs = @("-3.12")
    }
}
if (-not $pyExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $ver = (& python --version 2>&1 | Out-String)
    if ($ver -match "3\.1[2-9]") {
        $pyExe  = "python"
        $pyArgs = @()
    }
}
if (-not $pyExe) {
    Write-Host "ERROR: Python 3.12 was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')," -ForegroundColor Yellow
    Write-Host "then re-run this script." -ForegroundColor Yellow
    exit 1
}
Write-Host ("Using Python: {0} {1}" -f $pyExe, ($pyArgs -join ' ')) -ForegroundColor Green

# --- 2. Create the virtual environment -------------------------------------
if (Test-Path ".venv") {
    Write-Host ".venv already exists - reusing it." -ForegroundColor Yellow
}
else {
    Write-Host "Creating .venv ..." -ForegroundColor Cyan
    & $pyExe @pyArgs -m venv .venv
}

$venvPython = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "ERROR: venv python not found at $venvPython" -ForegroundColor Red
    exit 1
}

# --- 3. Install the pipeline (editable) ------------------------------------
Write-Host "Upgrading pip ..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip

Write-Host "Installing y2y-spatial-library + dependencies (can take several minutes) ..." -ForegroundColor Cyan
& $venvPython -m pip install -e ".[dev]"

# --- 4. Smoke tests ---------------------------------------------------------
Write-Host ""
Write-Host "=== Smoke tests ===" -ForegroundColor Cyan

Write-Host "-- CLI entry point --" -ForegroundColor Cyan
& $venvPython -m pipeline --help | Select-Object -First 3

Write-Host "-- Core imports --" -ForegroundColor Cyan
& $venvPython -c "import geopandas, rasterio, fiona, shapely, pyproj, arcgis, matplotlib, openpyxl, click; print('OK: all core imports succeeded')"

# --- 5. Freeze exact versions for reproducibility --------------------------
Write-Host "-- Writing requirements.lock.win --" -ForegroundColor Cyan
& $venvPython -m pip freeze | Out-File -Encoding ascii "requirements.lock.win"

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host "Activate the environment in new terminals with:" -ForegroundColor Yellow
Write-Host "    .\.venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "Then run:  y2y --help" -ForegroundColor White
Write-Host "Next: set the Y2Y_AGOL_CLIENT_ID variable and run 'y2y agol-sync login' (see ONBOARDING_WINDOWS.md)." -ForegroundColor Yellow
