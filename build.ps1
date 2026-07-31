$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }

Push-Location $projectRoot
try {
    & $python (Join-Path $projectRoot "build.py")
}
finally {
    Pop-Location
}

Write-Host "Build finished. Check dist/TableMiku/TableMiku.exe"
