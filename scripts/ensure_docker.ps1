param([Parameter(Mandatory=$true)][string]$Python, [int]$TimeoutSeconds = 90)
$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot
function Test-Engine {
    $Check = & $Python -X utf8 -m src.competition_web.docker_health
    return $LASTEXITCODE -eq 0
}
Push-Location $ProjectDir
try {
    if (Test-Engine) { Write-Output 'Docker Linux engine ready.'; exit 0 }
    $DockerRoot = Join-Path $env:ProgramFiles 'Docker\Docker'
    $Desktop = Join-Path $DockerRoot 'Docker Desktop.exe'
    if (-not (Test-Path -LiteralPath $Desktop)) { throw '请先安装并启动Docker Desktop的Linux引擎，然后重新打开启动有数。' }
    $Processes = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('Docker Desktop.exe','com.docker.backend.exe')
    })
    if (-not $Processes.Count) {
        # Windows may leave unopenable Unix socket reparse points after reboot.
        # Preserve only the known temporary socket directories, never Docker data.
        $Directories = @{
            'Docker\run' = @('dockerEthernetVfkit','dockerInference','sailor-ingest.sock','userAnalyticsOtlpHttp.sock')
            'docker-secrets-engine' = @('engine.sock')
        }
        foreach ($Relative in $Directories.Keys) {
            $SocketDir = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA $Relative))
            $Expected = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA $Relative))
            if ($SocketDir -ne $Expected -or -not $SocketDir.StartsWith([IO.Path]::GetFullPath($env:LOCALAPPDATA)+'\')) { throw 'Socket path outside local application directory.' }
            if (-not (Test-Path -LiteralPath $SocketDir)) { continue }
            $Folder = Get-Item -LiteralPath $SocketDir -Force
            $Parent = Get-Item -LiteralPath (Split-Path -Parent $SocketDir) -Force
            if (($Folder.Attributes -band [IO.FileAttributes]::ReparsePoint) -or ($Parent.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Unexpected socket directory redirection; no changes made.' }
            $Entries = @(Get-ChildItem -LiteralPath $SocketDir -Force)
            if (-not $Entries.Count) { continue }
            $Unknown = @($Entries | Where-Object { $_.Name -notin $Directories[$Relative] -or $_.PSIsContainer -or -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) })
            if ($Unknown.Count) { throw 'Docker临时目录含未知内容，已保留原件。请先手动启动Docker Desktop。' }
            $Backup = $SocketDir+'.before-start-'+[guid]::NewGuid().ToString('N')
            Rename-Item -LiteralPath $SocketDir -NewName (Split-Path -Leaf $Backup)
            Write-Output ('已保留上次运行的临时通信文件：'+$Backup)
        }
        Start-Process -FilePath $Desktop -WindowStyle Hidden
    }
    Write-Output '正在等待Docker引擎启动，尚未调用模型…'
    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $Deadline) {
        if (Test-Engine) { Write-Output 'Docker Linux engine ready.'; exit 0 }
        Start-Sleep -Seconds 2
    }
    throw 'Docker引擎未就绪，未启动AI或创建新时段。请查看Docker Desktop的错误提示后重试；不要重置银行数据。'
} finally { Pop-Location }
