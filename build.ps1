$ErrorActionPreference = "Stop"

python -m pip install -r requirements.txt
python -m PyInstaller --noconsole --name TableMiku --add-data "assets;assets" --add-data "data;data" --add-data "table_miku/qml;table_miku/qml" main.py

Write-Host "Build finished. Check dist/TableMiku/TableMiku.exe"
