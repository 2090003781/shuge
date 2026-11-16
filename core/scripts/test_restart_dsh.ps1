# 自测（全对着"假 DSH"跑，绝不碰真的 3080）：
#   T1 -Inline 端到端：把假的停掉 → 用启动器拉起来 → 日志收尾
#   T2 -Detach 那条路：出树的活**要么真起来了、要么明确报出来没起来**
#      （本机沙箱按命令行文本静默拦 Stop-Process/Start-Process/Get-NetTCPConnection，见 restart_dsh.ps1 注释）
# 跑：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test_restart_dsh.ps1
# ⚠️ 别在自己的命令行里写 fake 那个标记词再跑本脚本：找"假 DSH"是按命令行匹配的，会把自己也算进去。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$ps = (Get-Command powershell).Source
$dshScript = Join-Path $root 'scripts\restart_dsh.ps1'
$logFile = Join-Path $root 'data\logs\restart_dsh.log'
$port = 3099
$marker = 'fakeds-web'
$fails = 0
function Check($name, $cond, $extra) {
    Write-Host "$(if ($cond) { 'PASS' } else { 'FAIL' }) $name$(if ($cond) { '' } else { '  ← ' + $extra })"
    if (-not $cond) { $script:fails++ }
}
function Get-Fake {
    @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like "*$marker*" } |
        ForEach-Object { $_.ProcessId })
}
function Stop-Fake { Get-Fake | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 800 }
function Wait-Child($p, $seconds) {
    $null = $p.WaitForExit($seconds * 1000)      # 只等这一个进程，不等它的后代
    if ($p.HasExited) { return $p.ExitCode }
    return -999
}
$tmp = Join-Path $env:TEMP 'fakeds-restart-test'
Stop-Fake                                                    # 上一轮的假 DSH 可能还攥着临时文件
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$fakeCmd = Join-Path $tmp 'fakeds.cmd'
$fakeCode = @"
@echo off
powershell -NoProfile -Command "`$l = New-Object System.Net.Sockets.TcpListener -ArgumentList ([System.Net.IPAddress]::Loopback), $port; `$l.Start(); `$tag = '$marker'; Start-Sleep -Seconds 900"
"@
Set-Content -Path $fakeCmd -Value $fakeCode -Encoding ASCII
function Start-Fake {
    Stop-Fake
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $fakeCmd -WindowStyle Hidden
    for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Milliseconds 500; if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { return $true } }
    return $false
}

Write-Host '===== T1 -Inline 端到端（停假的 → 拉起来） ====='
Check '假 DSH 起来了（测试端口在监听）' (Start-Fake) "端口 $port 没监听"
$old = @(Get-Fake)[0]
$before = @(Get-Content $logFile -Encoding UTF8 -ErrorAction SilentlyContinue).Count
$cOut = Join-Path $tmp 'child.out'; $cErr = Join-Path $tmp 'child.err'
$args1 = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $dshScript, '-Inline', '-Port', "$port", '-ProcName', 'powershell.exe', '-MatchPattern', "*$marker*", '-Launcher', $fakeCmd)
$child = Start-Process -FilePath $ps -WindowStyle Hidden -PassThru -RedirectStandardOutput $cOut -RedirectStandardError $cErr -ArgumentList $args1
$code = Wait-Child $child 120
$tail = (@(Get-Content $logFile -Encoding UTF8 -ErrorAction SilentlyContinue) | Select-Object -Skip $before) -join "`n"
$listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
Check '脚本没报任何 ⚠️（即成功收尾）' (-not ($tail -like '*⚠️*')) $tail
Check '假的被停掉了（老 pid 没了）' (-not (Get-CimInstance Win32_Process -Filter "ProcessId=$old" -ErrorAction SilentlyContinue)) "老 pid=$old 还在"
Check '又被拉起来了（端口重新监听，且是新 pid）' ($listen -and $listen[0].OwningProcess -ne $old) "listen=$(if ($listen) { $listen[0].OwningProcess } else { '无' }) old=$old"
Check '日志收尾了（✅ DSH 回来了）' ($tail -like '*DSH 回来了*') $tail

Write-Host ''
Write-Host '===== T2 -Detach 那条路：要么起来了、要么大声报 ====='
$before2 = @(Get-Content $logFile -Encoding UTF8 -ErrorAction SilentlyContinue).Count
$dOut = Join-Path $tmp 'detach.out'
$args2 = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $dshScript, '-Detach', '-DelaySeconds', '5', '-Port', "$port", '-ProcName', 'powershell.exe', '-MatchPattern', "*$marker*", '-Launcher', $fakeCmd)
$child2 = Start-Process -FilePath $ps -WindowStyle Hidden -PassThru -RedirectStandardOutput $dOut -RedirectStandardError (Join-Path $tmp 'detach.err') -ArgumentList $args2
$code2 = Wait-Child $child2 60
Start-Sleep -Seconds 3
$out = @(Get-Content $dOut -Encoding UTF8 -ErrorAction SilentlyContinue) -join "`n"
$tail2 = (@(Get-Content $logFile -Encoding UTF8 -ErrorAction SilentlyContinue) | Select-Object -Skip $before2) -join "`n"
$loud = $tail2 -like '*甩出去的活*'          # 从日志读：子进程 stdout 是另一套编码，别拿它比中文
$workerRan = $tail2 -like '*等 5 秒*'        # 这句只有出树的活会写，发起方不写
Check '没装作成功（要么出树的活真写了日志，要么明确报"没起来"）' ($workerRan -or $loud) "tail=$tail2"
Write-Host "  这条路上实际：出树进程跑了=$workerRan，明确报没起来=$loud（本沙箱里预期是后者）"

Stop-Fake
Write-Host ''
if ($fails) { Write-Host "失败 $fails 条"; exit 1 }
Write-Host '全部通过'
exit 0