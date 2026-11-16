# 枢阁 · 可执行文件构建脚本（bin\core-host.exe 宿主 + bin\core.exe 内核）
#
# 为什么两个都要 -H windowsgui（2026-09-14 排查「黑窗口一直闪」的结论，别删这段）：
#   计划任务 core-watchdog 每 2 分钟用 Interactive 令牌拉起 bin\core-host.exe。
#   该 exe 若编译成 CONSOLE 子系统，Windows 每次都给它**新建一个控制台**（conhost + Windows
#   Terminal 的 OpenConsole），而它一发现「启停台已在跑」就 return 秒退 → 控制台随之销毁，
#   用户看到的就是一闪而过的黑窗。
#   编译成 GUI 子系统 = 本程序完全不分配控制台，从根上没有窗口（源码注释见 cmd\core-host\main.go 顶部）。
#   实测（毫秒级进程轮询）：
#     旧 CONSOLE 件：每触发一次 → core-host.exe + conhost + OpenConsole（闪窗）
#     新 GUI   件：每触发一次 → 只有 core-host.exe，无任何控制台宿主（不闪）
#
#   core.exe 目前靠「宿主用 DETACHED_PROCESS + HideWindow 拉起」躲过闪窗（见 cmd\core-host\proc 里的 spawn），
#   但只要有人直接双击 / 写个任务 / 脚本里裸 exec 它，CONSOLE 子系统就会闪。两个产物保持同一种身份，
#   才不会重演"以为 flag 上了、其实没上"。core 的日志本来就文件优先（data/logs/core.log），
#   插件输出走 supervisor 的 sink，都不依赖控制台 —— 所以这个 flag 对 core 也是零代价。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_exe.ps1                    # 只构建+校验（三个都编）
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Target host       # 只编宿主
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Swap              # 构建并热替换（停宿主→换→计划任务拉回；内核走启停台 stop/start）
param(
    [string]$CoreDir = (Split-Path -Parent $PSScriptRoot),
    [ValidateSet('all', 'host', 'core', 'launch')][string]$Target = 'all',
    [switch]$Swap
)

$ErrorActionPreference = 'Stop'

$go = 'C:\Program Files\Go\bin\go.exe'
if (-not (Test-Path $go)) { $go = (Get-Command go -ErrorAction Stop).Source }

$bin = Join-Path $CoreDir 'bin'
# 与原构建一致：CGO_ENABLED=0（见 go version -m 的 build 信息）
$env:CGO_ENABLED = '0'

# 子系统号：2=GUI（不分配控制台）  3=CONSOLE（会闪黑窗）
function Get-Subsystem([string]$p) {
    $b = [System.IO.File]::ReadAllBytes($p)
    $peOff = [BitConverter]::ToInt32($b, 0x3C)
    return [BitConverter]::ToUInt16($b, $peOff + 0x5C)
}

$plan = @()
if ($Target -eq 'all' -or $Target -eq 'host') {
    $plan += [pscustomobject]@{ Name = 'host'; Pkg = '.\cmd\core-host'; Final = 'core-host.exe' }
}
if ($Target -eq 'all' -or $Target -eq 'core') {
    $plan += [pscustomobject]@{ Name = 'core'; Pkg = '.\cmd\core'; Final = 'core.exe' }
}
if ($Target -eq 'all' -or $Target -eq 'launch') {
    # 包根目录那个显眼的启动器（辞玖 2026-10-07：快捷方式不可靠，换成真 exe）
    $plan += [pscustomobject]@{ Name = 'launch'; Pkg = '.\cmd\core-launch'; Final = 'launch.exe' }
}

# ── 构建 + 校验（校验不过就丢弃产物，绝不换入）──────────────
Push-Location $CoreDir
try {
    foreach ($p in $plan) {
        $staged = Join-Path $bin ($p.Name + '.new.exe')
        Write-Host "[build] $go build -ldflags '-H windowsgui' -o $($p.Name).new.exe $($p.Pkg)"
        & $go build -ldflags '-H windowsgui' -o $staged $p.Pkg
        if ($LASTEXITCODE -ne 0) { throw "构建失败（$($p.Name)），退出码 $LASTEXITCODE" }
        $sub = Get-Subsystem $staged
        if ($sub -ne 2) {
            Remove-Item $staged -Force -ErrorAction SilentlyContinue
            throw "子系统校验失败（$($p.Name)）：得到 $sub（2=GUI 才对）。直接拉起它会闪黑窗，已丢弃该产物。"
        }
        Write-Host "[ok] $($p.Name) 子系统 = GUI(2)，不会分配控制台"
    }
} finally {
    Pop-Location
}

if (-not $Swap) {
    Write-Host "[done] 产物：$(($plan | ForEach-Object { $_.Name + '.new.exe' }) -join ', ')（在 $bin）"
    Write-Host "       要换到现役位置请加 -Swap"
    return
}

# ── 热替换 ────────────────────────────────────────────────
function Wait-Http([string]$url, [int]$tries = 24) {
    for ($i = 0; $i -lt $tries; $i++) {
        Start-Sleep -Milliseconds 500
        try { return Invoke-RestMethod $url -TimeoutSec 2 } catch { }
    }
    return $null
}

foreach ($p in $plan) {
    $staged = Join-Path $bin ($p.Name + '.new.exe')
    $final = Join-Path $bin $p.Final

    if ($p.Name -eq 'host') {
        # 宿主占着文件：先停它，换完用计划任务拉回来（内核不重启）
        $old = Get-Process core-host -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($old) { Write-Host "[swap] 停止旧宿主 pid=$($old.Id)"; Stop-Process -Id $old.Id -Force; Start-Sleep -Seconds 2 }
        Move-Item $staged $final -Force
        Write-Host "[swap] 已换入 $final"
        Start-ScheduledTask -TaskName 'core-watchdog'
        $r = Wait-Http 'http://127.0.0.1:8790/healthz'
        if ($r -and $r.ok) { Write-Host "[ok] 启停台已回来：pid=$($r.pid) role=$($r.role)" }
        else { Write-Warning "12s 内 8790 未就绪，查看 data\logs\host.log" }
    } else {
        # 内核：走启停台的正规通道停止，换完再启动（不直接杀进程，保证账本收尾）
        Write-Host "[swap] 停止内核（经启停台 API）"
        try { Invoke-RestMethod 'http://127.0.0.1:8790/api/core/stop' -Method POST -TimeoutSec 30 | Out-Null } catch { }
        # ⚠️ 2026-10-06 实测：内核**优雅停机要 ~90 秒**（插件一个个收：06:37:57 收到停止 →
        #    06:39:30 才"平台已停止"）。原来只等 15 秒 → 进程还在、文件被占用 → Move-Item 报
        #    "Cannot create a file when that file already exists"，**而且脚本中途异常退出、
        #    内核没被拉回来**（人工收的尾）。所以：等到 120 秒，且**没退干净就报错退出**。
        $gone = $false
        for ($i = 0; $i -lt 240; $i++) {
            Start-Sleep -Milliseconds 500
            if (-not (Get-Process core -ErrorAction SilentlyContinue)) { $gone = $true; break }
        }
        if (-not $gone) {
            throw "内核 120 秒还没退干净（core.exe 仍在）—— 不换文件（换了也是占用失败）。先看 data\logs\core.log"
        }
        Move-Item $staged $final -Force
        Write-Host "[swap] 已换入 $final"
        try { Invoke-RestMethod 'http://127.0.0.1:8790/api/core/start' -Method POST -TimeoutSec 30 | Out-Null } catch { }
        # 换完新二进制起来本来就要 15~25 秒（原来只等 12s，正常情况也会报"未就绪"）
        $s = Wait-Http 'http://127.0.0.1:8795/healthz' 120
        if ($s) { Write-Host "[ok] 内核已回来：$($s | ConvertTo-Json -Compress)" }
        else { Write-Warning "60s 内内核未就绪，查看 data\logs\core.log" }
    }
}
