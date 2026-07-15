param(
    [string]$DistPath = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$static = Join-Path $root "provtidsbevakaren\static"
Push-Location $root
try {

if (-not (Test-Path -LiteralPath $python)) {
    python -m venv (Join-Path $root ".venv")
}
    & $python -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

$output = Join-Path $DistPath "No-Comment-Booking.exe"
$runningOutput = Get-Process -Name "No-Comment-Booking" -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and ([IO.Path]::GetFullPath($_.Path) -eq [IO.Path]::GetFullPath($output)) }
if ($runningOutput) {
    throw "Close No-Comment-Booking.exe before rebuilding it."
}
    & $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --collect-all selenium `
    --collect-all uvicorn `
    --collect-all fastapi `
    --collect-all qrcode `
    --collect-all tzdata `
    --add-data "${static};provtidsbevakaren\static" `
    --name No-Comment-Booking `
    --distpath $DistPath `
    --workpath (Join-Path $root "build") `
    --specpath (Join-Path $root "build") `
    (Join-Path $root "run.py")
    if ($LASTEXITCODE -ne 0) { throw "EXE build failed." }
    Write-Host "Built: $output"
}
finally {
    Pop-Location
}
