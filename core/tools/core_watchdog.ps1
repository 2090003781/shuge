# 枢阁平台 · core 看门狗（L0 外部锚点）
#
# 为什么需要它（2026-09-11 定案）：
#   平台账本/总线/插件都在 core 进程内，core 一死，平台内**没有任何东西**能救它
#   （总线是内存里的一坨，观察者、被观察者、裁判全在同一个进程里）。
#   所以"谁监督总线"的答案只能是**平台之外**的实体：Windows 计划任务 + 本脚本。
#
# 职责（只做一件事，越简单越可靠）：
#   探测 /healthz → 不在则拉起 → 记录状态跃迁（只在变化时写日志，不产噪音）
#
# ⚠️ 2026-10-06 辞玖：「打开顺序固定好，先主程序然后是库最后启动内核」——
#   原来这里探到内核不在就直接 `core.exe`，**绕过了"库是主体"那条链**（库还没起就把内核拉起来）。
#   现在分两级：
#     ① 主程序（core-host，8790）不在  → 拉起主程序（它自己会按 库 → 内核 的顺序保起来）
#     ② 主程序在、内核不在            → 让主程序按序拉内核（POST /api/core/start），本脚本不碰 core.exe
#
# 用法：
#   手动： powershell -NoProfile -ExecutionPolicy Bypass -File tools\core_watchdog.ps1
#   计划任务： schtasks /Create /TN "core-watchdog" /SC MINUTE /MO 2 /TR "..."
#   （由 tools\install_watchdog_task.ps1 一键注册；开机/登录各挂一次）
#
# 兼容 Windows PowerShell 5.1（不依赖 pwsh 7）。
param(
    # 默认平台根 = 本脚本所在目录的上一层（不再写死盘符目录；2026-10-06）
    [string]$CoreDir = (Split-Path -Parent $PSScriptRoot),
    [string]$HealthUrl = "http://127.0.0.1:8795/healthz",  # 内核内部端口（对外唯一地址是主进程 8790）
    [int]$StartupWaitSec = 12,
    [int]$ProbeTimeoutSec = 3
)

$ErrorActionPreference = "SilentlyContinue"
$logDir = Join-Path $CoreDir "data\logs"
$logFile = Join-Path $logDir "watchdog.log"
$stateFile = Join-Path $logDir "watchdog.state"
$coreExe = Join-Path $CoreDir "bin\core.exe"

# 每次运行都盖一个时间戳：用来确认"计划任务确实按周期触发了"（不写日志，避免噪音）
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
Set-Content -Path (Join-Path $logDir "watchdog.last") -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding UTF8

function Write-Log([string]$msg) {
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
    $line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Write-State([string]$state) {
    $prev = ""
    if (Test-Path $stateFile) { $prev = (Get-Content $stateFile -Raw).Trim() }
    if ($prev -ne $state) {
        Set-Content -Path $stateFile -Value $state -Encoding UTF8
        return $true   # 状态发生跃迁
    }
    return $false
}

function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec $ProbeTimeoutSec
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

# ── 主流程 ─────────────────────────────────────────────
if (Test-Health) {
    if (Write-State "healthy") { Write-Log "core 健康（首次确认或从异常恢复）" }
    exit 0
}

# 不健康：先保**主程序**（它负责按序保库与内核）；主程序在时让它按序拉内核，本脚本不直接碰 core.exe
$hostUrl = "http://127.0.0.1:8790/healthz"
$hostExe = Join-Path $CoreDir "bin\core-host.exe"
function Test-Host {
    try {
        $r = Invoke-WebRequest -Uri $hostUrl -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

if (-not (Test-Host)) {
    if (Write-State "host-down") { Write-Log "主程序不在 → 先拉主程序（它会按 库 → 内核 的顺序保起来）" }
    if (-not (Test-Path $hostExe)) {
        Write-Log "拉起失败：找不到 $hostExe"
        exit 2
    }
    Start-Process -FilePath $hostExe -WorkingDirectory $CoreDir -WindowStyle Hidden
} else {
    # 主程序在、内核不在 → 交给主程序按序拉（它会先 ensureNotes 再拉 core）
    if (Write-State "core-down") { Write-Log "主程序在、内核不在 → 让主程序按序拉内核（POST /api/core/start）" }
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8790/api/core/start" -Method POST -TimeoutSec 30 | Out-Null
    } catch {
        Write-Log ("让主程序拉内核失败：{0}" -f $_.Exception.Message)
    }
}
$deadline = (Get-Date).AddSeconds($StartupWaitSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (Test-Health) {
        if (Write-State "healthy") { Write-Log "core 已拉起并就绪（/healthz 200）" }
        exit 0
    }
}
Write-Log ("拉起后 {0}s 内 /healthz 仍不通，请查 data\logs\core.log" -f $StartupWaitSec)
exit 3
