#!/usr/bin/env bash
# 全量批量下载：15 个鸟类科，不限制每科数量
# 警告：此模式会下载大量图片，耗时很长，请确保磁盘空间充足。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/run.sh" --max-per-taxon 0 --workers 6 --quality-grade research --photo-size large
