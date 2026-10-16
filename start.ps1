# VISTA – rychlý start pro vývoj (bez Dockeru)
# Spustí samostatný RAW backend a frontend paralelně
$rawRepoRoot = $PSScriptRoot

Write-Host "===  VISTA RAW – Inspekce konstrukcí  ===" -ForegroundColor Cyan

# Backend
Write-Host "`n>> Spouštím backend (FastAPI)..." -ForegroundColor Yellow
$backendJob = Start-Job -ScriptBlock {
    Set-Location "$using:rawRepoRoot\backend"
    if (-not (Test-Path ".venv")) {
        python -m venv .venv
        .venv\Scripts\pip install -r requirements.txt --quiet
    }
    .venv\Scripts\uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
}

Start-Sleep -Seconds 3

# Frontend
Write-Host ">> Spouštím frontend (Vite)..." -ForegroundColor Yellow
$frontendJob = Start-Job -ScriptBlock {
    Set-Location "$using:rawRepoRoot\frontend"
    if (-not (Test-Path "node_modules")) {
        npm install --silent
    }
    npm run dev
}

Write-Host "`n  Backend:  http://localhost:8001" -ForegroundColor Green
Write-Host "  Frontend: http://localhost:5174" -ForegroundColor Green
Write-Host "  API docs: http://localhost:8001/docs" -ForegroundColor Green
Write-Host "`nCtrl+C pro ukončení`n"

try {
    while ($true) {
        Receive-Job $backendJob, $frontendJob 2>&1 | Write-Host
        Start-Sleep -Seconds 2
    }
} finally {
    Stop-Job $backendJob, $frontendJob
    Remove-Job $backendJob, $frontendJob -Force
}
