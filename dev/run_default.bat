@echo off
chcp 65001 >nul
REM 默认批量下载：15 个鸟类科，每科 200 条研究级观察
call "%~dp0run.bat" --max-per-taxon 200 --workers 6 --quality-grade research --photo-size large
