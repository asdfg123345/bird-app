# bird-app 鸟类

iNaturalist 鸟类数据采集、训练与视觉分析桌面应用（Python / Tkinter）。

## 功能

- 批量下载：iNaturalist 鸟类类群数据采集（照片 + 观察 + 分类树）
- 拖入爬取：txt/csv 物种清单拖入即爬；本地图片按文件夹名解析入库（SHA-256 去重）
- 数据管理：双层数据库（raw_data.db 原始留存 + bird_observations.db 规范主库）、数据面板、健康检查
- 模型训练：四模式训练、模型库管理、训练数据集导出/注册
- 视觉分析：ViT 物种识别、OWL-ViT 部位检测、图像特征分析

## 快速开始

```
python main.py gui       # 桌面控制台
python main.py batch     # 批量下载 15 个鸟类科
python main.py copyright # 版权与数字水印信息
```

环境：Windows 10/11 + Python 3.10+（训练需含 PyTorch 的解释器）。

## 版本历史

本项目按历代发布版本重建了完整 git 历史，从 v0.1.0（PyInstaller 爬虫初版）到 v1.7.0（混合加密发布版），运行 `git tag` 查看全部版本。

## 许可

- 代码：[MIT License](LICENSE)
- 通过 iNaturalist 采集的照片与观察数据版权归原观察者所有，再分发须遵循其各自的 CC 许可条款
- 内嵌数字水印与版权标识请勿去除或篡改

## 作者

天咏（明月照今日，风摇过两川。）