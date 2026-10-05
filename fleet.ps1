# Connect all nine planets to the live server in one command.
#
#   .\fleet.ps1 check    # confirm all nine keys (joins and leaves, no trades) ~5 s
#   .\fleet.ps1          # trade with all nine until the run ends; opens the status grid
#   .\fleet.ps1 stop     # kill any client still running in the container
#
# Keys and endpoint come from my-credentials.json (git-ignored). Ctrl+C stops all nine.
param(
    [ValidateSet("trade", "check", "stop")] [string]$Mode = "trade"
)
$ErrorActionPreference = "Continue"  # docker writes progress to stderr; check exit codes instead
Set-Location $PSScriptRoot

# Ctrl+C only stops the docker CLI; the clients inside the container must be
# killed too, or a later launch would put two clients on the same key.
function Stop-Fleet {
    $kill = "import os,signal`nfor p in os.listdir('/proc'):`n if p.isdigit() and int(p)!=os.getpid():`n  try:`n   c=open(f'/proc/{p}/cmdline','rb').read()`n   if b'bazaar_client.cli' in c or b'scripts/fleet.py' in c: os.kill(int(p),signal.SIGTERM)`n  except OSError: pass"
    docker compose exec -T bazaar python -c $kill *> $null
}

if ($Mode -eq "stop") { Stop-Fleet; Write-Host "all clients stopped"; exit 0 }

if (-not (Test-Path "my-credentials.json")) {
    throw "my-credentials.json is missing (it holds the nine keys and the endpoint)."
}

# Docker Desktop must be running before compose can do anything.
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "starting Docker Desktop..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep 2
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
    }
    if ($LASTEXITCODE -ne 0) { throw "Docker did not start" }
}

docker compose up -d *> $null
docker compose exec -T bazaar test -f bazaar_pb2.py
if ($LASTEXITCODE -ne 0) { docker compose exec -T bazaar scripts/gen_proto.sh }

if ($Mode -eq "check") {
    docker compose exec -T bazaar python scripts/fleet.py check
    exit $LASTEXITCODE
}

$out = "logs/live/fleet-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
# Open the nine-planet status grid as soon as the launcher writes it.
Start-Job -ArgumentList (Join-Path $PSScriptRoot "$out/fleet.html") -ScriptBlock {
    param($page)
    for ($i = 0; $i -lt 60 -and -not (Test-Path $page); $i++) { Start-Sleep -Milliseconds 500 }
    if (Test-Path $page) { Start-Process $page }
} | Out-Null

Stop-Fleet  # never two clients on one key
try {
    docker compose exec -T bazaar python scripts/fleet.py trade --out $out
} finally {
    Stop-Fleet
}
