$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }

& $python -m PyInstaller --noconsole --name TableMiku --add-data "assets;assets" --add-data "table_miku/qml;table_miku/qml" main.py

Write-Host "Build finished. Check dist/TableMiku/TableMiku.exe"
