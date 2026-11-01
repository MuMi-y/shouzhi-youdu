param(
    [ValidateSet('Start','Stop','Status')][string]$Action = 'Start',
    [switch]$NoModel, [switch]$WithoutHistory, [switch]$NewAISession,
    [switch]$Interactive, [switch]$OpenBrowser, [switch]$NoOpenBrowser,
    [ValidateRange(1,65535)][int]$Port = 8773,
    [string]$DataDir = '', [string]$Python = '', [string]$BatchBudget = ''
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProjectDir = Split-Path -Parent $PSScriptRoot
if (-not $DataDir) { $DataDir = Join-Path $ProjectDir 'runtime\competition_v1' }
$RuntimeDir = [System.IO.Path]::GetFullPath($DataDir)
$Launcher = Join-Path $PSScriptRoot 'start_competition.ps1'
if (-not $Python) {
    foreach ($Candidate in @('.venv-competition\Scripts\python.exe','.venv-web\Scripts\python.exe')) {
        $CandidatePath = Join-Path $ProjectDir $Candidate
        if (Test-Path -LiteralPath $CandidatePath) { $Python = $CandidatePath; break }
    }
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python)) { throw 'Python environment missing; install the pinned requirements first.' }
if ($Action -ne 'Start') { & $Launcher -Action $Action -DataDir $RuntimeDir -Python $Python; exit $LASTEXITCODE }
if ($NewAISession -and ($NoModel -or $BatchBudget)) { throw 'NewAISession cannot replace an explicit diagnostic budget or NoModel mode.' }
if (-not $NoModel) {
    & node (Join-Path $PSScriptRoot 'check_dsh_runtime.cjs')
    if ($LASTEXITCODE -ne 0) { throw 'AI dependency/configuration preflight failed; no session was created.' }
    & (Join-Path $PSScriptRoot 'ensure_docker.ps1') -Python $Python
    if ($LASTEXITCODE -ne 0) { throw 'Docker startup failed; no AI session was created.' }
}
$RecordPath = Join-Path $RuntimeDir 'launcher-process.json'
if (Test-Path -LiteralPath $RecordPath) {
    $Status = & $Launcher -Action Status -DataDir $RuntimeDir -Python $Python
    if ($Status.status -eq 'RUNNING') {
        if ($NewAISession) { throw 'Stop the verified product process before starting a new AI session.' }
        if ($BatchBudget) { throw 'Product already running; stop it before applying an explicit budget.' }
        $RunningState = Invoke-RestMethod "$($Status.url)/api/state" -TimeoutSec 5
        $ExpectedMode = if ($NoModel) { 'NO_MODEL' } else { 'MODEL' }
        if (-not $RunningState.model.managed -or $RunningState.model.mode -ne $ExpectedMode -or
            -not $RunningState.model.capabilities.goals -or -not $RunningState.model.capabilities.goal_followup -or
            ([bool]$RunningState.history_available -eq [bool]$WithoutHistory)) {
            throw 'Running configuration differs. Stop the verified product before changing mode/history/capabilities.'
        }
        Write-Output "Already running: $($Status.url)"
        if (-not $NoModel -and -not $RunningState.model.ready_for_requests) {
            Write-Host '网页仍可查看，但当前AI尚未就绪；不要把已有进程当作AI已恢复。'
            Write-Host '若上一AI时段已结束：先双击停止有数体验版，再双击启动有数体验版，按提示选择开启新时段或仅使用工具。'
            Write-Host '本次不会自动续开AI时段，也不会清除原来的记录。'
        }
        if ($OpenBrowser -and -not $NoOpenBrowser) { Start-Process $Status.url }
        exit 0
    }
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw 'Port occupied; no AI session was created.' }
if (-not $NoModel -and -not $BatchBudget) {
    $BudgetArgs = @('-X','utf8',(Join-Path $PSScriptRoot 'product_budget.py'),'--runtime',$RuntimeDir)
    if ($NewAISession) { $BudgetArgs += '--new-session' }
    $SelectionText = & $Python @BudgetArgs
    $SelectionCode = $LASTEXITCODE
    $Selection = $SelectionText | ConvertFrom-Json
    if ($SelectionCode -eq 3 -and $Interactive) {
        Write-Host '上一AI使用时段已结束。新时段最多60分钟、8次AI运行（含后台检查）、32次模型请求。重启不会重置。'
        $Choice = Read-Host '输入 1 开启新时段；输入 2 暂不启用AI，使用银行和计算功能'
        if ($Choice -eq '1') {
            $Selection = (& $Python @BudgetArgs --new-session) | ConvertFrom-Json
            $SelectionCode = $LASTEXITCODE
        } elseif ($Choice -eq '2') { $NoModel = $true; $SelectionCode = 0 }
        else { throw '未选择，本次未启动或创建新时段。' }
    }
    if ($SelectionCode -ne 0) { throw $Selection.message }
    if (-not $NoModel) { $BatchBudget = $Selection.path }
}
$Options = @{DataDir=$RuntimeDir; Python=$Python; Port=$Port; EnableGoals=$true; GoalFollowup=$true}
if ($WithoutHistory) { $Options.WithoutHistory = $true }
if ($NoModel) { $Options.NoModel = $true }
else { $Options.GoalAI = $true; $Options.BatchBudget = $BatchBudget }
& $Launcher @Options
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "Product ready: http://127.0.0.1:$Port"
if ($OpenBrowser -and -not $NoOpenBrowser) { Start-Process "http://127.0.0.1:$Port" }
