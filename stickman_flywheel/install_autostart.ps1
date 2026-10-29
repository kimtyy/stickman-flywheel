# 스틱맨 플라이휠 데몬을 Windows 작업 스케줄러에 등록한다 (1회 실행).
#  - 로그온 시 + 매일 08:50 시작, 이후 10분마다 재기동 시도(이미 떠 있으면 데몬이 스스로 종료 = 워치독)
#  - 절전 해제(WakeToRun), 놓친 실행 즉시 보충(StartWhenAvailable), 배터리 중에도 실행, 실패 시 자동 재시작
# 사용: powershell -ExecutionPolicy Bypass -File install_autostart.ps1
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py  = (Get-Command pythonw.exe).Source
$name = "StickmanFlywheelDaemon"

$action = New-ScheduledTaskAction -Execute $py -Argument "scheduler_daemon.py" -WorkingDirectory $dir
$t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$t2 = New-ScheduledTaskTrigger -Daily -At 08:50
$t2.Repetition = (New-ScheduledTaskTrigger -Once -At 08:50 -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Hours 23 -Minutes 55)).Repetition
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $name -Action $action -Trigger @($t1, $t2) -Settings $settings `
    -Description "스틱맨 숏폼 플라이휠 스케줄러 데몬 (09:00/15:00/16:00/17:30)" -Force | Out-Null
Write-Host "등록 완료: $name"
