@echo off
chcp 65001 >nul
REM iNaturalist 鸟类图片批量下载器启动脚本（Windows）
REM 用法：run.bat [参数]
REM 示例：run.bat --max-per-taxon 200 --workers 6

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10 或更高版本。
    echo 下载地址：https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [信息] 检查依赖...
pip install -r requirements.txt >nul 2>&1
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络连接。
    pause
    exit /b 1
)

echo [信息] 启动批量下载...
python batch_download_birds.py %*

echo [信息] 运行结束。
pause
