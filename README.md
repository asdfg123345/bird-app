# bird-app 鸟类

iNaturalist 鸟类数据采集、训练与视觉分析桌面应用（Python / Tkinter）。
> **安装包（推荐，v1.7.1）**：[`release/鸟类_v1.7.1/安装输出/鸟类_安装包_v1.7.1.exe`](release/鸟类_v1.7.1/安装输出/鸟类_安装包_v1.7.1.exe)
> （19.4 MB，自带 Python 3.11 运行时，目标机**无需安装 Python**；双击安装即用，
> 默认装到 `%LOCALAPPDATA%\Programs\鸟类`，安装/运行都不需要管理员权限）。
> **首次运行若出现「Windows 已保护你的电脑 / 发布者未知」**：这是未签名安装包的
> SmartScreen 信誉提示，点「仍要运行」即可；根治办法见
> [代码签名与 SmartScreen 说明](docs/安装包_代码签名与SmartScreen说明.md)。
>
> **源码运行（无需安装包）**：下载 [Releases 页面](https://github.com/asdfg123345/bird-app/releases/latest)
> 的源码包，解压后装 Python 3.10+ → 解压 → 命令行运行 `python main.py gui`。
>
> **完整操作手册**：[docs/操作手册_鸟类_v1.7.0_20261018_001.md](docs/操作手册_鸟类_v1.7.0_20261018_001.md)
>
> **安装包构建记录**：[docs/系统日志_安装包构建_鸟类_v1.7.1_20261118_001.md](docs/系统日志_安装包构建_鸟类_v1.7.1_20261118_001.md)

## 功能一览

- **批量下载**：iNaturalist 鸟类类群数据采集（照片 + 观察 + 分类树）
- **拖入爬取**：txt/csv 物种清单拖入即爬；本地图片按文件夹名解析入库（SHA-256 去重）
- **数据管理**：双层数据库（raw_data.db 原始留存 + bird_observations.db 规范主库）、数据面板、健康检查
- **模型训练**：四模式训练、模型库管理、训练数据集导出/注册、外部模型微调
- **视觉分析**：ViT 物种识别、OWL-ViT 部位检测、图像特征分析

## 环境要求

- **安装包**：Windows 10/11（64 位），无需安装 Python 或任何第三方依赖。
- **源码运行**：Windows 10/11 + Python 3.10 及以上（安装时勾选 Add python.exe to PATH）。
- 模型训练/视觉分析需要另有一个含 PyTorch 的 Python 解释器（程序会自动探测本机常见位置，
  也可在训练页手动指定）；下载、拖入导入、数据库、数据面板等主功能不依赖它。

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

### 打包发布（可复现）

`packaging/` 目录提供一条可复现的发布链路（PyInstaller 冻结 + Inno Setup 安装包）：

- `build_installer.py`：准备打包源码（冻结适配 + 源码指纹水印 + PE 版本资源 + Inno 脚本）
  → PyInstaller onedir 冻结 → 部署源码形态组件（训练脚本/视觉系统/部位训练/启动 bat）
  → 预置空库 → Inno Setup 编译 → 冒烟测试；
- `verify_installer.py`：`smoke`（发布程序冒烟）、`installer`（静默安装→功能→卸载全链路）、
  `payload`（受限环境下的安装载荷验证）三种模式。

```powershell
python packaging/build_installer.py                       # 完整构建 + 验证
python packaging/build_installer.py --skip-build --skip-smoke  # 复用已有发布程序，只重编安装包
python packaging/verify_installer.py installer "release/鸟类_v1.7.1/安装输出/鸟类_安装包_v1.7.1.exe"
```

带代码签名构建（需代码签名证书，消除 SmartScreen 提示）：

```powershell
python packaging/sign_release.py --list-certs             # 查看证书库
python packaging/build_installer.py --sign-thumbprint <证书指纹>
```

产物：`release/鸟类_v1.7.1/安装输出/鸟类_安装包_v1.7.1.exe`；发布单元可直接运行：
`release/鸟类_v1.7.1/发布程序/鸟类/鸟类.exe`。

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
（含部位模型训练）、v1.7.1（当前代码的最新安装包，含 v1.7.0 之后的性能与正确性修复），
运行 `git tag` 查看全部版本。

## 许可

- 代码：[MIT License](LICENSE)
- 通过 iNaturalist 采集的照片与观察数据版权归原观察者所有，再分发须遵循其各自的 CC 许可条款
- 内嵌数字水印与版权标识请勿去除或篡改

## 作者

天咏（明月照今日，风摇过两川。）
