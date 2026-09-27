$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$backendDirectory = Join-Path $projectRoot 'backend'
$frontendDirectory = Join-Path $projectRoot 'frontend'
$workDirectory = Join-Path $projectRoot 'work'
$testDatabase = 'reconflow_e2e'
$databasePort = if ($env:RECONFLOW_E2E_DATABASE_PORT) { $env:RECONFLOW_E2E_DATABASE_PORT } else { '5432' }
$databaseUrl = "postgresql+psycopg://reconflow:reconflow@127.0.0.1:$databasePort/$testDatabase"

$pythonCandidates = @(
    (Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'),
    (Join-Path $projectRoot 'work\.venv\Scripts\python.exe')
)
$pythonExecutable = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonExecutable) {
    throw 'Python virtual environment not found in backend\.venv or work\.venv.'
}

$psqlCommand = Get-Command psql.exe -ErrorAction SilentlyContinue
$psqlExecutable = if ($env:RECONFLOW_POSTGRES_BIN) {
    Join-Path $env:RECONFLOW_POSTGRES_BIN 'psql.exe'
} elseif ($psqlCommand) {
    $psqlCommand.Source
} else {
    throw 'psql was not found. Add PostgreSQL bin to PATH or set RECONFLOW_POSTGRES_BIN.'
}
if (-not (Test-Path -LiteralPath $psqlExecutable)) {
    throw "PostgreSQL executable not found: $psqlExecutable"
}

$nodeExecutable = (Get-Command node -ErrorAction Stop).Source
$pnpmExecutable = (Get-Command pnpm -ErrorAction Stop).Source
$viteEntry = Join-Path $frontendDirectory 'node_modules\vite\bin\vite.js'
if (-not (Test-Path -LiteralPath $viteEntry)) {
    throw 'Frontend dependencies are missing. Run pnpm install --frozen-lockfile.'
}

New-Item -ItemType Directory -Force -Path $workDirectory | Out-Null
$env:PGPASSWORD = 'reconflow'
$env:PGPORT = $databasePort
$databaseExists = & $psqlExecutable -h 127.0.0.1 -U reconflow -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$testDatabase'"
if ($databaseExists -ne '1') {
    & $psqlExecutable -h 127.0.0.1 -U reconflow -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $testDatabase"
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated test database.' }
}

# Only the schema in the dedicated reconflow_e2e database is reset.
& $psqlExecutable -h 127.0.0.1 -U reconflow -d $testDatabase -v ON_ERROR_STOP=1 -c 'SET client_min_messages TO warning; DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
if ($LASTEXITCODE -ne 0) { throw 'Could not reset the isolated test database.' }

$env:DATABASE_URL = $databaseUrl
$env:DEMO_MODE = 'false'
$env:RECONFLOW_TEST_INSTANCE = 'true'
Push-Location $backendDirectory
try {
    # Windows PowerShell 5 classifies ordinary native stderr (Alembic INFO) as an error record.
    $ErrorActionPreference = 'Continue'
    try { & $pythonExecutable -m alembic upgrade head 2>&1; $migrationExit = $LASTEXITCODE }
    finally { $ErrorActionPreference = 'Stop' }
    if ($migrationExit -ne 0) { throw 'Isolated test database migrations failed.' }
} finally {
    Pop-Location
}

$backendProcess = $null
$frontendProcess = $null
try {
    $backendProcess = Start-Process -FilePath $pythonExecutable -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8010') -WorkingDirectory $backendDirectory -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $workDirectory 'e2e-backend.out.log') -RedirectStandardError (Join-Path $workDirectory 'e2e-backend.err.log')

    $env:VITE_API_TARGET = 'http://127.0.0.1:8010'
    $frontendProcess = Start-Process -FilePath $nodeExecutable -ArgumentList @($viteEntry, '--host', '127.0.0.1', '--port', '5180', '--strictPort') -WorkingDirectory $frontendDirectory -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $workDirectory 'e2e-frontend.out.log') -RedirectStandardError (Join-Path $workDirectory 'e2e-frontend.err.log')

    $ready = $false
    foreach ($attempt in 1..60) {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8010/api/health' -TimeoutSec 1
            $front = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:5180' -TimeoutSec 1
            if ($health.status -eq 'ok' -and $health.test_instance -eq $true -and $front.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $ready) { throw 'Isolated test services did not become ready.' }

    $env:PLAYWRIGHT_BASE_URL = 'http://127.0.0.1:5180'
    Push-Location $frontendDirectory
    try {
        $ErrorActionPreference = 'Continue'
        try { & $pnpmExecutable exec playwright test --workers=1 2>&1; $playwrightExit = $LASTEXITCODE }
        finally { $ErrorActionPreference = 'Stop' }
        if ($playwrightExit -ne 0) { throw 'Playwright test failed.' }
    } finally {
        Pop-Location
    }

    # Restart only the API process and verify that decisions and calculated values
    # are read back from the dedicated PostgreSQL database, not process memory.
    if ($backendProcess -and -not $backendProcess.HasExited) {
        $backendProcessId = $backendProcess.Id
        Stop-Process -Id $backendProcessId -Force -ErrorAction Stop
        Wait-Process -Id $backendProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
    $backendProcess = Start-Process -FilePath $pythonExecutable -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8010') -WorkingDirectory $backendDirectory -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $workDirectory 'e2e-backend-restart.out.log') -RedirectStandardError (Join-Path $workDirectory 'e2e-backend-restart.err.log')
    $restartReady = $false
    foreach ($attempt in 1..60) {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8010/api/health' -TimeoutSec 1
            if ($health.status -eq 'ok' -and $health.test_instance -eq $true) {
                $restartReady = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $restartReady) { throw 'Isolated API did not become ready after restart.' }

    $reviewCases = @(Invoke-RestMethod -Uri 'http://127.0.0.1:8010/api/cases?status=in_progress' -TimeoutSec 5)
    if ($reviewCases.Count -ne 1) { throw "Expected one reopened case after restart, got $($reviewCases.Count)." }
    $caseDetail = Invoke-RestMethod -Uri "http://127.0.0.1:8010/api/cases/$($reviewCases[0].id)" -TimeoutSec 5
    $persistedComment = 'Audit restart persistence decision without confirmed recovery.'
    if (@($caseDetail.history | Where-Object { $_.comment -eq $persistedComment }).Count -eq 0) {
        throw 'The saved decision comment was not preserved after API restart.'
    }
    $summary = Invoke-RestMethod -Uri 'http://127.0.0.1:8010/api/summary' -TimeoutSec 5
    $pln = $summary.by_currency | Where-Object { $_.currency -eq 'PLN' }
    if ($summary.active_alerts -ne 1 -or $pln.amount_differences.total -ne '50.00') {
        throw 'Status or financial difference changed after API restart.'
    }
    Write-Output 'Restart persistence check passed: prior decision retained, reopened active=1, PLN difference=50.00.'
} finally {
    if ($frontendProcess -and -not $frontendProcess.HasExited) {
        Stop-Process -Id $frontendProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($backendProcess -and -not $backendProcess.HasExited) {
        Stop-Process -Id $backendProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
