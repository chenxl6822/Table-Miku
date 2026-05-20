$ErrorActionPreference = "Stop"

python -m pip install -r requirements.txt
python -m PyInstaller --noconsole --name TableMiku --add-data "assets;assets" main.py

Write-Host "Build finished. Check dist/TableMiku/TableMiku.exe"
