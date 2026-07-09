<#
    Y2Y Spatial Library - create a data root
    -----------------------------------------
    Scaffolds the folder structure the pipeline reads/writes (library/,
    queue/, inventory/, reports/) at a location of your choice - typically
    a SharePoint/OneDrive folder so the library files are shared with the
    team, while the CODE + venv live on a separate LOCAL path.

    Run from the code repo root:

        powershell -ExecutionPolicy Bypass -File .\scripts\new_data_root.ps1 `
            -DataRoot "C:\Users\<you>\OneDrive - ...\Y2Y_Spatial_Library"

    After this, work by activating the local venv and pointing the pipeline
    at the data root, e.g.:

        C:\Y2Y\y2y-spatial-library\.venv\Scripts\Activate.ps1
        y2y --root "C:\Users\<you>\OneDrive - ...\Y2Y_Spatial_Library" ingest

    IMPORTANT: pause OneDrive/SharePoint sync while running y2y commands, so
    the live inventory.db is never synced mid-write. See DEPLOYMENT.md.
#>

param(
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [string]$CodeRoot = "."
)

$ErrorActionPreference = "Stop"

Write-Host "=== Creating Y2Y data root at: $DataRoot ===" -ForegroundColor Cyan

# Top-level working directories.
$dirs = @(
    "queue\incoming",
    "queue\processing",
    "queue\rejected",
    "queue\archived",
    "inventory",
    "reports"
)
foreach ($d in $dirs) {
    $full = Join-Path $DataRoot $d
    New-Item -ItemType Directory -Force -Path $full | Out-Null
    Write-Host "  + $d"
}

# Copy the library/ folder skeleton (the 10 category folders + Species
# subcategories) from the code repo so the taxonomy is visible up front.
$srcLib = Join-Path $CodeRoot "library"
$dstLib = Join-Path $DataRoot "library"
if (Test-Path $srcLib) {
    Copy-Item -Recurse -Force $srcLib $dstLib
    Write-Host "  + library\ (category skeleton copied from code repo)"
}
else {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataRoot "library\spatial") | Out-Null
    Write-Host "  + library\spatial\ (empty; code repo skeleton not found)"
}

Write-Host ""
Write-Host "=== Data root ready ===" -ForegroundColor Green
Write-Host "Drop source files into:  $(Join-Path $DataRoot 'queue\incoming')" -ForegroundColor White
Write-Host "Run the pipeline with:   y2y --root `"$DataRoot`" <command>" -ForegroundColor White
Write-Host "Pause OneDrive sync while running y2y commands (see DEPLOYMENT.md)." -ForegroundColor Yellow
