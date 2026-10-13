# 广工机会提醒

一个面向广东工业大学学生的 Windows 信息提醒工具。它每天定时检查学校公开网站，收集竞赛、实习和出国项目，完成后自动退出，不常驻后台。

## 功能

- 竞赛、实习、出国三类信息；包括明确标注的国际/海外寒暑期项目
- Windows 定时检查与新消息通知
- 原通知链接、简介和硬性条件采集
- 本地个人档案与匹配度排序
- 已达成/未达成条件提示
- 项目收藏与分类筛选
- 按标题、简介和来源搜索
- 扩展赛事信息源；读取就业指导中心赛事卡片及分页、工程训练中心学科竞赛栏目，并从蓝桥杯官网提取当届报名时间
- 过滤导航、场地/住宿等非赛事链接和过旧赛事；来源结构变化时记录解析错误
- 手动检测和计划任务共用互斥锁，避免并发检查覆盖提醒状态；计划任务会在采集出错时报告失败
- 多窗口修改个人档案和收藏时采用状态事务；检测提交前合并最新档案和收藏，减少最后写入覆盖
- 已见通知按周刷新正文；同一原文链接的标题或报名截止日期变化时重新提醒，已过期通知按周复核以发现延期，同时避免每次计划检查重复抓取。详情抓取失败或正文动态加载时保留旧内容，不把页脚当作有效摘要
- 报名、申报、申请或提交截止日期已过的项目自动从本地列表和收藏中清理
- 多个信息源并行检查

## 隐私

个人档案、收藏、检测历史和日志只保存在用户电脑的：

```text
%LOCALAPPDATA%\GDUTCompetitionWatcher
```

这些数据不会写入仓库，也不会上传到 GitHub。

## 从源码运行

需要 Python 3.12 或更高版本：

```powershell
python -m pip install -r requirements.txt
python src\gdut_competition_watcher.py
```

执行一次静默检查并退出：

```powershell
python src\gdut_competition_watcher.py --scheduled
```

## 构建 Windows 程序

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name GDUTCompetitionWatcher --collect-all winotify `
  src\gdut_competition_watcher.py
```

生成文件位于 `dist\GDUTCompetitionWatcher.exe`。

## 数据准确性

工具只读取学校公开网页。网页返回验证页、无可解析链接或超过 8 MB 时会记为来源错误；网站改版、网络故障、动态加载的网页正文，以及只存在于附件、图片、微信群或公众号中的要求，都可能影响识别结果。报名和申请前请以学校原通知为准。

## 安装与卸载

升级前先关闭程序，再运行 Windows 发布包中的安装脚本。卸载脚本会移除程序文件、计划任务和快捷方式，但保留 `%LOCALAPPDATA%\GDUTCompetitionWatcher` 中的个人档案、历史和日志。

## 开源许可

MIT License

