$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$backendDirectory = Join-Path $projectRoot 'backend'
$workDirectory = Join-Path $projectRoot 'work'
$clusterDirectory = Join-Path $workDirectory 'postgres-audit-cluster'
$testDatabase = 'reconflow_pg_audit'
$testPort = 55432
$databaseUrl = "postgresql+psycopg://reconflow@127.0.0.1:$testPort/$testDatabase"
$pythonCandidates = @(
    (Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'),
    (Join-Path $projectRoot 'work\.venv\Scripts\python.exe')
)
$pythonExecutable = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonExecutable) { throw 'Python virtual environment not found.' }

$postgresBin = $env:RECONFLOW_POSTGRES_BIN
if (-not $postgresBin) {
    $initdbCommand = Get-Command initdb.exe -ErrorAction SilentlyContinue
    if ($initdbCommand) { $postgresBin = Split-Path -Parent $initdbCommand.Source }
}
if (-not $postgresBin) {
    throw 'PostgreSQL tools not found. Add PostgreSQL bin to PATH or set RECONFLOW_POSTGRES_BIN.'
}
$psqlExecutable = Join-Path $postgresBin 'psql.exe'
$initdbExecutable = Join-Path $postgresBin 'initdb.exe'
$pgCtlExecutable = Join-Path $postgresBin 'pg_ctl.exe'
$pgIsReadyExecutable = Join-Path $postgresBin 'pg_isready.exe'
$postgresExecutable = Join-Path $postgresBin 'postgres.exe'
foreach ($executable in @($psqlExecutable, $initdbExecutable, $pgCtlExecutable, $pgIsReadyExecutable, $postgresExecutable)) {
    if (-not (Test-Path -LiteralPath $executable)) { throw "PostgreSQL executable not found: $executable" }
}

New-Item -ItemType Directory -Force -Path $workDirectory | Out-Null
$serverStarted = $false
$serverProcess = $null
try {
    if (-not (Test-Path -LiteralPath (Join-Path $clusterDirectory 'PG_VERSION'))) {
        & $initdbExecutable -D $clusterDirectory -U reconflow -A trust --encoding=UTF8 --no-locale 2>&1 | Tee-Object -FilePath (Join-Path $workDirectory 'postgres-audit-initdb.log')
        if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the isolated PostgreSQL cluster.' }
    }
    $quotedClusterDirectory = '"' + $clusterDirectory + '"'
    $serverProcess = Start-Process -FilePath $postgresExecutable -ArgumentList @('-D', $quotedClusterDirectory, '-p', $testPort, '-h', '127.0.0.1') -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $workDirectory 'postgres-audit-server.out.log') -RedirectStandardError (Join-Path $workDirectory 'postgres-audit-server.log')
    $serverStarted = $true
    $ready = $false
    foreach ($attempt in 1..60) {
        & $pgIsReadyExecutable -h 127.0.0.1 -p $testPort -d postgres -U reconflow *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw 'The isolated PostgreSQL server is not ready.' }
    "127.0.0.1:$testPort - accepting connections" | Tee-Object -FilePath (Join-Path $workDirectory 'postgres-audit-readiness.log')

    $databaseExists = & $psqlExecutable -h 127.0.0.1 -p $testPort -U reconflow -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$testDatabase'"
    if ($databaseExists -ne '1') {
        & $psqlExecutable -h 127.0.0.1 -p $testPort -U reconflow -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $testDatabase"
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated PostgreSQL audit database.' }
    }

    & $psqlExecutable -h 127.0.0.1 -p $testPort -U reconflow -d $testDatabase -v ON_ERROR_STOP=1 -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
    if ($LASTEXITCODE -ne 0) { throw 'Could not reset the isolated PostgreSQL audit database.' }

    $env:DATABASE_URL = $databaseUrl
    $env:RECONFLOW_POSTGRES_TEST_URL = $databaseUrl
    $env:DEMO_MODE = 'false'
    $env:RECONFLOW_TEST_INSTANCE = 'true'
    Push-Location $backendDirectory
    try {
        & $pythonExecutable -m alembic upgrade head 2>&1 | Tee-Object -FilePath (Join-Path $workDirectory 'postgres-audit-migration.log')
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL audit migration failed.' }
        & $pythonExecutable -m pytest tests\test_postgres_integration.py -q -p no:cacheprovider 2>&1 | Tee-Object -FilePath (Join-Path $workDirectory 'postgres-audit-tests.log')
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL integration tests failed.' }
        & $psqlExecutable -h 127.0.0.1 -p $testPort -U reconflow -d $testDatabase -Atc "SELECT current_database(), current_setting('server_version'), count(*) FROM payment_assignments GROUP BY 1,2" 2>&1 | Tee-Object -FilePath (Join-Path $workDirectory 'postgres-audit-database.log')
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL evidence query failed.' }
    } finally {
        Pop-Location
    }
} finally {
    if ($serverStarted) {
        & $pgCtlExecutable -D $clusterDirectory stop -m fast 2>&1 | Tee-Object -FilePath (Join-Path $workDirectory 'postgres-audit-stop.log')
        if ($serverProcess) {
            Wait-Process -Id $serverProcess.Id -Timeout 15 -ErrorAction SilentlyContinue
        }
    }
}
