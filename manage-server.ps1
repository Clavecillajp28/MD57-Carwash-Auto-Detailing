param([ValidateSet('start','restart','stop','status')][string]$Action='status')
$ErrorActionPreference='Stop'
$serverRoot=[IO.Path]::GetFullPath($PSScriptRoot)
$serverState=Join-Path $serverRoot '.runtime/server.json'
$serverInfo=$null
if (Test-Path -LiteralPath $serverState) { $serverInfo=Get-Content -LiteralPath $serverState -Raw | ConvertFrom-Json }
function Get-VerifiedServer {
    if (-not $serverInfo) { return $null }
    $process=Get-CimInstance Win32_Process -Filter "ProcessId = $($serverInfo.pid)"
    if (-not $process) { return $null }
    $expected=Join-Path $serverRoot '_Backend/runtime.py'
    if ($serverInfo.workspace -ne $serverRoot -or -not $process.CommandLine.Contains($expected)) {
        throw 'PID does not match this workspace runtime. Stop the old server manually.'
    }
    return $process
}
$server=Get-VerifiedServer
if ($Action -eq 'status') {
    if ($server) { $serverInfo | Format-List pid,host,port,version,started_at }
    else { Write-Output 'No managed MD57 server is running.' }
    exit
}
if ($Action -eq 'stop' -or $Action -eq 'restart') {
    if ($server) { $process=Get-Process -Id $server.ProcessId; Stop-Process -Id $server.ProcessId; if (-not $process.WaitForExit(10000)) { throw 'Server did not stop.' } }
    if ($Action -eq 'stop') { exit }
}
elseif ($server) { Write-Output 'MD57 is already running.'; exit }
$runtimeDir=Join-Path $serverRoot '.runtime'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$launcher=(Get-Command python -ErrorAction Stop).Source
$python=(& $launcher -c 'import sys; print(sys.executable)').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) { throw 'Cannot resolve the Python interpreter.' }
$runtime=Join-Path $serverRoot '_Backend/runtime.py'
$stamp=Get-Date -Format 'yyyyMMdd_HHmmss'
$started=Start-Process -FilePath $python -ArgumentList @('"'+$runtime+'"') -WorkingDirectory $serverRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir "server_$stamp.stdout.log") -RedirectStandardError (Join-Path $runtimeDir "server_$stamp.stderr.log") -PassThru
for ($attempt=0;$attempt -lt 40;$attempt++) {
    Start-Sleep -Milliseconds 250
    if ($started.HasExited) { throw "Startup failed. Read .runtime/server_$stamp.stderr.log" }
    if (Test-Path -LiteralPath $serverState) {
        $loaded=Get-Content -LiteralPath $serverState -Raw | ConvertFrom-Json
        if ($loaded.pid -eq $started.Id) { Write-Output "MD57 $($loaded.version) is running at http://$($loaded.host):$($loaded.port)"; exit }
    }
}
throw 'Startup has not completed. Check the .runtime logs before starting another instance.'
