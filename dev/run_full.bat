@echo off
chcp 65001 >nul
REM 全量批量下载：15 个鸟类科，不限制每科数量（受目标网站数据总量限制）
REM 警告：此模式会下载大量图片，耗时很长，请确保磁盘空间充足。
call "%~dp0run.bat" --max-per-taxon 0 --workers 6 --quality-grade research --photo-size large
