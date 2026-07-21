@echo off
chcp 65001 >nul
schtasks /Delete /TN "广工竞赛提醒-上午" /F 2>nul
schtasks /Delete /TN "广工竞赛提醒-晚上" /F 2>nul
del "%USERPROFILE%\Desktop\广工竞赛提醒.lnk" 2>nul
echo 已移除计划任务和桌面快捷方式。本地历史保留在 %%LOCALAPPDATA%%\GDUTCompetitionWatcher。
pause
