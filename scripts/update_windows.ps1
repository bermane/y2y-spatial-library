<#
    Y2Y Spatial Library — Windows update
    -------------------------------------
    Pulls the latest code from GitHub and reconciles dependencies.

    Run from the repository root whenever Ethan has pushed an update:

        cd C:\path\to\y2y-spatial-library
        powershell -ExecutionPolicy Bypass -File .\scripts\update_windows.ps1

    Because the package is installed "editable" (pip install -e .), a git pull
    makes new *code* live immediately. The pip step below only does real work
    when a release changed the *dependencies*; it is safe to run every time.

    NOTE: this does NOT touch your data (library/, inventory/, queue/). Those
    live outside git and are never modified by an update.
#>

$ErrorActionPreference = "Stop"

Write-Host "=== Y2Y Spatial Library — update ===" -ForegroundColor Cyan

# --- 1. Pull latest code ----------------------------------------------------
# (No-op if you already pulled via GitHub Desktop.)
if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "Pulling latest from GitHub ..." -ForegroundColor Cyan
    git pull --ff-only
} else {
    Write-Host "git not found on PATH — pull via GitHub Desktop before running this, or install Git for Windows." -ForegroundColor Yellow
}

# --- 2. Reconcile dependencies ---------------------------------------------
$venvPython = Join-Path (Resolve-Path ".venv") "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "ERROR: .venv not found. Run scripts\setup_windows.ps1 first." -ForegroundColor Red
    exit 1
}
Write-Host "Reconciling dependencies (only does work if they changed) ..." -ForegroundColor Cyan
& $venvPython -m pip install -e ".[dev]"

# --- 3. Smoke test ----------------------------------------------------------
Write-Host "-- CLI smoke --" -ForegroundColor Cyan
& $venvPython -m pipeline --help | Select-Object -First 3

Write-Host "`n=== Update complete ===" -ForegroundColor Green
