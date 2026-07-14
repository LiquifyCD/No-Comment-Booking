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

$output = Join-Path $root "dist\Provtidsbevakaren.exe"
if (Get-Process -Name "Provtidsbevakaren" -ErrorAction SilentlyContinue) {
    throw "Close Provtidsbevakaren.exe before rebuilding it."
}
    & $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --collect-all selenium `
    --collect-all uvicorn `
    --collect-all fastapi `
    --add-data "${static};provtidsbevakaren\static" `
    --name Provtidsbevakaren `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build") `
    --specpath (Join-Path $root "build") `
    (Join-Path $root "run.py")
    if ($LASTEXITCODE -ne 0) { throw "EXE build failed." }
    Write-Host "Built: $output"
}
finally {
    Pop-Location
}
