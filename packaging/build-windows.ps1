# Build dist\ptdesk\ and dist\ptdesk-windows.zip on Windows.
# Usage (PowerShell, in the project folder):
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

# Separate venv name so it does not clash with a Linux .venv in a synced folder
$venv = ".venv-win"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    py -3 -m venv $venv
}
$py = "$venv\Scripts\python.exe"

& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install fehlgeschlagen" }

& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests fehlgeschlagen" }

& $py packaging\make_icon.py
& "$venv\Scripts\pyinstaller.exe" --noconfirm --clean --distpath dist --workpath build packaging\ptdesk.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fehlgeschlagen" }

Copy-Item README.md, LICENSE, THIRD_PARTY_NOTICES.md dist\ptdesk\
Compress-Archive -Path dist\ptdesk -DestinationPath dist\ptdesk-windows.zip -Force
Write-Host "Fertig: dist\ptdesk\ptdesk.exe und dist\ptdesk-windows.zip"
