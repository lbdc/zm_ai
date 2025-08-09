# Check current execution policy
$currentPolicy = Get-ExecutionPolicy -Scope CurrentUser

if ($currentPolicy -eq "Restricted") {
    Write-Host "`nYour PowerShell execution policy is currently set to 'Restricted'.`n" -ForegroundColor Red
    Write-Host "To allow this script to run, execute the following command manually:" -ForegroundColor Yellow
    Write-Host "`nSet-ExecutionPolicy RemoteSigned -Scope CurrentUser`n" -ForegroundColor Cyan
    Write-Host "Then rerun this script." -ForegroundColor Yellow
    exit
}

# Set UTF-8 encoding for Python
$env:PYTHONIOENCODING = "utf-8"

# Check if virtual environment is already active
if (-not $env:VIRTUAL_ENV) {
    $venvPath = ".\venv\Scripts\Activate.ps1"
    if (Test-Path $venvPath) {
        Write-Host "`nActivating virtual environment..." -ForegroundColor Cyan
        & $venvPath
    } else {
        Write-Host "`nVirtual environment not found at $venvPath" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "`nVirtual environment already active: $env:VIRTUAL_ENV" -ForegroundColor Green
}

# Run the app
Write-Host "`nStarting FastAPI app..." -ForegroundColor Green
python .\zm_ai.py --loop
