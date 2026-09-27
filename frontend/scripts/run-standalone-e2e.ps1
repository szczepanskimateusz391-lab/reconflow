$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$workDirectory = Join-Path $projectRoot 'work'
$clusterDirectory = Join-Path $workDirectory 'postgres-e2e-cluster'
$postgresBin = $env:RECONFLOW_POSTGRES_BIN
if (-not $postgresBin) {
    $initdbCommand = Get-Command initdb.exe -ErrorAction SilentlyContinue
    if ($initdbCommand) { $postgresBin = Split-Path -Parent $initdbCommand.Source }
}
if (-not $postgresBin) {
    throw 'PostgreSQL tools not found. Add PostgreSQL bin to PATH or set RECONFLOW_POSTGRES_BIN.'
}
$initdbExecutable = Join-Path $postgresBin 'initdb.exe'
$postgresExecutable = Join-Path $postgresBin 'postgres.exe'
$pgCtlExecutable = Join-Path $postgresBin 'pg_ctl.exe'
$pgIsReadyExecutable = Join-Path $postgresBin 'pg_isready.exe'
foreach ($executable in @($initdbExecutable, $postgresExecutable, $pgCtlExecutable, $pgIsReadyExecutable)) {
    if (-not (Test-Path -LiteralPath $executable)) { throw "PostgreSQL executable not found: $executable" }
}
if ($env:RECONFLOW_E2E_DATABASE_PORT) {
    $testPort = [int]$env:RECONFLOW_E2E_DATABASE_PORT
} else {
    # Let Windows choose an available loopback port; fixed ports may become reserved.
    $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $probe.Start()
    try { $testPort = ([System.Net.IPEndPoint]$probe.LocalEndpoint).Port }
    finally { $probe.Stop() }
}
$serverProcess = $null
$serverStarted = $false

New-Item -ItemType Directory -Force -Path $workDirectory | Out-Null
try {
    if (-not (Test-Path -LiteralPath (Join-Path $clusterDirectory 'PG_VERSION'))) {
        & $initdbExecutable -D $clusterDirectory -U reconflow -A trust --encoding=UTF8 --no-locale 2>&1 | Tee-Object -FilePath (Join-Path $workDirectory 'e2e-postgres-initdb.log')
        if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the isolated E2E PostgreSQL cluster.' }
    }
    if (Test-Path -LiteralPath (Join-Path $clusterDirectory 'postmaster.pid')) {
        throw "The isolated E2E PostgreSQL cluster is already running. Stop that test instance before starting another run; this script will not stop a process it did not start."
    }
    $quotedClusterDirectory = '"' + $clusterDirectory + '"'
    $serverProcess = Start-Process -FilePath $postgresExecutable -ArgumentList @('-D', $quotedClusterDirectory, '-p', $testPort, '-h', '127.0.0.1') -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $workDirectory 'e2e-postgres.out.log') -RedirectStandardError (Join-Path $workDirectory 'e2e-postgres.log')
    $serverStarted = $true
    $ready = $false
    foreach ($attempt in 1..60) {
        & $pgIsReadyExecutable -h 127.0.0.1 -p $testPort -d postgres -U reconflow *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw "The isolated E2E PostgreSQL server is not ready on port $testPort. Check work/e2e-postgres.log." }
    $env:RECONFLOW_E2E_DATABASE_PORT = "$testPort"
    & (Join-Path $PSScriptRoot 'run-isolated-e2e.ps1') 2>&1 | Tee-Object -FilePath (Join-Path $workDirectory 'e2e-full-run.log')
    if ($LASTEXITCODE -ne 0) { throw 'The isolated E2E flow failed.' }
} finally {
    if ($serverStarted -and (Test-Path -LiteralPath (Join-Path $clusterDirectory 'postmaster.pid'))) {
        & $pgCtlExecutable -D $clusterDirectory stop -m fast 2>&1 | Tee-Object -FilePath (Join-Path $workDirectory 'e2e-postgres-stop.log')
        if ($serverProcess) { Wait-Process -Id $serverProcess.Id -Timeout 15 -ErrorAction SilentlyContinue }
    }
}
