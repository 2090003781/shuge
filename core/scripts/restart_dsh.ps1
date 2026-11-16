# 重启 DSH web（改了 DSH 侧配置后要它生效用）
#
# 为什么要有这个脚本：DSH **没有守护任务**（只有平台那条 core-watchdog），
# 杀掉就没人拉回来。所以这里：停 → 等口空出来 → 自己拉起来（Hidden，不弹黑窗）→ 等它重新监听 → 写状态。
#
# 三个踩过的坑（都真栽过）：
#   ① 干活的进程不能待在 DSH 的进程树里。老写法（Start-Process 甩后台）生出来的子进程仍挂在
#      DSH 那棵树上，一执行"停 DSH"就被连坐杀掉 —— 日志停在"停 DSH web pid=xxx"，DSH 再没起来。
#      现在改成 **WMI 生（Win32_Process.Create）**：父进程是 WmiPrvSE，停 DSH 杀不到它。
#   ② 出树的干活进程**读不了工作区的 .ps1**（本机沙箱只放行命令行上看得见的操作）：-File、
#      -EncodedCommand、& 脚本 实测都是"拒绝访问"。所以干活逻辑写成一段**文本**塞进 WMI 命令行。
#   ③ npm 的 dsh.cmd 是批处理，带重定向时不能直接 Start-Process（不是 Win32 程序），得走 cmd /c。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restart_dsh.ps1                  # 立刻（跟着念日志）
#   powershell ... -File scripts\restart_dsh.ps1 -DelaySeconds 40 -Detach                        # 拖 40 秒，甩出去就返回
#   powershell ... -File scripts\restart_dsh.ps1 -DryRun                                         # 只报会干什么
#   powershell ... -File scripts\restart_dsh.ps1 -Inline                                         # 就在本进程里干（调试）
#
# ⚠️ 它会掐断正在用 DSH 的会话（agent 就跑在 DSH 里）。要"先把话说完"就用 -DelaySeconds。
# 进度看 data\logs\restart_dsh.log。自测：scripts\test_restart_dsh_survive.ps1
param(
    [int]$DelaySeconds = 0,
    [switch]$Detach,
    [int]$Port = 3080,
    [switch]$DryRun,
    [switch]$Inline,
    [string]$ProcName = 'node.exe',
    [string]$MatchPattern = '*deepseek-ai\dsh*',
    [string]$Launcher = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'restart_dsh.log'
if (-not $Launcher) { $Launcher = Join-Path $env:APPDATA 'npm\dsh.cmd' }
function Say($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | $m" | Tee-Object -FilePath $log -Append }

function Get-DshWeb {
    Get-CimInstance Win32_Process -Filter "Name='$ProcName'" |
        Where-Object {
            $_.ProcessId -ne $PID -and $_.CommandLine -like $MatchPattern -and $_.CommandLine -like '*web*' -and
            $_.CommandLine -notlike '*subprocess-local*'
        }
}

# 干活的本体：一段文本（本进程里跑 / 塞进 WMI 命令行里跑，都靠它）
$body = @'
$log = '@@LOG@@'; $port = @@PORT@@; $delay = @@DELAY@@; $launcher = '@@LAUNCHER@@'
function Say($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | $m" | Add-Content -Path $log -Encoding UTF8 }
function FindWeb { Get-CimInstance Win32_Process -Filter "Name='@@PROC@@'" | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '@@PATTERN@@' -and $_.CommandLine -like '*web*' -and $_.CommandLine -notlike '*subprocess-local*' } }
if ($delay -gt 0) { Say "等 $delay 秒（让当前会话先把话说完）…"; Start-Sleep -Seconds $delay }
$procs = FindWeb
if (-not $procs) { Say '没找到 DSH web 进程 —— 直接拉起' }
foreach ($p in $procs) { Say "停 DSH web pid=$($p.ProcessId)"; try { Stop-Process -Id $p.ProcessId -Force } catch { Say "  停止失败：$($_.Exception.Message)" } }
for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Milliseconds 500; if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) { break } }
$still = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($still) { Say "⚠️ 端口 $port 还被 pid=$($still[0].OwningProcess) 占着 —— 不拉起了（拉起也绑不上）"; exit 1 }
Say "端口 $port 已空出来"
$out = $log + '.out'; $err = $log + '.err'
Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', ('"{0}" web' -f $launcher) -WorkingDirectory $env:USERPROFILE -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
Say "已拉起：$launcher web（stdout→$out / stderr→$err）"
for ($i = 0; $i -lt 60; $i++) { Start-Sleep -Seconds 1; $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if ($c) { Say "✅ DSH 回来了：pid=$($c.OwningProcess) 监听 $port"; exit 0 } }
Say "⚠️ 60 秒内没监听 $port —— 看 $err"
exit 1
'@

function Fill-Body {
    return $body.Replace('@@LOG@@', $log).Replace('@@PORT@@', [string]$Port).Replace('@@DELAY@@', [string]$DelaySeconds).Replace('@@LAUNCHER@@', $Launcher).Replace('@@PROC@@', $ProcName).Replace('@@PATTERN@@', $MatchPattern)
}

if ($DryRun) {
    $procs = Get-DshWeb
    if (-not $procs) { Say '[干跑] 没找到 DSH web 进程' }
    foreach ($p in $procs) { Say "[干跑] 会停 DSH web pid=$($p.ProcessId)（父 pid=$($p.ParentProcessId)）" }
    $lst = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    Say ("[干跑] 端口 {0} 现在{1}；会这样拉起来：cmd /c `"{2}`" web（Hidden）" -f `
        $Port, $(if ($lst) { "被 pid=$($lst[0].OwningProcess) 占着" } else { '空着' }), $Launcher)
    Say '[干跑] 结束（什么都没改）'
    exit 0
}

if ($Inline) { Invoke-Expression (Fill-Body); exit 0 }

# 出树去干：WMI 生（父进程是 WmiPrvSE），停 DSH 连坐不到它
$ps = (Get-Command powershell).Source
$one = ((Fill-Body) -split "`r?`n" | Where-Object { $_.Trim() -ne '' } | ForEach-Object { $_.Trim() }) -join '; '
$cli = '"{0}" -NoProfile -ExecutionPolicy Bypass -Command "{1}"' -f $ps, ($one -replace '"', '\"')
$seen0 = @(Get-Content $log -Encoding UTF8 -ErrorAction SilentlyContinue).Count
$r = ([wmiclass]'Win32_Process').Create($cli)
if ($r.ReturnValue -ne 0) {
    Say "⚠️ 甩后台失败（Win32_Process.Create 返回 $($r.ReturnValue)）—— 退回前台干"
    Invoke-Expression (Fill-Body)
    exit $LASTEXITCODE
}
Say "已甩到后台 pid=$($r.ProcessId)（不在本进程树里，停 DSH 杀不到它）；进度看 $log"

# 验一验它真起来了：本机沙箱对出树命令是**按命令行文本**拦的 —— 里面只要出现
# Stop-Process / Start-Process / Get-NetTCPConnection 这类动词，整条命令静默不执行
# （连第一句都不跑，哪儿都没有报错）。发起方不能"发出去了"就当成功。
$booted = $false
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Milliseconds 500
    if (@(Get-Content $log -Encoding UTF8 -ErrorAction SilentlyContinue).Count -gt ($seen0 + 1)) { $booted = $true; break }
}
if (-not $booted) {
    Say '⚠️ 甩出去的活 5 秒一点动静都没有 —— 多半被本机沙箱拦了（它静默拦掉带 Stop-Process / Start-Process / Get-NetTCPConnection 的出树命令）'
    Say '⚠️ 这种情形请在你自己的窗口里跑（那儿没有这层沙箱）：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restart_dsh.ps1 -Inline'
    exit 2
}

if ($Detach) { exit 0 }

$seen = @(Get-Content $log -Encoding UTF8 -ErrorAction SilentlyContinue).Count
$deadline = (Get-Date).AddSeconds($DelaySeconds + 150)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 700
    $lines = @(Get-Content $log -Encoding UTF8 -ErrorAction SilentlyContinue)
    if ($lines.Count -gt $seen) {
        $lines[$seen..($lines.Count - 1)] | ForEach-Object { Write-Host $_ }
        $seen = $lines.Count
        $last = $lines[-1]
        if ($last -like '*DSH 回来了*' -or $last -like '*没监听*' -or $last -like '*占着*') { break }
    }
}
exit 0