@echo off
rem 拖入自动处理接口：将文件或文件夹拖放到本脚本图标上即可
rem 支持：txt/csv 清单（每行一个类群ID或物种名）→ 自动爬取
rem       图片文件/文件夹（文件夹名=物种名/中文名/类群ID）→ 按种类依次指纹编码入库
rem 可一次拖入多个项目；源文件保持原位，报告存于 data\drop_inbox\processed_已处理\
rem 作者签名：天咏（明月照今日，风摇过两川。）
rem 说明：本脚本与本程序输出均采用系统默认中文编码（GBK），请勿添加 chcp 切换，否则中文会乱码
cd /d "%~dp0"

if "%~1"=="" (
    echo 未拖入文件：将以常驻监视模式运行，监视 data\drop_inbox 目录。
    echo 将 txt/csv 清单、图片或图片文件夹放入该目录即可自动处理，按 Ctrl+C 退出。
    "%~dp0iNaturalist鸟类爬虫.exe" drop --watch
    goto end
)

rem 收集全部拖入项（支持一次拖入多个文件/文件夹），合并为一次调用
set "ARGS="
:collect
if "%~1"=="" goto run
set "ARGS=%ARGS% "%~1""
shift
goto collect

:run
"%~dp0iNaturalist鸟类爬虫.exe" drop --file %ARGS%

:end
pause
