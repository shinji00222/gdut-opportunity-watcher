param(
    [string]$FirstTime = "08:00",
    [string]$SecondTime = "20:00"
)

$ErrorActionPreference = "Stop"
$appDir = Join-Path $env:LOCALAPPDATA "GDUTCompetitionWatcher"
$exeSource = Join-Path $PSScriptRoot "GDUTCompetitionWatcher.exe"
$exeTarget = Join-Path $appDir "GDUTCompetitionWatcher.exe"

if (-not (Test-Path -LiteralPath $exeSource)) {
    throw "找不到 GDUTCompetitionWatcher.exe，请从完整发布包内运行此脚本。"
}

New-Item -ItemType Directory -Path $appDir -Force | Out-Null
Copy-Item -LiteralPath $exeSource -Destination $exeTarget -Force

$action = New-ScheduledTaskAction -Execute $exeTarget -Argument "--scheduled"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

foreach ($item in @(@("上午", $FirstTime), @("晚上", $SecondTime))) {
    $trigger = New-ScheduledTaskTrigger -Daily -At $item[1]
    Register-ScheduledTask -TaskName "广工竞赛提醒-$($item[0])" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "检查广东工业大学竞赛通知，完成后自动退出" -Force | Out-Null
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $desktop "广工竞赛提醒.lnk"))
$shortcut.TargetPath = $exeTarget
$shortcut.WorkingDirectory = $appDir
$shortcut.Save()

Start-Process $exeTarget
Write-Host "安装完成：每天 $FirstTime 和 $SecondTime 自动检查。"
