<#
    Y2Y Spatial Library — Windows first-time setup
    ------------------------------------------------
    Creates an isolated Python 3.12 virtual environment and installs the
    pipeline into it (editable), then runs smoke tests.

    Run this ONCE, from the repository root, in PowerShell:

        cd C:\path\to\y2y-spatial-library
        powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1

    Prerequisites (install first — see ONBOARDING_WINDOWS.md):
      * Python 3.12  (https://www.python.org/downloads/  — check "Add to PATH")
      * Git          (https://git-scm.com/download/win)  — only needed to clone/update
    ArcGIS Pro is NOT required to run the pipeline; it is used separately
    (its own Python), only for the manual VTPK build step for vector-tile layers.
#>

$ErrorActionPreference = "Stop"

Write-Host "=== Y2Y Spatial Library — Windows setup ===" -ForegroundColor Cyan

# --- 1. Locate Python 3.12 --------------------------------------------------
# Prefer the py launcher; fall back to python on PATH.
$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    try { & py -3.12 --version *>$null; if ($LASTEXITCODE -eq 0) { $py = @("py","-3.12") } } catch {}
}
if (-not $py -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $v = & python --version 2>&1
    if ($v -match "3\.1[2-9]") { $py = @("python") }
}
if (-not $py) {
    Write-Host "ERROR: Python 3.12 was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')," -ForegroundColor Yellow
    Write-Host "then re-run this script." -ForegroundColor Yellow
    exit 1
}
Write-Host "Using Python: $($py -join ' ')" -ForegroundColor Green

# --- 2. Create the virtual environment -------------------------------------
if (Test-Path ".venv") {
    Write-Host ".venv already exists — reusing it." -ForegroundColor Yellow
} else {
    Write-Host "Creating .venv ..." -ForegroundColor Cyan
    & $py[0] $py[1..($py.Count-1)] -m venv .venv
}

$venvPython = Join-Path (Resolve-Path ".venv") "Scripts\python.exe"
if (-not (Test-Path $venvPython)) { Write-Host "ERROR: venv python not found at $venvPython" -ForegroundColor Red; exit 1 }

# --- 3. Install the pipeline (editable) ------------------------------------
Write-Host "Upgrading pip ..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip

Write-Host "Installing y2y-spatial-library + dependencies (this can take several minutes) ..." -ForegroundColor Cyan
& $venvPython -m pip install -e ".[dev]"

# --- 4. Smoke tests ---------------------------------------------------------
Write-Host "`n=== Smoke tests ===" -ForegroundColor Cyan

Write-Host "-- CLI entry point --" -ForegroundColor Cyan
& $venvPython -m pipeline --help | Select-Object -First 3

Write-Host "-- Core imports --" -ForegroundColor Cyan
& $venvPython -c "import geopandas, rasterio, fiona, shapely, pyproj, arcgis, matplotlib, openpyxl, click; print('OK: all core imports succeeded')"

# --- 5. Freeze exact versions for reproducibility --------------------------
Write-Host "-- Writing requirements.lock.win --" -ForegroundColor Cyan
& $venvPython -m pip freeze | Out-File -Encoding utf8 "requirements.lock.win"

Write-Host "`n=== Setup complete ===" -ForegroundColor Green
Write-Host "Activate the environment in new terminals with:" -ForegroundColor Yellow
Write-Host "    .\.venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "Then run:  y2y --help" -ForegroundColor White
Write-Host "Next: set `$env:Y2Y_AGOL_CLIENT_ID and run 'y2y agol-sync login' (see ONBOARDING_WINDOWS.md)." -ForegroundColor Yellow
