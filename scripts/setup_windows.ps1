<#
    Y2Y Spatial Library - Windows first-time setup
    ------------------------------------------------
    Creates an isolated Python virtual environment (3.13 preferred, 3.12
    also supported), installs the pipeline into it (editable) at the pinned
    versions in constraints-windows.txt, then runs smoke tests and the full
    test suite.

    Run this ONCE, from the repository root, in PowerShell:

        cd C:\path\to\y2y-spatial-library
        powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1

    Prerequisites (install first - see ONBOARDING_WINDOWS.md):
      * Python 3.13  (https://www.python.org/downloads/  - check "Add to PATH")
                     3.12 also works. 3.14 does NOT (no fiona build yet).
      * Git          (https://git-scm.com/download/win)  - only to clone/update
    ArcGIS Pro is NOT required to run the pipeline; it is used separately
    (its own Python), only for the manual VTPK build for vector-tile layers.
#>

$ErrorActionPreference = "Stop"

Write-Host "=== Y2Y Spatial Library - Windows setup ===" -ForegroundColor Cyan

# --- 1. Locate Python 3.13 (or 3.12) ----------------------------------------
# Prefer the 'py' launcher; fall back to 'python' on PATH. Python 3.14 and
# newer are refused: fiona has no build for them yet, so the install fails.
$pyExe  = $null
$pyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @("3.13", "3.12")) {
        & py "-$v" --version 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $pyExe  = "py"
            $pyArgs = @("-$v")
            break
        }
    }
}
if (-not $pyExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $ver = (& python --version 2>&1 | Out-String)
    if ($ver -match "Python 3\.1[23]\.") {
        $pyExe  = "python"
        $pyArgs = @()
    }
}
if (-not $pyExe) {
    Write-Host "ERROR: Python 3.13 (or 3.12) was not found." -ForegroundColor Red
    Write-Host "Install Python 3.13 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')," -ForegroundColor Yellow
    Write-Host "then re-run this script. Python 3.14 and newer are not supported yet." -ForegroundColor Yellow
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
& $venvPython -m pip install -e ".[dev]" -c constraints-windows.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: the install failed. Nothing further was run." -ForegroundColor Red
    exit 1
}

# --- 4. Smoke tests ---------------------------------------------------------
Write-Host ""
Write-Host "=== Smoke tests ===" -ForegroundColor Cyan

Write-Host "-- CLI entry point --" -ForegroundColor Cyan
& $venvPython -m pipeline --help | Select-Object -First 3

Write-Host "-- Core imports --" -ForegroundColor Cyan
& $venvPython -c "import geopandas, rasterio, fiona, shapely, pyproj, arcgis, matplotlib, openpyxl, click; print('OK: all core imports succeeded')"

# --- 5. Full test suite -----------------------------------------------------
# Runs against temporary folders only: it does not read or change the data
# root and does not contact ArcGIS Online. Takes a couple of minutes.
Write-Host ""
Write-Host "=== Test suite (a couple of minutes) ===" -ForegroundColor Cyan
& $venvPython -m pytest -q -p no:cacheprovider
$testsOk = ($LASTEXITCODE -eq 0)

# --- 6. Record the installed versions ---------------------------------------
Write-Host "-- Writing requirements.lock.win --" -ForegroundColor Cyan
& $venvPython -m pip freeze | Out-File -Encoding ascii "requirements.lock.win"

Write-Host ""
if (-not $testsOk) {
    Write-Host "=== Setup finished, but some tests FAILED ===" -ForegroundColor Red
    Write-Host "Do not run y2y against the real library yet." -ForegroundColor Yellow
    Write-Host "Send the test output above to Ethan." -ForegroundColor Yellow
    exit 1
}
Write-Host "=== Setup complete - all tests passed ===" -ForegroundColor Green
Write-Host "Activate the environment in new terminals with:" -ForegroundColor Yellow
Write-Host "    .\.venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "Then run:  y2y --help" -ForegroundColor White
Write-Host "Next: set the Y2Y_AGOL_CLIENT_ID variable and run 'y2y agol-sync login' (see ONBOARDING_WINDOWS.md)." -ForegroundColor Yellow
