<#
run_hub.ps1 - start the CabiNet hub on this Windows PC. LITE MODE ONLY.
PowerShell twin of run_hub.cmd (same hub.env, same flags pass-through):

    .\deploy\windows\run_hub.ps1
    .\deploy\windows\run_hub.ps1 --no-seed

Why lite only, the firewall rule and the router recipe: deploy\WINDOWS_HUB.md
#>
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $here '..\..')

$envFile = Join-Path $here 'hub.env'
$cfg = @{}
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { $cfg[$matches[1]] = $matches[2] }
    }
}
if (-not $cfg['HUB_IP']) {
    Write-Host "run_hub: HUB_IP is not set."
    Write-Host "  copy deploy\windows\hub.env.example to deploy\windows\hub.env and put"
    Write-Host "  this PC's LAN address in it (the one you reserved on your router)."
    exit 2
}

function Test-Py($exe, $extra) {
    try { & $exe @extra -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
}
$py = $null; $pyArgs = @()
if ($cfg['CABINET_PYTHON'] -and (Test-Path $cfg['CABINET_PYTHON']) -and (Test-Py $cfg['CABINET_PYTHON'] @())) { $py = $cfg['CABINET_PYTHON'] }
elseif (Test-Py 'py' @('-3'))  { $py = 'py'; $pyArgs = @('-3') }
elseif (Test-Py 'python' @())   { $py = 'python' }
if (-not $py) {
    Write-Host "run_hub: no Python 3.11+ found. Install it from python.org (tick 'py launcher')"
    Write-Host "  or set CABINET_PYTHON=<full path to python.exe> in deploy\windows\hub.env."
    exit 2
}

# the hub logs emoji; a cp1252 console would otherwise crash the logger
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$hubIp = $cfg['HUB_IP']
Write-Host "CabiNet hub (lite) on http://${hubIp}:8081  -  Python: $py $pyArgs"
Write-Host "  first time only, in an ADMIN prompt, so the machines can reach it:"
Write-Host '  netsh advfirewall firewall add rule name="CabiNet hub" dir=in action=allow protocol=TCP localport=8081'
Write-Host "  stop with Ctrl+C."
Write-Host ""
Set-Location (Join-Path $root 'G2S')
& $py @pyArgs -u g2s_host.py --harvest --host-base "http://${hubIp}:8081" --log-dir logs @args
exit $LASTEXITCODE
