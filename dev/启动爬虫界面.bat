@echo off
rem 鸟类爬虫桌面控制台启动脚本（双击即弹出操作界面）
rem 作者签名：天咏（明月照今日，风摇过两川。）
chcp 65001 >nul
cd /d "%~dp0"

rem 优先使用 pythonw（无黑色控制台窗口，纯界面）；不存在则退回 python
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw bird_crawler_gui.py
) else (
    start "" python bird_crawler_gui.py
)
