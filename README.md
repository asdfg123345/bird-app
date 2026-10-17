# bird-app 鸟类

iNaturalist 鸟类数据采集、训练与视觉分析桌面应用（Python / Tkinter）。
> **小白快速上手**：无需 Git——前往 [Releases 页面](https://github.com/asdfg123345/bird-app/releases/latest)
> 下载 `鸟类_v1.7.0_源码.zip`，解压后按包内《下载后先看我.txt》三步即可运行
> （装 Python → 解压 → 命令行运行 `python main.py gui`）。
>
> **完整操作手册**：[docs/操作手册_鸟类_v1.7.0_20261018_001.md](docs/操作手册_鸟类_v1.7.0_20261018_001.md)

## 功能一览

- **批量下载**：iNaturalist 鸟类类群数据采集（照片 + 观察 + 分类树）
- **拖入爬取**：txt/csv 物种清单拖入即爬；本地图片按文件夹名解析入库（SHA-256 去重）
- **数据管理**：双层数据库（raw_data.db 原始留存 + bird_observations.db 规范主库）、数据面板、健康检查
- **模型训练**：四模式训练、模型库管理、训练数据集导出/注册、外部模型微调
- **视觉分析**：ViT 物种识别、OWL-ViT 部位检测、图像特征分析

## 环境要求

- Windows 10/11 + Python 3.10 及以上（安装时勾选 Add python.exe to PATH）
- 模型训练需要含 PyTorch 的 Python 解释器（程序会自动探测本机常见位置，也可在训练页手动指定）

## 使用说明

### 启动方式

1. **桌面控制台（推荐）**：双击 `src/启动爬虫界面.bat`，控制台内含「拖入文件爬取」标签页，
   可直接选择 txt/csv 处理，或启动常驻监视 `data/drop_inbox`（放入文件即自动爬取）
2. **命令行**：在 `src/` 目录打开命令行：

```
python main.py               # 查看全部命令
python main.py gui           # 桌面控制台
python main.py batch         # 批量下载 15 个鸟类科
python main.py panel         # 浏览器数据面板
python main.py stats         # 数据库统计
python main.py init-db       # 初始化数据库
python main.py health        # 数据库健康检查
python main.py copyright     # 版权与数字水印信息
```

### 拖入文件自动爬取

1. **文本清单**：将包含「类群ID 或 物种名」的 txt/csv 拖到 `src/拖入文件自动爬取.bat`
   图标上，自动解析并爬取。清单示例（每行一项）：

       20967
       Upupa epops

2. **本地图片入库**：将图片文件夹（或若干图片文件）拖到同一 bat 图标上，按文件夹名/
   子文件夹名解析物种，图片按种类依次指纹编码入库：内容 SHA-256 去重，重复图片自动跳过。

### 数据存储

| 内容 | 位置 |
|---|---|
| 照片 | `data/taxon_<ID>_<名称>/photos/` |
| 数据库 | `data/db/`（raw_data.db + bird_observations.db，SHA-256 自动去重） |
| 模型库 | `data/model_library/`（训练完成的模型自动入库） |
| 训练数据集注册表 | `data/train_datasets/registry.json` |

### 模型训练

训练页签支持四种模式（基础训练 / 继续训练 / 外部导入训练 / 外部模型微调）。
训练前需在页签中选择含 torch 的 Python 解释器；数据集可由 `dataset` 命令导出，
或从本地层级文件夹导入（train/val/test 自动 8:1:1 划分，验证集为空时自动防护）。

### 模型训练环境配置

训练与视觉分析需要独立的 PyTorch 环境（爬虫主程序只需 `requests` + `Pillow`）。
完整步骤见 [模型训练环境配置](docs/模型训练环境配置_20261018_001.md)，
依赖清单见 `src/requirements-train.txt`。速览：

```powershell
python -m venv .venv-train
.\.venv-train\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
.\.venv-train\Scripts\python.exe -m pip install -r src\requirements-train.txt
```

安装后在「模型训练」页把训练解释器指向 `.venv-train\Scripts\python.exe` 即可。

### 打包发布（可选）

`packaging/` 目录提供完整打包链路：`准备打包源码*.py`（源码指纹水印注入）→
`构建脚本*.ps1`（PyInstaller 构建）→ `鸟类.spec` → `封装脚本*.iss`（Inno Setup 安装包）。

## 目录结构

```
├── src/          最新版源码（主入口 main.py，GUI、爬虫、数据库、训练、视觉系统）
├── dev/          开发主线工作区快照（含 README、requirements、tests）
├── packaging/    打包脚本（iss/spec/ps1/封面）
├── docs/         开发系统日志与使用文档（2026-09-05 起全量）
├── LICENSE       MIT 许可证
└── README.md
```

## 版本历史

本项目按历代发布版本重建了完整 git 历史：从 2026-09-05 早期开发日志，到 v0.1.0
（PyInstaller 爬虫初版）、v1.0.0（首个安装包）、v1.3.0（应用更名「鸟类」）、v1.7.0
（混合加密发布版），运行 `git tag` 查看全部版本。

## 许可

- 代码：[MIT License](LICENSE)
- 通过 iNaturalist 采集的照片与观察数据版权归原观察者所有，再分发须遵循其各自的 CC 许可条款
- 内嵌数字水印与版权标识请勿去除或篡改

## 作者

天咏（明月照今日，风摇过两川。）
