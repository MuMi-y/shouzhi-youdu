param(
    [ValidateSet('Start','Stop','Status')][string]$Action = 'Start',
    [switch]$Foreground, [switch]$NoModel, [switch]$WithoutHistory,
    [switch]$EnableGoals, [switch]$GoalFollowup, [switch]$GoalAI, [string]$BatchBudget = '',
    [ValidateRange(1,65535)][int]$Port = 8773, [string]$DataDir = '', [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot
if (-not $DataDir) { $DataDir = Join-Path $ProjectDir 'runtime\competition_v1' }
$RuntimeDir = [System.IO.Path]::GetFullPath($DataDir)
if (-not $Python) {
    $Python = Join-Path $ProjectDir '.venv-competition\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python)) { $Python = Join-Path $ProjectDir '.venv-web\Scripts\python.exe' }
}
$EntryPath = Join-Path $PSScriptRoot 'competition_web.py'
$ProcessRecord = Join-Path $RuntimeDir 'launcher-process.json'
$DemoUrl = "http://127.0.0.1:$Port"
function Get-ServiceProcess([int]$ProcessId) {
    return Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"
}
function Test-ServiceCommand($Process, [int]$ServicePort) {
    if (-not $Process) { return $false }
    $EntryPattern = '(?:^|\s)(?:"' + [regex]::Escape($EntryPath) + '"|' + [regex]::Escape($EntryPath) + ')(?=\s|$)'
    $RuntimePattern = '(?:^|\s)--data-dir\s+(?:"' + [regex]::Escape($RuntimeDir) + '"|' + [regex]::Escape($RuntimeDir) + ')(?=\s|$)'
    return $Process.CommandLine -match $EntryPattern -and $Process.CommandLine -match $RuntimePattern -and $Process.CommandLine -match ('(?:^|\s)--port\s+' + $ServicePort + '(?=\s|$)')
}
function Get-Identity($Process) {
    return @{pid=[int]$Process.ProcessId;parent_pid=[int]$Process.ParentProcessId;executable=$Process.ExecutablePath;
        start_ticks=(Get-Process -Id $Process.ProcessId).StartTime.ToUniversalTime().Ticks.ToString()}
}
function Assert-Identity($Identity, [int]$ServicePort) {
    $Actual = Get-ServiceProcess ([int]$Identity.pid)
    if (-not $Actual) { return $null }
    if (-not (Test-ServiceCommand $Actual $ServicePort) -or $Actual.ExecutablePath -ne $Identity.executable -or
        $Actual.ParentProcessId -ne $Identity.parent_pid -or
        (Get-Process -Id $Actual.ProcessId).StartTime.ToUniversalTime().Ticks.ToString() -ne [string]$Identity.start_ticks) {
        throw 'Process identity/start time/parent chain changed; refusing this PID.'
    }
    return $Actual
}
function Get-Listeners([int]$ServicePort) {
    return @(Get-NetTCPConnection -LocalPort $ServicePort -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
}
function Get-OwnedChain([int]$OwnerId, $Launcher, [int]$ServicePort) {
    $Chain = @(); $NextId = $OwnerId
    for ($Depth=0; $Depth -lt 8; $Depth++) {
        $Actual = Get-ServiceProcess $NextId
        if (-not (Test-ServiceCommand $Actual $ServicePort)) { throw 'Listener is not the expected source/runtime process.' }
        $Chain += Get-Identity $Actual
        if ($NextId -eq [int]$Launcher.pid) { $null=Assert-Identity $Launcher $ServicePort; return $Chain }
        $NextId = [int]$Actual.ParentProcessId
    }
    throw 'Listener is not a verified descendant of this launcher.'
}
function Assert-Stopped([int]$ServicePort, $Identities) {
    foreach ($Identity in $Identities) {
        if (Assert-Identity $Identity $ServicePort) { throw 'Recorded process still running; backup is not ready.' }
    }
    if ((Get-Listeners $ServicePort).Count) { throw 'Port is still listening; backup is not ready.' }
    $Remaining = @(Get-CimInstance Win32_Process | Where-Object { Test-ServiceCommand $_ $ServicePort })
    if ($Remaining.Count) { throw 'Matching source/runtime process remains; backup is not ready.' }
}
if ($Action -in @('Status','Stop')) {
    if (-not (Test-Path -LiteralPath $ProcessRecord)) { throw 'No launcher process record; inspect the actual process before stopping.' }
    $Record = Get-Content -LiteralPath $ProcessRecord -Raw -Encoding utf8 | ConvertFrom-Json
    if ([System.IO.Path]::GetFullPath($Record.runtime) -ne $RuntimeDir) { throw 'Record runtime mismatch.' }
    if ($Record.version -eq 2) {
        if ($Record.entry -ne $EntryPath -or [int]$Record.port -lt 1 -or [int]$Record.port -gt 65535) { throw 'Record entry/port mismatch.' }
        $ServicePort = [int]$Record.port
        $Identities = @($Record.processes)
        if (-not $Identities.Count) { throw 'Process identity chain missing.' }
    } else {
        $LegacyUri = [uri]$Record.url
        if ($LegacyUri.Scheme -ne 'http' -or $LegacyUri.Host -ne '127.0.0.1') { throw 'Invalid legacy local URL.' }
        $ServicePort = $LegacyUri.Port
        $Legacy = Get-ServiceProcess ([int]$Record.pid)
        if (-not $Legacy) { throw 'Legacy launcher absent; inspect child ownership before backup.' }
        $RecordedStart = if ($Record.started_at -is [datetime]) { $Record.started_at } else { [datetime]::Parse($Record.started_at,[System.Globalization.CultureInfo]::InvariantCulture,[System.Globalization.DateTimeStyles]::RoundtripKind) }
        if (-not (Test-ServiceCommand $Legacy $ServicePort) -or (Get-Process -Id $Legacy.ProcessId).StartTime.ToUniversalTime().Ticks -ne $RecordedStart.ToUniversalTime().Ticks) { throw 'Legacy process identity mismatch.' }
        $Listeners = @(Get-Listeners $ServicePort)
        if ($Listeners.Count -ne 1) { throw 'Legacy listener ownership is ambiguous.' }
        $Identities = @(Get-OwnedChain $Listeners[0] (Get-Identity $Legacy) $ServicePort)
    }
    $DemoUrl = "http://127.0.0.1:$ServicePort"
    $Alive = @($Identities | ForEach-Object { Assert-Identity $_ $ServicePort } | Where-Object { $null -ne $_ })
    $Listeners = @(Get-Listeners $ServicePort)
    if (-not $Alive.Count) {
        Assert-Stopped $ServicePort $Identities
        Write-Output 'Stopped (verified: no recorded process, listener or matching runtime process).';exit 0
    }
    if ($Listeners.Count -ne 1 -or $Listeners[0] -notin @($Identities | ForEach-Object { [int]$_.pid })) { throw 'Listener does not match recorded process chain.' }
    if ($Action -eq 'Status') {
        $Health = Invoke-RestMethod "$DemoUrl/api/health" -TimeoutSec 3
        [pscustomobject]@{status='RUNNING';url=$DemoUrl;listener_pid=$Listeners[0];verified_pids=@($Alive.ProcessId);health=$Health};exit 0
    }
    $State = Invoke-RestMethod "$DemoUrl/api/state" -TimeoutSec 5
    if (@($State.jobs | Where-Object { $_.status -eq 'RUNNING' -or
        ($_.status -eq 'QUEUED' -and (-not $State.model.managed -or $State.model.worker_running)) }).Count) {
        throw 'A model job is active. Wait for it to finish before stopping and backing up.'
    }
    # Recorded leaf-to-launcher order handles Windows venv redirectors explicitly.
    foreach ($Identity in $Identities) {
        if (Assert-Identity $Identity $ServicePort) {
            Stop-Process -Id ([int]$Identity.pid)
            Wait-Process -Id ([int]$Identity.pid) -Timeout 15 -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 300
    Assert-Stopped $ServicePort $Identities
    Write-Output 'Stopped and verified: no Python owner or listener remains. Full runtime backup is safe.'
    exit 0
}
if (-not (Test-Path -LiteralPath $Python)) { throw 'Create .venv-competition and install requirements-competition.lock.txt first, or specify -Python.' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw 'Port is occupied; use another port or verify/stop its owner.' }
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
$Options = @('-X','utf8',('"'+$EntryPath+'"'),'--port',"$Port",'--data-dir',('"'+$RuntimeDir+'"'))
if ($NoModel) { $Options += '--no-model' }
if ($WithoutHistory) { $Options += '--without-history' }
if ($EnableGoals) { $Options += '--enable-goals' }
if ($GoalFollowup) { $Options += '--goal-followup' }
if ($GoalAI) { $Options += '--goal-ai' }
if ($BatchBudget) { $Options += @('--batch-budget',('"'+[System.IO.Path]::GetFullPath($BatchBudget)+'"')) }
if ($Foreground) {
    $ForegroundOptions = @('-X','utf8',$EntryPath,'--port',"$Port",'--data-dir',$RuntimeDir)
    if ($NoModel) { $ForegroundOptions += '--no-model' }
    if ($WithoutHistory) { $ForegroundOptions += '--without-history' }
    if ($EnableGoals) { $ForegroundOptions += '--enable-goals' }
    if ($GoalFollowup) { $ForegroundOptions += '--goal-followup' }
    if ($GoalAI) { $ForegroundOptions += '--goal-ai' }
    if ($BatchBudget) { $ForegroundOptions += @('--batch-budget',[System.IO.Path]::GetFullPath($BatchBudget)) }
    & $Python @ForegroundOptions
    exit $LASTEXITCODE
}
$ServiceProcess = Start-Process -FilePath $Python -ArgumentList $Options -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $RuntimeDir 'server.out.log') -RedirectStandardError (Join-Path $RuntimeDir 'server.err.log')
$Launcher = Get-Identity (Get-ServiceProcess $ServiceProcess.Id)
@{version=2;pid=$ServiceProcess.Id;port=$Port;entry=$EntryPath;url=$DemoUrl;runtime=$RuntimeDir;processes=@($Launcher)} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ProcessRecord -Encoding utf8
for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
    Start-Sleep -Milliseconds 300
    if ($ServiceProcess.HasExited) { throw 'Service exited. Inspect server.err.log in the specified runtime.' }
    try { $Health = Invoke-RestMethod "$DemoUrl/api/health" -TimeoutSec 1 } catch { continue }
    if ($Health.ok) {
        $Listeners = @(Get-Listeners $Port)
        if ($Listeners.Count -ne 1) { throw 'Ready port ownership is ambiguous.' }
        $Chain = @(Get-OwnedChain $Listeners[0] $Launcher $Port)
        @{version=2;pid=$ServiceProcess.Id;port=$Port;entry=$EntryPath;url=$DemoUrl;runtime=$RuntimeDir;processes=$Chain} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ProcessRecord -Encoding utf8
        Write-Output "Ready: $DemoUrl (verified listener PID $($Listeners[0]))";exit 0
    }
}
throw 'Service did not become ready; inspect its logs before retrying.'
