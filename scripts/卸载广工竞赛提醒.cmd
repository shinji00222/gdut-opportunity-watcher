@echo off
chcp 65001 >nul
schtasks /Delete /TN "广工竞赛提醒-上午" /F 2>nul
schtasks /Delete /TN "广工竞赛提醒-晚上" /F 2>nul
del "%USERPROFILE%\Desktop\广工竞赛提醒.lnk" 2>nul
del "%LOCALAPPDATA%\GDUTCompetitionWatcher\GDUTCompetitionWatcher.exe" 2>nul
if exist "%LOCALAPPDATA%\GDUTCompetitionWatcher\GDUTCompetitionWatcher.exe" (
  echo 无法删除程序文件，请先关闭广工竞赛提醒后重新运行卸载程序。
  pause
  exit /b 1
)
echo 已移除计划任务、桌面快捷方式和程序文件。本地档案、历史与日志保留在 %%LOCALAPPDATA%%\GDUTCompetitionWatcher。
pause
