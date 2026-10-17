@echo off
rem 鸟类爬虫桌面控制台启动脚本（双击即可弹出操作界面）
rem 作者签名：天咏（明月照今日，风摇过两川。）
cd /d "%~dp0"
rem 优先使用 pythonw（无黑色控制台窗口），不存在则回退 python
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw main.py gui
) else (
    start "" python main.py gui
)
