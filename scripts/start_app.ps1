# Start (or restart) the Streamlit app pointed at a source system.
#
#   powershell -File scripts\start_app.ps1              # SAP (default demo)
#   powershell -File scripts\start_app.ps1 -Source olist
#
# Stops any running instance first, sets the source env vars, and starts
# the app detached on port 8501. This is the supported way to switch
# sources until the in-app switcher (V2) exists.
param(
    [ValidateSet("sap", "olist")]
    [string]$Source = "sap"
)

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match "streamlit" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

if ($Source -eq "olist") {
    $env:DG_SOURCE_SCHEMA = "raw_olist"
    $env:DG_ENABLE_OLIST = "true"
    $env:DG_DOMAIN_CONTEXT = "a Brazilian e-commerce order-to-delivery data product"
} else {
    $env:DG_SOURCE_SCHEMA = "raw_sap"
    $env:DG_ENABLE_OLIST = "false"
    $env:DG_DOMAIN_CONTEXT = ""
}

Start-Process -FilePath $py `
    -ArgumentList "-m", "streamlit", "run", "app/Home.py", "--server.headless", "true", "--server.port", "8501" `
    -WorkingDirectory $root -WindowStyle Hidden

Start-Sleep -Seconds 10
try {
    $code = (Invoke-WebRequest -Uri "http://localhost:8501" -UseBasicParsing -TimeoutSec 15).StatusCode
    Write-Host "App running on http://localhost:8501 (source: $Source, HTTP $code)"
} catch {
    Write-Host "App may still be starting - check http://localhost:8501 (source: $Source)"
}
