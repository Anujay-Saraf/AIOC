param(
    [int]$ApiPort = 8000,
    [int]$WebPort = 3000,
    [switch]$Dev,
    [switch]$SkipInstall,
    [switch]$Rebuild
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$webRoot = Join-Path $root "web"
$python = Join-Path $root "venv\Scripts\python.exe"

function Stop-PortOwner {
    param([int]$Port)
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        $owner = $connection.OwningProcess
        if ($owner -and $owner -ne $PID) {
            Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
        }
    }
}

function Stop-ChildProcess {
    param($Process)
    if ($Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
}

function Test-PortAvailable {
    param([int]$Port)
    try {
        $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        if ($connections) {
            return $false
        }
        return $true
    } catch {
        # Fallback for environments where Get-NetTCPConnection is unavailable or restricted
        $netstatLines = netstat -ano | Select-String "^\s*TCP\s+[^\s]+:$Port\s+[^\s]+\s+LISTENING"
        return -not $netstatLines
    }
}

function Get-FreePort {
    param(
        [int]$StartPort = 8000,
        [int]$MaxPort = 9000
    )
    for ($port = $StartPort; $port -le $MaxPort; $port++) {
        if (Test-PortAvailable -Port $port) {
            return $port
        }
    }
    throw "No free TCP port found between $StartPort and $MaxPort."
}

function Start-Frontend {
    param(
        [int]$BasePort,
        [int]$MaxPort = 3100
    )

    for ($port = $BasePort; $port -le $MaxPort; $port++) {
        if (-not (Test-PortAvailable -Port $port)) {
            Write-Host "Port $port is unavailable for frontend, trying next port..." -ForegroundColor Yellow
            continue
        }

        Write-Host "Starting frontend on http://localhost:$port" -ForegroundColor Green
        $webCommand = if ($Dev) { "npm run dev -- --hostname 127.0.0.1 --port $port" } else { "npm run start -- --hostname 127.0.0.1 --port $port" }
        $frontendProcess = Start-Process -FilePath 'cmd.exe' `
            -ArgumentList @('/c', $webCommand) `
            -WorkingDirectory $webRoot `
            -PassThru

        Start-Sleep -Seconds 5
        $frontendProcess.Refresh()
        if (-not $frontendProcess.HasExited) {
            return @{ Process = $frontendProcess; Port = $port }
        }

        Write-Host "Frontend port $port failed with exit code $($frontendProcess.ExitCode). Trying next port..." -ForegroundColor Yellow
        Stop-ChildProcess -Process $frontendProcess
    }

    throw "Frontend could not start on any port between $BasePort and $MaxPort."
}

Write-Host "AIOC base: $root" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "Creating Python virtual environment..." -ForegroundColor Yellow
    python -m venv (Join-Path $root "venv")
}

if (-not $SkipInstall) {
    Write-Host "Checking Python dependencies..." -ForegroundColor Yellow
    & $python -m pip install -r (Join-Path $root "requirements.txt")

    if (-not (Test-Path -LiteralPath (Join-Path $webRoot "node_modules"))) {
        Write-Host "Installing web dependencies..." -ForegroundColor Yellow
        Push-Location $webRoot
        try {
            npm ci
        } finally {
            Pop-Location
        }
    }
}

if (-not $Dev) {
    $nextBuild = Join-Path $webRoot ".next"
    if ($Rebuild -or -not (Test-Path -LiteralPath $nextBuild)) {
        Write-Host "Building production web app..." -ForegroundColor Yellow
        Push-Location $webRoot
        try {
            $env:NODE_OPTIONS = "--max-old-space-size=768"
            npm run build
        } finally {
            Pop-Location
        }
    }
}

if (-not (Test-PortAvailable -Port $ApiPort)) {
    Write-Host "Port $ApiPort is unavailable; searching for a free backend port..." -ForegroundColor Yellow
    $ApiPort = Get-FreePort -StartPort 8000 -MaxPort 8100
    Write-Host "Selected free backend port $ApiPort" -ForegroundColor Yellow
}
if (-not (Test-PortAvailable -Port $WebPort)) {
    Write-Host "Port $WebPort is unavailable; searching for a free frontend port..." -ForegroundColor Yellow
    $WebPort = Get-FreePort -StartPort 3000 -MaxPort 3100
    Write-Host "Selected free frontend port $WebPort" -ForegroundColor Yellow
}

Write-Host "Starting backend on http://localhost:$ApiPort" -ForegroundColor Green
$backend = Start-Process -FilePath $python `
    -ArgumentList @('-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', $ApiPort) `
    -WorkingDirectory $root `
    -PassThru

Write-Host "Setting NEXT_PUBLIC_API_BASE for frontend to http://127.0.0.1:$ApiPort" -ForegroundColor Cyan
$env:NEXT_PUBLIC_API_BASE = "http://127.0.0.1:$ApiPort"

$frontendResult = Start-Frontend -BasePort $WebPort -MaxPort 3100
$frontend = $frontendResult.Process
$WebPort = $frontendResult.Port

Write-Host ""
Write-Host "AI Operations Command Center is running." -ForegroundColor Cyan
Write-Host "Frontend: http://localhost:$WebPort"
Write-Host "Backend:  http://localhost:$ApiPort"
Write-Host "Health:   http://localhost:$ApiPort/api/health"
Write-Host "Press Ctrl+C or close this terminal to stop both services."

try {
    while ($true) {
        if ($backend.HasExited) {
            throw "Backend stopped with exit code $($backend.ExitCode)."
        }
        if ($frontend.HasExited) {
            throw "Frontend stopped with exit code $($frontend.ExitCode)."
        }
        Start-Sleep -Seconds 2
        $backend.Refresh()
        $frontend.Refresh()
    }
} finally {
    Stop-ChildProcess -Process $backend
    Stop-ChildProcess -Process $frontend
}
