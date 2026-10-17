# iNaturalist 鸟类图片批量下载器

基于 iNaturalist 官方 API（https://api.inaturalist.org/v1）合规下载鸟类观察记录与图片。支持按科分类存放，自动限速、失败重试、断点续传。

## 环境要求

- Python 3.10 或更高版本
- 网络连接（访问 iNaturalist API）
- 足够的磁盘空间（图片按尺寸占用，large 尺寸平均约 200KB/张）

## 文件说明

| 文件 | 说明 |
|------|------|
| `batch_download_birds.py` | 批量下载入口脚本 |
| `download_inaturalist.py` | 单类群下载核心模块 |
| `requirements.txt` | Python 依赖 |
| `run.bat` / `run.sh` | 通用启动脚本（自动安装依赖） |
| `run_default.bat` / `run_default.sh` | 默认配置快捷启动 |
| `run_full.bat` / `run_full.sh` | 全量下载快捷启动 |

## 安装步骤

### Windows

1. 安装 Python（https://www.python.org/downloads/），安装时勾选 **Add Python to PATH**。
2. 将本文件夹复制到目标电脑任意位置。
3. 双击 `run_default.bat` 即可开始默认批量下载。

### Linux / macOS

1. 安装 Python 3.10+。
2. 将本文件夹复制到目标电脑任意位置。
3. 打开终端，进入该文件夹：

```bash
cd /path/to/iNaturalist
chmod +x run.sh run_default.sh run_full.sh
./run_default.sh
```

## 命令行用法

直接使用 Python：

```bash
python batch_download_birds.py --max-per-taxon 200 --workers 6 --quality-grade research --photo-size large
```

或使用启动脚本：

```bash
run.bat --max-per-taxon 500 --workers 8 --quality-grade research
```

### 主要参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--max-per-taxon` | 200 | 每个类群最多下载的观察记录数；设为 0 表示不限制 |
| `--workers` | 6 | 并行图片下载线程数 |
| `--quality-grade` | research | 观察质量过滤：`research`（研究级）/ `needs_id` / `casual` / `any` |
| `--photo-size` | large | 图片尺寸：`original` / `large` / `medium` |
| `--taxon-ids` | 内置 15 科 | 自定义类群 ID，空格分隔 |
| `--save-raw-json` | 关闭 | 保存原始 API 数据为 JSONL |
| `--no-skip-places` | 关闭 | 启用地名查询（会增加大量 API 调用） |

### 自定义类群示例

```bash
python batch_download_birds.py --taxon-ids 574 6912 19728 --max-per-taxon 100 --workers 4
```

## 输出目录结构

```
data/
├── taxon_574_Phasianidae_xxx/          # 雉科
│   ├── photos/                         # 图片文件
│   ├── observations.csv                # 观察记录元数据
│   ├── photos.csv                      # 图片下载记录
│   ├── places.json                     # 地名信息（仅当启用时）
│   └── summary.json                    # 该类群下载汇总
├── taxon_6912_Anatidae_xxx/            # 鸭科
│   └── ...
└── batch_summary.json                  # 总汇总
```

## 风险提示

- **中文路径**：Windows 下若将项目放在中文路径中，部分旧版 Python 或第三方工具可能出现编码问题。建议放在英文路径，如 `D:\iNaturalist`。
- **全量下载**：`--max-per-taxon 0` 会尝试下载全部可用数据，耗时可能数小时至数十小时，磁盘占用可能达到数十 GB。
- **API 限速**：脚本已内置 1.1 秒请求间隔和 429 自动等待机制，符合 iNaturalist 合理使用要求。

## 合规声明

本工具仅通过 iNaturalist 官方 API 获取公开数据，遵守目标网站 robots.txt 与 API 使用条款。下载的图片受各自上传者选择的许可协议约束，请在使用时遵守相应许可证（如 CC-BY、CC-BY-NC 等）。
