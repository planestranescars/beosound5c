$root = "$PSScriptRoot"

Write-Host "Stopping any existing services..." -ForegroundColor DarkGray

# Kill any python processes running our scripts
Get-WmiObject Win32_Process -Filter "Name='python.exe' OR Name='python3.exe'" | ForEach-Object {
    $cmd = $_.CommandLine
    if ($cmd -match 'tidal.service|input\.py|http\.server') {
        Write-Host "  Killing: $cmd" -ForegroundColor DarkGray
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep 1

Write-Host "Starting BeoSound 5c dev stack from $root" -ForegroundColor Cyan

Start-Process powershell -ArgumentList "-NoExit", "-Command",
    "Set-Location '$root'; Write-Host 'TIDAL SERVICE' -ForegroundColor Cyan; python services/sources/tidal/service.py"

Start-Process powershell -ArgumentList "-NoExit", "-Command",
    "Set-Location '$root'; Write-Host 'INPUT / HID' -ForegroundColor Yellow; python services/input.py"

Start-Process powershell -ArgumentList "-NoExit", "-Command",
    "Set-Location '$root'; Write-Host 'HTTP SERVER  ->  http://localhost:8000/web/softarc/tidal.html' -ForegroundColor Green; python -m http.server 8000"

Start-Sleep 2
Start-Process "msedge" "http://localhost:8000/web/softarc/tidal.html"

Write-Host "Done. Three service windows opened + Edge launched." -ForegroundColor Cyan
