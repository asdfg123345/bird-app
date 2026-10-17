#!/usr/bin/env bash
# 默认批量下载：15 个鸟类科，每科 200 条研究级观察
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/run.sh" --max-per-taxon 200 --workers 6 --quality-grade research --photo-size large
