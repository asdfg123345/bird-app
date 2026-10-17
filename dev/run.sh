#!/usr/bin/env bash
# iNaturalist 鸟类图片批量下载器启动脚本（Linux / macOS）
# 用法：./run.sh [参数]
# 示例：./run.sh --max-per-taxon 200 --workers 6

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 &> /dev/null; then
    echo "[错误] 未检测到 python3，请先安装 Python 3.10 或更高版本。"
    exit 1
fi

echo "[信息] 检查依赖..."
python3 -m pip install -r requirements.txt

echo "[信息] 启动批量下载..."
python3 batch_download_birds.py "$@"

echo "[信息] 运行结束。"
