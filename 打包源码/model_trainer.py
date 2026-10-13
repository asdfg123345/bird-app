#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 模块：鸟类图像分类模型训练脚本（可被 GUI 启动并实时显示终端进程）
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-27
"""鸟类图像分类模型训练脚本（PyTorch + torchvision，GPU 优先）。

面向「模型训练」界面的后端：从固定 train/val/test 划分的数据集目录
读取图片，微调预训练 ResNet50 做 200 类鸟类分类，全程向标准输出打印
带时间戳的进度日志，供 GUI 子进程实时捕获显示。

数据目录约定（与训练数据集压缩版一致，自动下钻一层外壳）：
    数据根目录/
        train/<类别文件夹>/*.jpg
        val/<类别文件夹>/*.jpg
        test/<类别文件夹>/*.jpg
类别文件夹名即类别标签（同名自动合并，靠类别序号区分）。

用法（一般由 GUI 调用，也可命令行独立运行）：
    python model_trainer.py --data-dir <数据根目录> --epochs 15 --batch-size 32
    python model_trainer.py --data-dir <数据根目录> --list-only   # 只扫描不训练
    python model_trainer.py --data-dir <数据根目录> --mode low    # 模式预设训练，产物入库
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# 划分目录名（小写比较），未匹配到的子目录（如部位检测数据集）自动忽略
SPLIT_NAMES = ("train", "val", "test")
IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# 模型输出根目录（相对脚本所在目录，避免硬编码盘符，也不污染数据集目录）
BASE_DIR = Path(__file__).resolve().parent
BASE_OUTPUT_DIR = BASE_DIR / "data" / "model_output"

# 可选依赖：torch / torchvision / PIL，缺失时给出明确安装提示后退出
try:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, Dataset
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from torchvision import models, transforms
    HAS_TVM = True
except ImportError:
    HAS_TVM = False

# 共享模型库模块（同目录 model_library.py）：命名取号 / 注册入库 / 预览图 / 训练模式预设。
# 缺失时仅「--mode 模式化训练与入库」不可用；旧训练路径（不带 --mode）不受影响。
try:
    import model_library
    HAS_MODEL_LIBRARY = True
except ImportError:
    model_library = None
    HAS_MODEL_LIBRARY = False


# 日志文件路径（main() 确定输出目录后设置；None 表示仅打印到标准输出）
_LOG_FILE_PATH: Path | None = None


def _emit(line: str) -> None:
    """把一行文本同时写到标准输出与日志文件（未启用日志文件时只打印）。"""
    print(line, flush=True)
    path = _LOG_FILE_PATH
    if path is None:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def log(msg: str) -> None:
    """向标准输出打印带时间戳的日志（GUI 子进程按行实时捕获），并写入日志文件。"""
    _emit(f"[{datetime.now():%H:%M:%S}] {msg}")


def log_metric(payload: dict) -> None:
    """输出结构化指标行（GUI 解析用，不显示在文本日志区）。

    格式：[METRIC] {json}；同时写入日志文件，便于事后复盘。
    """
    try:
        body = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    _emit("[METRIC] " + body)


# ============================================================
# 设备资源探测辅助（Task 2：适应模式 / 高速模式批大小推导）
# ============================================================

def get_gpu_memory_bytes() -> int | None:
    """读取 0 号 GPU 的显存总量（字节）；无 GPU 或读取失败返回 None。"""
    if not (HAS_TORCH and torch.cuda.is_available()):
        return None
    try:
        return int(torch.cuda.get_device_properties(0).total_memory)
    except Exception:
        return None


def get_system_memory_bytes() -> tuple[int, int] | None:
    """读取系统物理内存（总量, 可用）字节数；仅支持 Windows，失败返回 None。

    使用 ctypes 调用 Win32 API GlobalMemoryStatusEx，纯标准库实现。
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            # 与 Win32 MEMORYSTATUSEX 结构一一对应
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return int(stat.ullTotalPhys), int(stat.ullAvailPhys)
    except Exception:
        pass
    return None


def get_disk_free_bytes(path: Path) -> int:
    """返回路径所在磁盘的剩余字节数；路径尚不存在时向上回溯最近存在的祖先目录。"""
    probe = Path(path)
    while not probe.exists():
        parent = probe.parent
        if parent == probe:  # 已到根目录仍不存在，按根目录探测
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def amp_supported() -> bool:
    """检查当前环境是否支持 CUDA 混合精度（autocast + GradScaler）。"""
    return bool(HAS_TORCH and torch.cuda.is_available()
                and hasattr(torch.cuda, "amp")
                and hasattr(torch.cuda.amp, "autocast")
                and hasattr(torch.cuda.amp, "GradScaler"))


def derive_auto_batch(mode: str, architecture: str = "resnet50",
                      amp: bool = True) -> tuple[int, list[str]]:
    """按设备资源、网络架构与 AMP 状态自适应推导批大小。

    推导规则（安全优先）：
        - 显存预算：adaptive 用显存的 55%，high 用 80%；
        - 每样本显存按架构区分：ResNet 系约 5MB，ViT 系约 8MB（224px 输入 +
          混合精度实测经验值）；关闭 AMP 时按 2 倍估算（FP32 占用更高）；
        - 结果限制：adaptive 夹在 [8, 128]，high 夹在 [16, 256]；
        - 内存约束：系统可用内存减 2GB 预留后，每样本再按 4MB 估算，取更小值；
        - 无 GPU：CPU 安全回退（batch=8），调用方应同时把 workers 置 0。
    返回 (批大小, 中文探测日志行列表)，由调用方逐行打印。
    """
    lines: list[str] = []
    gpu_mem = get_gpu_memory_bytes()
    if gpu_mem is None:
        lines.append("未检测到 GPU，按 CPU 安全参数训练，速度较慢")
        lines.append("CPU 安全回退: batch=8, workers=0")
        return 8, lines

    # 每样本显存估算按架构区分；FP32 训练时占用约为混合精度的 2 倍
    arch_key = str(architecture or "resnet50")
    per_sample_gpu_bytes = 5 * 1024 * 1024         # ResNet 系（resnet50/resnest101）
    if arch_key.startswith("vit"):
        per_sample_gpu_bytes = 8 * 1024 * 1024     # ViT 注意力图占用更高
    if not amp:
        per_sample_gpu_bytes *= 2
    sample_mb = per_sample_gpu_bytes // 1024 // 1024
    lines.append(f"GPU 显存: {gpu_mem / 1024 ** 3:.2f} GB"
                 f"（架构 {arch_key}，混合精度={'开' if amp else '关'}，"
                 f"每样本按 {sample_mb}MB 估算）")
    mem_info = get_system_memory_bytes()
    if mem_info is not None:
        total_mem, avail_mem = mem_info
        lines.append(f"系统内存: 总量 {total_mem / 1024 ** 3:.2f} GB，"
                     f"可用 {avail_mem / 1024 ** 3:.2f} GB")
    else:
        avail_mem = None
        lines.append("系统内存: 未能读取（跳过内存约束，仅按显存推导）")

    # 显存预算推导：预算比例 × 显存总量 ÷ 每样本估算占用，再夹到安全区间
    budget_ratio = 0.55 if mode == "adaptive" else 0.80
    low_bound, high_bound = (8, 128) if mode == "adaptive" else (16, 256)
    batch_gpu = int(gpu_mem * budget_ratio // per_sample_gpu_bytes)
    batch_gpu = max(low_bound, min(high_bound, batch_gpu))
    batch = batch_gpu
    lines.append(f"显存预算: 按显存的 {budget_ratio:.0%} 推导，"
                 f"限制在 [{low_bound}, {high_bound}]，得 batch={batch_gpu}")

    # 系统内存约束：可用内存预留 2GB 后，每样本按 4MB 估算，与显存结果取更小值
    if avail_mem is not None:
        mem_budget = avail_mem - 2 * 1024 ** 3
        if mem_budget <= 0:
            lines.append("[告警] 系统可用内存不足 2GB，按最小批大小 1 训练（可能很慢）")
            batch = 1
        else:
            batch_mem = int(mem_budget // (4 * 1024 * 1024))
            batch = min(batch_gpu, batch_mem)
            lines.append(f"内存约束: 可用内存预留 2GB 后按每样本 4MB 估算，得上限 {batch_mem}")
    batch = max(1, batch)  # 极端环境兜底，避免批大小为 0 导致除零
    lines.append(f"最终批大小: {batch}")
    return batch, lines


def derive_auto_workers(mode: str | None) -> tuple[int, list[str]]:
    """按训练模式推导数据加载进程数（脚本有 __main__ 守护，Windows 多进程安全）。

    规则：
        - low（低速后台）：固定 2 个加载进程，尽量少占 CPU；
        - adaptive：按 CPU 核数一半推导，夹在 [2, 8]；
        - high（高速）：按 CPU 核数一半推导，夹在 [4, 12]；
        - 无模式（旧路径）按 adaptive 处理；
        - 命令行显式 --workers 正数优先于本推导（由调用方保证）；
        - 内存约束：可用内存不足时按每进程估算占用下调，防止子进程 OOM 崩溃。
    返回 (进程数, 中文探测日志行列表)。
    """
    mode = mode or "adaptive"
    cpu = os.cpu_count() or 4
    if mode == "low":
        workers = 2
        note = "低速模式：固定 2 个加载进程（后台低占用）"
    elif mode == "high":
        workers = max(4, min(12, cpu // 2))
        note = f"高速模式：按 CPU 核数 {cpu} 的一半推导"
    else:
        workers = max(2, min(8, cpu // 2))
        note = f"适应模式：按 CPU 核数 {cpu} 的一半推导"

    # 内存约束：每个加载进程 import torch 约 800MB，可用内存的 60% 留给加载进程池，
    # 防止 Windows 下多进程同时加载 torch 触发 OpenBLAS/系统内存分配失败
    mem_info = get_system_memory_bytes()
    if mem_info is not None:
        _, avail_mem = mem_info
        per_worker_bytes = 800 * 1024 * 1024
        mem_cap = max(1, int(avail_mem * 0.6 // per_worker_bytes))
        if workers > mem_cap:
            note += (f"；系统可用内存 {avail_mem / 1024 ** 3:.2f}GB，"
                     f"按每进程约 800MB 限制到 {mem_cap}")
            workers = mem_cap
    return workers, [f"数据加载进程: {note}，得 workers={workers}"]


def _infer_architecture(base_model: str) -> str:
    """在不实例化网络的前提下，按基础模型标识推断架构类型（供批大小估算）。

    与 build_model 的分支判定保持一致：内置 resnet50 / vit_bird_525；
    模型库条目读其 architecture 字段（旧记录缺省按 resnet50）。
    """
    base = (base_model or "resnet50").strip()
    if base == "resnet50":
        return "resnet50"
    if base == "vit_bird_525":
        return "vit_bird_525"
    if HAS_MODEL_LIBRARY:
        try:
            record = model_library.get_model(base)
        except Exception:
            record = None
        if record:
            return str(record.get("architecture") or "resnet50")
    return "resnet50"


def resolve_effective_root(root: Path) -> Path:
    """自动下钻定位真正的数据根目录（压缩包外壳多包一层时）。"""
    current = root
    for _ in range(4):
        if any((current / s).is_dir() for s in SPLIT_NAMES):
            return current
        children = [c for c in current.iterdir() if c.is_dir()]
        if len(children) != 1:
            return current
        current = children[0]
    return current


def scan_dataset(root: Path) -> dict[str, dict[str, list[Path]]]:
    """扫描数据根目录，返回 {类别名: {划分名: [图片路径]}}。"""
    grouped: dict[str, dict[str, list[Path]]] = {}
    for split in SPLIT_NAMES:
        split_dir = root / split
        if not split_dir.is_dir():
            continue
        for class_dir in sorted(split_dir.iterdir()):
            if not class_dir.is_dir():
                continue
            images = [p for p in class_dir.rglob("*")
                      if p.is_file() and p.suffix.lower() in IMG_SUFFIXES]
            if images:
                grouped.setdefault(class_dir.name, {}).setdefault(split, []).extend(images)
    return grouped


class BirdDataset(Dataset):
    """按固定划分类别文件夹读取图片的简单 Dataset（训练/验证/测试通用）。

    对损坏/无法读取的图片做容错：返回全零占位图并计数，训练主循环据此告警，
    避免因单张坏图导致整个训练崩溃。
    """

    # 类别级计数器：键为类别序号，值为损坏图片数
    corrupt_count: dict = {}

    def __init__(self, samples: list[tuple[Path, int]], transform=None):
        self.samples = samples
        self.transform = transform

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int):
        path, label = self.samples[idx]
        try:
            img = Image.open(path)
            img.load()          # 强制解码，提前暴露截断/损坏文件
            img = img.convert("RGB")
        except Exception:
            BirdDataset.corrupt_count[label] = \
                BirdDataset.corrupt_count.get(label, 0) + 1
            img = Image.new("RGB", (224, 224), (0, 0, 0))
        if self.transform:
            img = self.transform(img)
        return img, label


def build_samples(grouped: dict[str, dict[str, list[Path]]], split: str,
                  label_to_idx: dict[str, int]) -> list[tuple[Path, int]]:
    """把某一类别的某划分图片展开为 (路径, 标签序号) 样本列表。"""
    samples: list[tuple[Path, int]] = []
    for class_name, splits in grouped.items():
        idx = label_to_idx[class_name]
        for path in splits.get(split, []):
            samples.append((path, idx))
    return samples


# ============================================================
# 分架构训练策略注册表（不同网络用不同优化策略）
# 说明：训练模式（adaptive/low/high/custom）负责「训练多久、多大批量、何时早停」；
#       本注册表负责「怎么优化」——优化器、学习率分配、调度器、增强、标签平滑。
#       两者正交叠加：模式定节奏，策略定手法。
# ============================================================
# ViT 系列（鸟类专域起点 / 通用 HuggingFace ViT）共用同一套迁移策略
_VIT_TRANSFER_STRATEGY = {
    "label": "ViT 迁移策略",
    "optimizer": "adamw",
    "backbone_lr_mult": 0.1,   # 骨干学习率 = 基础学习率 × 0.1（ViT 对学习率敏感）
    "weight_decay": 0.05,
    "warmup_epochs": 2,        # 线性升温轮数，避免开局大梯度破坏预训练特征
    "scheduler": "cosine",     # 余弦退火
    "label_smoothing": 0.1,
    "augment": "basic",        # 轻增强：仅缩放 + 水平翻转
    "description": "差异化学习率（骨干0.1×/分类头1×）+ 2轮升温 + 余弦退火 + 标签平滑0.1",
}

ARCH_TRAINING_STRATEGY = {
    "resnet50": {
        "label": "ResNet50 标准策略",
        "optimizer": "adamw",
        "backbone_lr_mult": 1.0,   # 1.0 = 单组等学习率（保持原有训练行为）
        "weight_decay": 1e-4,
        "warmup_epochs": 0,
        "scheduler": "cosine",
        "label_smoothing": 0.0,
        "augment": "basic",
        "description": "等学习率 + 余弦退火 + 基础增强（与原训练行为一致）",
    },
    "vit_bird_525": _VIT_TRANSFER_STRATEGY,
    "vit_hf": _VIT_TRANSFER_STRATEGY,
    "resnest101": {
        "label": "ResNeSt101 策略",
        "optimizer": "sgd",
        "backbone_lr_mult": 0.1,
        "weight_decay": 1e-4,
        "warmup_epochs": 1,
        "scheduler": "cosine",
        "label_smoothing": 0.1,
        "augment": "color",        # 增强内置数据增广：色彩抖动有助于大类别分类
        "description": "SGD(动量0.9) + 差异化学习率 + 1轮升温 + 余弦退火 + 标签平滑0.1",
    },
}


def get_training_strategy(architecture) -> dict:
    """按架构取训练策略；未登记架构按 ResNet50 标准策略兜底。"""
    return ARCH_TRAINING_STRATEGY.get(str(architecture or "resnet50"),
                                      ARCH_TRAINING_STRATEGY["resnet50"])


# ============================================================
# 评估指标注册表：架构 → 主指标与可选判据
# 说明：当前训练器实现图像分类，各分类架构主指标为 Top-1 准确率；
#       分割(mIoU)/检测(mAP) 条目为结构预留，待相应训练管线接入。
# ============================================================
_CLS_METRIC_ENTRY = {
    "primary": "top1_acc",
    "options": ["top1_acc", "top5_acc", "macro_f1", "val_loss"],
}
EVAL_METRIC_REGISTRY = {
    "resnet50": _CLS_METRIC_ENTRY,
    "vit_bird_525": _CLS_METRIC_ENTRY,
    "vit_hf": _CLS_METRIC_ENTRY,
    "resnest101": _CLS_METRIC_ENTRY,
    "segformer_seg": {"primary": "miou", "options": ["miou"]},   # 预留（当前不训练分割）
    "generic": _CLS_METRIC_ENTRY,
}

# 判据方向：True = 越大越好，False = 越小越好
METRIC_HIGHER_BETTER = {
    "top1_acc": True, "val_acc": True, "top5_acc": True,
    "macro_f1": True, "miou": True, "mAP": True, "val_loss": False,
}

# 指标中文名（日志与报告展示）
METRIC_CN_NAMES = {
    "top1_acc": "Top-1 准确率", "val_acc": "Top-1 准确率",
    "top5_acc": "Top-5 准确率", "macro_f1": "宏平均F1",
    "val_loss": "验证损失", "miou": "mIoU", "mAP": "mAP",
}


def get_eval_metrics(architecture) -> dict:
    """按架构取评估指标配置（主指标 + 可选判据）；未登记按分类兜底。"""
    return EVAL_METRIC_REGISTRY.get(str(architecture or "resnet50"),
                                    _CLS_METRIC_ENTRY)


# ============================================================
# 数据集划分预设（4 级 + 自定义）
# 说明：ratio 为 (训练, 验证, 检测) 三段权重；None 表示不使用自动划分
#       （沿用数据目录里既有的 train/val/test 结构）。
# ============================================================
SPLIT_PRESETS = {
    "existing":    {"label": "使用目录既有划分（默认）", "ratio": None},
    "balanced":    {"label": "均衡标准级 8:1:1", "ratio": (8.0, 1.0, 1.0)},
    "train_first": {"label": "训练优先级 9:0.5:0.5", "ratio": (9.0, 0.5, 0.5)},
    "val_heavy":   {"label": "验证强化级 7:1.5:1.5", "ratio": (7.0, 1.5, 1.5)},
    "custom":      {"label": "自定义比例", "ratio": None},
}


def parse_split_ratio(text) -> tuple | None:
    """解析 "训练,验证,检测" 比例字符串，返回三元组；非法时返回 None。"""
    if not text:
        return None
    parts = [p.strip() for p in str(text).replace("：", ":").split(",")]
    if len(parts) != 3:
        return None
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    if any(v < 0 for v in values) or sum(values) <= 0:
        return None
    if values[0] <= 0:
        return None  # 训练集必须大于 0
    return tuple(values)


def scan_flat_dataset(root: Path) -> dict[str, list[Path]]:
    """扫描数据集为「类别 → 图片列表」的平铺结构（忽略既有划分）。

    兼容两种目录形态：
      ① 平铺结构：root/类别名/*.jpg；
      ② 既有划分结构：root/{train,val,test}/类别名/*.jpg（三划分合并后按类别归并，
         便于用户用同一份数据按新比例重新划分）。
    """
    grouped: dict[str, list[Path]] = {}
    if any((root / s).is_dir() for s in SPLIT_NAMES):
        for split in SPLIT_NAMES:
            split_dir = root / split
            if not split_dir.is_dir():
                continue
            for class_dir in sorted(split_dir.iterdir()):
                if not class_dir.is_dir():
                    continue
                images = [p for p in class_dir.rglob("*")
                          if p.is_file() and p.suffix.lower() in IMG_SUFFIXES]
                if images:
                    grouped.setdefault(class_dir.name, []).extend(images)
    else:
        for class_dir in sorted(root.iterdir()):
            if not class_dir.is_dir():
                continue
            images = [p for p in class_dir.rglob("*")
                      if p.is_file() and p.suffix.lower() in IMG_SUFFIXES]
            if images:
                grouped[class_dir.name] = images
    return grouped


def per_class_adaptive_split(grouped_flat: dict[str, list[Path]],
                             ratio, seed: int) -> tuple:
    """按比例做「类内自适应分层划分」，返回 ({类名: {划分: [路径]}}, 统计字典)。

    小类保底规则（避免某些类别在验证/检测集中缺席，影响训练效果评估）：
        - 类别图片数 < 3：全部进训练集；
        - 类别图片数 < 6：不进检测集（只分训练/验证）；
        - 类别图片数 < 12：验证集不足 1 张时补 1 张、检测集不足 1 张时补 1 张；
        - 任何情况下训练集至少保留 1 张。
    类内随机打乱使用固定种子（可复现），类别之间互不影响。
    """
    ratios = [float(x) for x in ratio]
    total = sum(ratios) or 1.0
    norm = [r / total for r in ratios]
    rng = random.Random(seed)
    result: dict = {}
    stats: dict = {}
    for class_name, paths in grouped_flat.items():
        items = sorted(paths)          # 先排序，保证跨平台/跨次运行顺序一致
        rng.shuffle(items)             # 再按固定种子打乱
        n = len(items)
        if n < 3:
            assign = {"train": items, "val": [], "test": []}
        else:
            n_val = int(round(n * norm[1]))
            n_test = int(round(n * norm[2]))
            if n < 6:
                n_test = 0             # 小类不占检测集
            elif n < 12 and n_test == 0:
                n_test = 1             # 中小类至少留 1 张做检测
            if n_val == 0:
                n_val = 1              # 至少留 1 张做验证
            n_val = min(n_val, max(0, n - 1))
            n_test = min(n_test, max(0, n - n_val - 1))
            assign = {
                "test": items[:n_test],
                "val": items[n_test:n_test + n_val],
                "train": items[n_test + n_val:],
            }
        result[class_name] = assign
        stats[class_name] = {s: len(assign[s]) for s in SPLIT_NAMES}
    return result, stats


def build_param_groups(model, architecture: str, base_lr: float,
                       weight_decay: float, backbone_lr_mult: float) -> tuple:
    """按架构拆分骨干/分类头参数组，返回 (参数组列表, 说明文本)。

    分类头模块识别：torchvision/timm 的 fc，HuggingFace ViT 的 classifier。
    骨干组学习率 = base_lr × backbone_lr_mult，分类头组 = base_lr；
    倍率为 1.0 或无法区分时合并为单组（等价原行为，保持向后兼容）。
    """
    head_module, head_name = None, "分类头"
    if hasattr(model, "classifier"):
        head_module, head_name = model.classifier, "classifier"
    elif hasattr(model, "fc"):
        head_module, head_name = model.fc, "fc"
    head_ids = {id(p) for p in head_module.parameters()} if head_module is not None else set()
    head_params, backbone_params = [], []
    for param in model.parameters():
        if not param.requires_grad:
            continue
        (head_params if id(param) in head_ids else backbone_params).append(param)
    all_params = backbone_params + head_params
    if backbone_lr_mult == 1.0 or not head_params or not backbone_params:
        groups = [{"params": all_params, "lr": base_lr, "weight_decay": weight_decay}]
        note = f"单组等学习率 {base_lr:g}（{len(all_params)} 个参数张量）"
        return groups, note
    groups = [
        {"params": backbone_params, "lr": base_lr * backbone_lr_mult,
         "weight_decay": weight_decay},
        {"params": head_params, "lr": base_lr, "weight_decay": weight_decay},
    ]
    note = (f"差异化学习率：骨干 {base_lr * backbone_lr_mult:g}"
            f"（{len(backbone_params)} 个张量）、{head_name} {base_lr:g}"
            f"（{len(head_params)} 个张量）")
    return groups, note


def build_scheduler(optimizer, strategy: dict, epochs: int) -> tuple:
    """按策略构建学习率调度器（线性升温 + 余弦退火），返回 (scheduler, 说明)。

    不引入新依赖，用 LambdaLR 手写；warmup=0 且未指定 cosine 时返回 None
    （保持原有的恒定学习率行为）。
    """
    warmup = int(strategy.get("warmup_epochs") or 0)
    kind = strategy.get("scheduler")
    if kind != "cosine" and warmup <= 0:
        return None, "恒定学习率（无调度器）"
    import math as _math

    def lr_lambda(epoch_index: int) -> float:
        """epoch_index 从 0 开始（PyTorch LambdaLR 约定）。"""
        if warmup > 0 and epoch_index < warmup:
            return float(epoch_index + 1) / float(warmup + 1)  # 线性升温
        if kind != "cosine":
            return 1.0
        progress = (epoch_index - warmup) / max(1, epochs - warmup)
        progress = min(max(progress, 0.0), 1.0)
        return 0.5 * (1.0 + _math.cos(_math.pi * progress))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)
    parts = []
    if warmup > 0:
        parts.append(f"{warmup} 轮线性升温")
    if kind == "cosine":
        parts.append("余弦退火")
    return scheduler, " + ".join(parts)


def build_transforms(strategy: dict, img_size: int) -> tuple:
    """按策略返回 (训练增强, 评估增强)。

    basic：缩放 + 随机水平翻转（与既有训练完全一致）；
    color：在 basic 基础上加入轻微色彩抖动（供 ResNeSt 等新架构使用）。
    """
    normalize = transforms.Normalize([0.485, 0.456, 0.406],
                                     [0.229, 0.224, 0.225])
    eval_tf = transforms.Compose([
        transforms.Resize((img_size, img_size)),
        transforms.ToTensor(),
        normalize,
    ])
    layers = [transforms.Resize((img_size, img_size)),
              transforms.RandomHorizontalFlip()]
    if strategy.get("augment") == "color":
        layers.append(transforms.ColorJitter(brightness=0.1, contrast=0.1,
                                             saturation=0.1))
    layers += [transforms.ToTensor(), normalize]
    return transforms.Compose(layers), eval_tf


def _macro_f1(labels: list, preds: list, num_classes: int) -> float:
    """手写宏平均 F1（避免引入 sklearn 依赖，保证任何环境结果一致）。"""
    if num_classes <= 0 or not labels:
        return 0.0
    tp = [0] * num_classes
    fp = [0] * num_classes
    fn = [0] * num_classes
    for t, p in zip(labels, preds):
        if t == p:
            tp[t] += 1
        else:
            fp[p] += 1
            fn[t] += 1
    f1_sum = 0.0
    for i in range(num_classes):
        precision = tp[i] / (tp[i] + fp[i]) if (tp[i] + fp[i]) else 0.0
        recall = tp[i] / (tp[i] + fn[i]) if (tp[i] + fn[i]) else 0.0
        f1_sum += 2 * precision * recall / (precision + recall) \
            if (precision + recall) else 0.0
    return f1_sum / num_classes


def per_class_metrics(labels: list, preds: list, class_names: list) -> list:
    """计算每个类别的 precision/recall/f1/support，供评估报告 CSV 使用。"""
    num_classes = len(class_names)
    tp = [0] * num_classes
    fp = [0] * num_classes
    fn = [0] * num_classes
    support = [0] * num_classes
    for t, p in zip(labels, preds):
        if 0 <= t < num_classes:
            support[t] += 1
        if t == p:
            tp[t] += 1
        else:
            if 0 <= p < num_classes:
                fp[p] += 1
            if 0 <= t < num_classes:
                fn[t] += 1
    rows = []
    for i in range(num_classes):
        precision = tp[i] / (tp[i] + fp[i]) if (tp[i] + fp[i]) else 0.0
        recall = tp[i] / (tp[i] + fn[i]) if (tp[i] + fn[i]) else 0.0
        f1 = 2 * precision * recall / (precision + recall) \
            if (precision + recall) else 0.0
        rows.append({
            "class": class_names[i], "index": i,
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(f1, 4), "support": support[i],
        })
    return rows


def evaluate_model(model, loader, criterion, device, num_classes: int,
                   collect_predictions: bool = False) -> dict:
    """在给定数据加载器上评估模型，返回指标字典。

    计算项：验证损失、Top-1、Top-5、宏平均 F1；collect_predictions=True 时
    额外返回逐样本的 (labels, preds) 便于生成每类明细报表。
    """
    model.eval()
    loss_sum, total = 0.0, 0
    top1_correct, top5_correct = 0, 0
    all_labels: list = []
    all_preds: list = []
    with torch.no_grad():
        for imgs, labels in loader:
            # pin_memory + non_blocking：异步拷贝，验证阶段同样避免 CPU 阻塞
            imgs = imgs.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            outputs = model(imgs)
            loss_sum += criterion(outputs, labels).item() * imgs.size(0)
            k = min(5, outputs.shape[1])
            topk = outputs.topk(k, dim=1).indices          # (N, k)
            top1_correct += (topk[:, 0] == labels).sum().item()
            # Top-5 命中：真实标签出现在前 k 个候选中
            hit5 = (topk == labels.unsqueeze(1)).any(dim=1).sum().item()
            top5_correct += hit5
            total += labels.size(0)
            all_preds.extend(topk[:, 0].cpu().tolist())
            all_labels.extend(labels.cpu().tolist())
    metrics = {
        "val_loss": round(loss_sum / max(1, total), 4),
        "top1_acc": round(top1_correct / max(1, total), 4),
        "top5_acc": round(top5_correct / max(1, total), 4),
        "macro_f1": round(_macro_f1(all_labels, all_preds, num_classes), 4),
        "count": total,
    }
    if collect_predictions:
        metrics["labels"] = all_labels
        metrics["preds"] = all_preds
    return metrics


def _save_resume_state(out_dir: Path, payload: dict) -> None:
    """原子写入断点状态文件（先写 .tmp 再替换，避免写一半损坏）。"""
    target = out_dir / "resume_state.pth"
    tmp = out_dir / "resume_state.tmp"
    try:
        torch.save(payload, str(tmp))
        os.replace(str(tmp), str(target))
    except Exception as exc:  # 断点保存失败不影响训练主流程
        log(f"[警告] 断点状态保存失败（不影响本次训练）：{exc}")
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def build_model(base_model: str, class_count: int, vit_dir=None):
    """按基础模型标识构建分类网络，返回 (model, architecture, 说明文本)。

    三分支：
      resnet50      —— torchvision ImageNet 预训练 ResNet50（默认）；
      vit_bird_525  —— transformers 本地 525 类鸟类 ViT（鸟类专域起点）；
      其他          —— 视为模型库中的模型名，读其权重继续微调
                       （旧记录缺 architecture 字段时按 resnet50 处理）。
    统一语义：先按目标类别数替换分类头，再载入基础权重（strict=False，
    分类头形状不同自动跳过，骨干权重完成迁移）。
    """
    base = (base_model or "resnet50").strip()
    if base == "resnet50":
        weights = models.ResNet50_Weights.DEFAULT
        model = models.resnet50(weights=weights)
        in_features = model.fc.in_features
        model.fc = nn.Linear(in_features, class_count)
        return model, "resnet50", "ImageNet 预训练 ResNet50"

    if base == "vit_bird_525":
        # 延迟导入：仅在选用 ViT 基础模型时才需要 transformers
        try:
            from transformers import ViTForImageClassification
        except ImportError:
            raise RuntimeError(
                "选用 525 类鸟类 ViT 基础模型需要 transformers 库，"
                "请先安装：pip install transformers")
        vit_path = Path(vit_dir).expanduser() if vit_dir else \
            model_library.resolve_vit_bird_525_dir()
        if not (vit_path / "model.safetensors").is_file():
            raise RuntimeError(
                f"未找到 525 类鸟类 ViT 权重文件：{vit_path}\\model.safetensors\n"
                f"请用 --vit-dir 或环境变量 {model_library.VIT_525_ENV_VAR} 指定权重目录。")
        model = ViTForImageClassification.from_pretrained(str(vit_path))
        in_features = model.classifier.in_features
        model.classifier = nn.Linear(in_features, class_count)
        return model, "vit_bird_525", f"525 类鸟类 ViT（{vit_path}）"

    # 模型库条目：以已训练权重继续微调
    record = model_library.get_model(base)
    if record is None or record.get("builtin"):
        raise RuntimeError(
            f"无法识别基础模型「{base}」：不是内置选项"
            f"（resnet50 / vit_bird_525），模型库中也不存在该模型。")
    model_dir = Path(str(record.get("path") or ""))
    # 权重文件：优先训练产物 best/last，兼容 HuggingFace 的 model.safetensors 与其余候选
    candidates = model_library.find_candidate_weights(model_dir)
    weights_path = None
    for cand_name in ("best_model.pt", "last_model.pt", "model.safetensors"):
        matched = [p for p in candidates if p.name == cand_name]
        if matched:
            weights_path = matched[0]
            break
    if weights_path is None:
        weights_path = candidates[0] if candidates else None
    if weights_path is None:
        raise RuntimeError(
            f"模型库模型「{base}」缺少可用权重文件"
            f"（best_model.pt / last_model.pt / model.safetensors）：{model_dir}")

    architecture = str(record.get("architecture") or "resnet50")
    # 分类头替换：按架构重建对应的骨干网络，保证权重键名与结构匹配
    if architecture in ("vit_bird_525", "vit_hf"):
        try:
            from transformers import AutoConfig, ViTForImageClassification
        except ImportError:
            raise RuntimeError(
                "微调 ViT 架构的库内模型需要 transformers 库，"
                "请先安装：pip install transformers")
        # 优先用模型自身目录的 config.json（本机训练产物已随目录复制配置）；
        # 缺失时回退到内置鸟类 ViT 的配置
        if (model_dir / "config.json").is_file() and \
                str(weights_path.name).startswith(("best_model", "last_model")):
            cfg = AutoConfig.from_pretrained(str(model_dir), local_files_only=True)
            cfg.num_labels = class_count
            model = ViTForImageClassification(cfg)
        else:
            vit_src = model_library.resolve_vit_bird_525_dir()
            model = ViTForImageClassification.from_pretrained(str(vit_src))
            in_features = model.classifier.in_features
            model.classifier = nn.Linear(in_features, class_count)
    elif architecture == "resnest101":
        try:
            import timm
        except ImportError:
            raise RuntimeError(
                "微调 ResNeSt101 架构的库内模型需要 timm 库，"
                "请先安装：pip install timm")
        model = timm.create_model("resnest101e", pretrained=False,
                                  num_classes=class_count)
    elif architecture == "resnet50":
        model = models.resnet50(weights=None)
        in_features = model.fc.in_features
        model.fc = nn.Linear(in_features, class_count)
    else:
        # 仅入库管理的架构（mmpretrain ViT / SegFormer 分割 / LLM-RNN 等）不能作训练起点
        info = model_library.get_architecture_info(architecture)
        raise RuntimeError(
            f"模型库模型「{base}」的架构为「{info.get('display_name')}」，"
            f"当前不支持作为训练起点（仅入库管理）。"
            f"请改选 ResNet50 / 鸟类ViT，或其他可推理架构的库内模型。")

    state = torch.load(str(weights_path), map_location="cpu", weights_only=False)
    state = model_library.unwrap_state_dict(state)
    # DataParallel 保存的权重（module. 前缀）先剥前缀再比对键名
    state = {str(k)[7:] if str(k).startswith("module.") else str(k): v
             for k, v in state.items()}
    # strict=False 只能容忍缺失/多余键，形状不匹配的键（如类别数不同的
    # 分类头）仍会报错；先按当前网络形状过滤再载入，骨干权重完成迁移
    model_shapes = {k: tuple(v.shape) for k, v in model.state_dict().items()}
    filtered: dict = {}
    skipped = 0
    for key, value in state.items():
        if isinstance(value, torch.Tensor) and \
                model_shapes.get(key) == tuple(value.shape):
            filtered[key] = value
        else:
            skipped += 1
    if not filtered:
        raise RuntimeError(
            f"模型库模型「{base}」的权重与 {architecture} 网络结构不匹配，"
            f"无法作为训练起点（迁移 0 项权重）。")
    model.load_state_dict(filtered, strict=False)
    note = (f"库内模型「{base}」继续微调（{weights_path.name}，架构 {architecture}，"
            f"迁移 {len(filtered)} 项权重，跳过分类头等 {skipped} 项形状不符项）")
    return model, architecture, note


def main() -> int:
    """训练主流程：解析参数 → 扫描数据 → 构建模型 → 逐轮训练/验证。"""
    parser = argparse.ArgumentParser(
        description="鸟类图像分类模型训练（ResNet50 微调，GPU 优先）")
    parser.add_argument("--data-dir", required=True, help="数据根目录（含 train/val/test）")
    parser.add_argument("--epochs", type=int, default=None,
                        help="训练轮数（默认 15；--mode 时未传则用模式预设值）")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="批大小（默认 32；adaptive/high 模式未传时按设备自适应推导）")
    parser.add_argument("--lr", type=float, default=None,
                        help="学习率（默认 1e-3；--mode 时未传则用模式预设值）")
    parser.add_argument("--img-size", type=int, default=224, help="输入图片边长（默认 224）")
    parser.add_argument("--workers", type=int, default=None,
                        help="数据加载线程数（默认 4；--mode 时 Windows 下默认 0）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    parser.add_argument("--out-dir", default=None,
                        help="模型与标签保存目录（默认 数据根目录/_model_output）")
    parser.add_argument("--list-only", action="store_true",
                        help="只扫描数据并打印类别/张数统计，不训练")
    # ---- Task 2 新增：训练模式 / 早停 / 优化器 / 模型命名入库 ----
    parser.add_argument("--mode", default=None,
                        choices=["adaptive", "low", "high", "custom"],
                        help="训练模式预设：adaptive=适应设备 / low=低速 / high=高速 / "
                             "custom=自定义（不传则保持默认行为，产物不入库）")
    parser.add_argument("--optimizer", default=None,
                        choices=["adamw", "adam", "sgd"],
                        help="优化器（默认 adamw；--mode 时未传则用模式预设值）")
    parser.add_argument("--patience", type=int, default=None,
                        help="早停耐心值：验证精度连续 N 轮无提升则提前结束（默认不早停，0 表示禁用）")
    parser.add_argument("--model-name", default="resnet50",
                        help="模型基名，入库目录名为 基名_M月D日_NNN（默认 resnet50）")
    parser.add_argument("--base-model", default="resnet50",
                        help="基础模型：resnet50=ImageNet 预训练（默认）/"
                             "vit_bird_525=本地 525 类鸟类 ViT/"
                             "模型库中的模型名=以其权重继续微调")
    parser.add_argument("--vit-dir", default=None,
                        help="525 类鸟类 ViT 权重目录（含 model.safetensors）；"
                             "缺省按 环境变量 BIRD_VIT_525_DIR → 脚本上两级 "
                             "bird_species_image_detection 顺序探测")
    parser.add_argument("--amp", action="store_true",
                        help="启用混合精度训练（仅 GPU 生效；高速模式预设自动开启）")
    # ---- 数据集自动划分（4 级比例 / 自定义） ----
    parser.add_argument("--auto-split", action="store_true",
                        help="按 --split-ratio 对数据做类内自适应划分（忽略目录既有 train/val/test）")
    parser.add_argument("--split-ratio", default=None,
                        help="自动划分比例「训练,验证,检测」，如 8,1,1（默认 8,1,1）；"
                             "需与 --auto-split 同时使用")
    # ---- 最佳模型判据（多指标体系） ----
    parser.add_argument("--select-metric", default=None,
                        choices=["top1_acc", "top5_acc", "macro_f1", "val_loss"],
                        help="最佳模型判据（默认按架构主指标，分类为 top1_acc）")
    # ---- 中断续训 ----
    parser.add_argument("--resume", default=None,
                        help="从指定输出目录的 resume_state.pth 断点继续训练；"
                             "此时 --epochs 表示总目标轮数")
    args = parser.parse_args()

    # ---- 依赖自检：给出清晰的中文安装提示 ----
    missing = []
    if not HAS_PIL:
        missing.append("Pillow（pip install pillow）")
    if not HAS_TORCH:
        missing.append("PyTorch（pip install torch torchvision）")
    if not HAS_TVM:
        missing.append("torchvision（pip install torchvision）")
    if missing:
        log("缺少训练依赖，请先安装：")
        for m in missing:
            log(f"  - {m}")
        log("CUDA(GPU)版安装示例：pip install torch torchvision --index-url "
            "https://download.pytorch.org/whl/cu126")
        return 2

    # ---- Task 2：训练模式预设检查与参数归一化 ----
    # 取值优先级：命令行显式传值 > 模式预设值 > 旧默认值。
    mode = args.mode
    preset = None
    if mode:
        if not HAS_MODEL_LIBRARY:
            log("训练模式依赖同目录的 model_library.py（共享模型库模块），未找到该文件。")
            return 2
        preset = model_library.TRAINING_MODE_PRESETS.get(mode)
        if preset is None:
            log(f"未知的训练模式: {mode}（可选 adaptive/low/high/custom）")
            return 1
        # 自定义模式的轮数/批大小/耐心值没有预设兜底，必须显式给出，避免误用空预设
        if mode == "custom":
            if args.epochs is None:
                log("自定义模式必须显式指定 --epochs（训练轮数）。")
                return 1
            if args.batch_size is None:
                log("自定义模式必须显式指定 --batch-size（批大小）。")
                return 1
            if args.patience is None:
                log("自定义模式必须显式指定 --patience（早停耐心值，传 0 表示不早停）。")
                return 1

    def pick(explicit, preset_value, legacy_value):
        """单个参数取值：命令行显式值 > 模式预设值 > 旧默认值。"""
        if explicit is not None:
            return explicit
        if mode and preset_value is not None:
            return preset_value
        return legacy_value

    epochs = pick(args.epochs, preset["epochs"] if preset else None, 15)
    lr = pick(args.lr, preset["lr"] if preset else None, 1e-3)
    optimizer_name = pick(args.optimizer, preset["optimizer"] if preset else None, "adamw")
    patience = pick(args.patience, preset["patience"] if preset else None, None)
    workers = args.workers  # None 表示未显式指定，待设备探测后再决定默认值
    # 批大小单独归一化：adaptive/high 预设的 batch=None 表示「按设备推导」，
    # 与「未传参数回退旧默认 32」含义不同，不能走 pick 的回退逻辑
    if args.batch_size is not None:
        batch_size = args.batch_size          # 命令行显式值最优先
    elif mode and preset["batch"] is not None:
        batch_size = preset["batch"]          # 模式预设给出固定批大小（如 low=8）
    elif mode:
        batch_size = None                     # 留空，待设备资源探测后推导
    else:
        batch_size = 32                       # 旧路径默认值

    # 参数合法性校验（中文报错，非 0 退出）
    if epochs is None or epochs <= 0:
        log(f"参数错误：训练轮数必须为正整数，当前为 {epochs}。")
        return 1
    if batch_size is not None and batch_size <= 0:
        log(f"参数错误：批大小必须为正整数，当前为 {batch_size}。")
        return 1
    if patience is not None and patience < 0:
        log(f"参数错误：早停耐心值不能为负数，当前为 {patience}。")
        return 1
    if workers is not None and workers < 0:
        log(f"参数错误：数据加载线程数不能为负数，当前为 {workers}。")
        return 1

    # ---- 自动划分比例归一化 ----
    split_ratio = parse_split_ratio(args.split_ratio) if args.split_ratio else None
    if args.auto_split:
        if split_ratio is None:
            split_ratio = (8.0, 1.0, 1.0)      # 未指定时用均衡标准级 8:1:1
        log(f"自动划分比例（训练:验证:检测）= "
            f"{split_ratio[0]:g}:{split_ratio[1]:g}:{split_ratio[2]:g}")
    elif args.split_ratio:
        log("提示：--split-ratio 需与 --auto-split 同时使用，本次已忽略该参数。")

    # ---- 中断续训：读取断点状态，并用快照回填数据/模型参数 ----
    resume_dir = None
    resume_state = None
    if args.resume:
        resume_dir = Path(args.resume).resolve()
        resume_file = resume_dir / "resume_state.pth"
        if resume_file.is_file():
            try:
                resume_state = torch.load(str(resume_file), map_location="cpu",
                                          weights_only=False)
            except Exception as exc:
                log(f"[警告] 断点文件读取失败，将按全新训练执行：{exc}")
                resume_state = None
        else:
            log(f"[警告] 断点目录缺少 resume_state.pth（{resume_file}），将按全新训练执行。")
        if isinstance(resume_state, dict):
            snapshot = resume_state.get("snapshot") or {}
            # 用快照回填训练条件，保证数据划分与网络结构与中断前完全一致
            if snapshot.get("data_dir"):
                args.data_dir = snapshot["data_dir"]
            if snapshot.get("base_model"):
                args.base_model = snapshot["base_model"]
            if snapshot.get("img_size"):
                args.img_size = int(snapshot["img_size"])
            if snapshot.get("seed") is not None:
                args.seed = int(snapshot["seed"])
            if snapshot.get("auto_split"):
                args.auto_split = True
                restored = parse_split_ratio(snapshot.get("split_ratio"))
                if restored:
                    split_ratio = restored
            log(f"续训模式：读取断点（已完成 "
                f"{resume_state.get('completed_epoch', 0)} 轮，"
                f"最佳 {METRIC_CN_NAMES.get(resume_state.get('best_metric_name'), '指标')}"
                f"={resume_state.get('best_metric')}）")

    # ---- 扫描数据集 ----
    data_root = resolve_effective_root(Path(args.data_dir).resolve())
    if not data_root.exists() or not data_root.is_dir():
        log(f"数据目录不存在或不是文件夹: {data_root}")
        return 1
    split_report = None
    if args.auto_split and split_ratio:
        flat = scan_flat_dataset(data_root)
        if not flat:
            log(f"未在 {data_root} 下找到任何类别目录与图片。")
            return 1
        grouped, split_stats = per_class_adaptive_split(flat, split_ratio, args.seed)
        split_report = {
            "mode": "auto_split",
            "ratio": list(split_ratio),
            "seed": args.seed,
            "data_root": str(data_root),
            "per_class": split_stats,
        }
        log(f"已按 {split_ratio[0]:g}:{split_ratio[1]:g}:{split_ratio[2]:g} "
            f"完成类内自适应分层划分（含小类保底）。")
    else:
        grouped = scan_dataset(data_root)
        if not grouped:
            log(f"未在 {data_root} 下找到 train/val/test 划分目录或任何图片。")
            return 1

    classes = sorted(grouped)
    label_to_idx = {name: i for i, name in enumerate(classes)}
    idx_to_label = {i: name for name, i in label_to_idx.items()}
    counts = {s: sum(len(v.get(s, [])) for v in grouped.values()) for s in SPLIT_NAMES}
    log(f"数据根目录: {data_root}")
    log(f"类别数: {len(classes)}")
    for s in SPLIT_NAMES:
        log(f"  {s}: {counts[s]} 张")
    if args.list_only:
        log("（list-only 模式：仅统计，不训练）")
        return 0
    if counts["train"] == 0:
        log("训练集为 0 张，无法训练。")
        return 1

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type == "cuda":
        # 输入尺寸固定为 224×224，开启 cudnn 自动寻优，让卷积选择最快算法
        torch.backends.cudnn.benchmark = True

    # ---- 可复现性：固定随机种子 ----
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(args.seed)

    log(f"计算设备: {device}" +
        (f"（GPU: {torch.cuda.get_device_name(0)}）" if device.type == "cuda" else ""))

    # ---- Task 2：批大小自适应推导 / workers 归一化 / AMP 开关归一化 ----
    # AMP 开关：命令行 --amp 显式开启 > 模式预设（True/"auto" 在有 GPU 时启用）> 关闭
    # 注意：AMP 需在批大小推导之前确定（FP32 与混合精度的每样本显存估算不同）
    has_gpu = device.type == "cuda"
    amp_enabled = False
    if args.amp:
        amp_enabled = has_gpu and amp_supported()
    elif mode and preset["amp"] in (True, "auto"):
        amp_enabled = has_gpu and amp_supported()
    if args.amp and not amp_enabled:
        log("提示：当前无可用 GPU 或环境不支持混合精度，已忽略 --amp（AMP 仅在 CUDA 上生效）。")
    elif mode and not args.amp and preset["amp"] is True and not amp_enabled:
        log("提示：当前无可用 GPU，已忽略该模式的混合精度（AMP）预设。")

    # 批大小：按模式 + 架构 + AMP 状态推导（命令行显式 --batch-size 最优先）
    if batch_size is None:
        # 仅 adaptive/high 预设的 batch 为 None（表示按设备资源自适应推导）
        arch_hint = _infer_architecture(args.base_model)
        batch_size, probe_lines = derive_auto_batch(mode, arch_hint, amp_enabled)
        for line in probe_lines:
            log(line)

    # workers：命令行显式正数最优先；未指定或 <=0 时按模式自动推导
    # （脚本有 __main__ 守护，Windows 下多进程加载安全；纯 CPU 训练保持主进程加载）
    if workers is None or workers <= 0:
        if device.type != "cuda":
            workers = 0
        else:
            workers, worker_lines = derive_auto_workers(mode)
            for line in worker_lines:
                log(line)

    # ---- 模型：按基础模型选择构建（ImageNet ResNet50 / 鸟类ViT / 库内微调） ----
    try:
        model, architecture, base_note = build_model(
            args.base_model, len(classes), vit_dir=args.vit_dir)
    except RuntimeError as exc:
        log(f"[错误] 基础模型加载失败：{exc}")
        return 1
    model.to(device)
    log(f"基础模型: {base_note}")
    log(f"网络架构: {architecture}，分类头类别数: {len(classes)}")

    # ---- 分架构训练策略：优化器结构/学习率分配/调度器/增强/标签平滑 ----
    strategy = get_training_strategy(architecture)
    metrics_cfg = get_eval_metrics(architecture)
    select_metric = args.select_metric or metrics_cfg.get("primary") or "top1_acc"
    higher_better = METRIC_HIGHER_BETTER.get(select_metric, True)
    # 策略里的优化器仅在用户/模式未显式指定优化器时生效（显式指定优先）
    if not args.optimizer and not (mode and preset and preset.get("optimizer")):
        optimizer_name = strategy.get("optimizer", optimizer_name)
    log(f"训练策略: {strategy.get('label')} —— {strategy.get('description')}")
    log(f"最佳模型判据: {METRIC_CN_NAMES.get(select_metric, select_metric)}"
        f"（{'越大越好' if higher_better else '越小越好'}）")

    # ---- 数据增强与加载（增强强度由策略决定） ----
    img_size = args.img_size
    train_tf, eval_tf = build_transforms(strategy, img_size)

    train_set = BirdDataset(build_samples(grouped, "train", label_to_idx), train_tf)
    val_set = BirdDataset(build_samples(grouped, "val", label_to_idx), eval_tf)
    test_set = BirdDataset(build_samples(grouped, "test", label_to_idx), eval_tf)
    # Windows 多进程 DataLoader 需 if __name__ == "__main__" 守护，故此处直接提示
    if workers > 0 and sys.platform.startswith("win"):
        log("提示：Windows 下多线程数据加载可能不稳定，如遇异常请将线程设为 0 重试。")
    pin = (device.type == "cuda")
    # 多进程加载时保持进程常驻并预取下一批，避免每轮重建进程与 GPU 空等数据
    loader_extra = {"persistent_workers": True, "prefetch_factor": 4} \
        if workers > 0 else {}
    train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True,
                              num_workers=workers, pin_memory=pin, **loader_extra)
    val_loader = DataLoader(val_set, batch_size=batch_size, shuffle=False,
                            num_workers=workers, pin_memory=pin, **loader_extra)
    test_loader = DataLoader(test_set, batch_size=batch_size, shuffle=False,
                             num_workers=workers, pin_memory=pin, **loader_extra)

    # 损失函数：按策略启用标签平滑（0 表示关闭，等价原 CrossEntropyLoss）
    smoothing = float(strategy.get("label_smoothing") or 0.0)
    criterion = nn.CrossEntropyLoss(label_smoothing=smoothing)

    # 参数组：按策略拆分骨干/分类头（差异化学习率）
    param_groups, groups_note = build_param_groups(
        model, architecture, lr, float(strategy.get("weight_decay") or 0.0),
        float(strategy.get("backbone_lr_mult") or 1.0))
    log(f"参数组: {groups_note}")
    if optimizer_name == "sgd":
        optimizer = torch.optim.SGD(param_groups, lr=lr, momentum=0.9)
    elif optimizer_name == "adam":
        optimizer = torch.optim.Adam(param_groups, lr=lr)
    else:
        optimizer = torch.optim.AdamW(param_groups, lr=lr)
    # 学习率调度器：线性升温 + 余弦退火（按策略）
    scheduler, sched_note = build_scheduler(optimizer, strategy, epochs)
    log(f"学习率调度: {sched_note}")
    # 混合精度梯度缩放器（enabled=False 时全部操作自动退化为普通前向/反向）
    scaler = torch.cuda.amp.GradScaler(enabled=amp_enabled)

    # ---- 输出目录：续训沿用断点目录；模式化训练走共享模型库自动取号；否则沿用旧默认目录 ----
    register_after_train = False      # 训练正常结束后是否注册入库
    generate_preview = False          # 是否生成预览图（仅模型库新流程）
    is_resume = isinstance(resume_state, dict) and resume_dir is not None
    if is_resume:
        out_dir = resume_dir
        register_after_train = True
        generate_preview = True
    elif mode and not args.out_dir:
        # 新流程：目录名按 基名_M月D日_NNN 自动取号，输出到共享模型库根目录
        dir_name, _date_str, _version = model_library.next_model_name(args.model_name)
        out_dir = model_library.MODEL_LIBRARY_DIR / dir_name
        register_after_train = True
        generate_preview = True
    else:
        out_dir = Path(args.out_dir).resolve() if args.out_dir else BASE_OUTPUT_DIR
        # 显式指定输出目录 + 模式化训练：仍注册入库（预览图不生成，保持旧产物结构）
        register_after_train = bool(mode)

    # 磁盘安全检查：模式化训练时输出磁盘剩余不足 5GB 拒绝训练（安全优先）
    if mode:
        disk_free = get_disk_free_bytes(out_dir)
        log(f"输出磁盘剩余空间: {disk_free / 1024 ** 3:.2f} GB")
        if disk_free < 5 * 1024 ** 3:
            log("[错误] 输出磁盘剩余空间不足 5GB，为避免训练中断导致产物损坏，已拒绝训练。"
                "请清理磁盘空间后重试。")
            return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 启用训练日志文件（双写 stdout + 文件；续训时接续同一日志，新训练重置） ----
    global _LOG_FILE_PATH
    _LOG_FILE_PATH = out_dir / "train_log.txt"
    if not is_resume:
        try:
            _LOG_FILE_PATH.write_text("", encoding="utf-8")
        except OSError as exc:
            log(f"[警告] 训练日志文件创建失败（不影响训练）：{exc}")
            _LOG_FILE_PATH = None
    log(f"训练日志文件: {_LOG_FILE_PATH if _LOG_FILE_PATH else '（未启用）'}")

    # 自动划分报告落盘（在内存中完成划分，不移动/复制用户原始文件）
    if split_report is not None:
        try:
            (out_dir / "split_report.json").write_text(
                json.dumps(split_report, ensure_ascii=False, indent=2),
                encoding="utf-8")
            log(f"数据划分报告已保存: {out_dir / 'split_report.json'}")
        except OSError as exc:
            log(f"[警告] 数据划分报告写入失败（不影响训练）：{exc}")

    # ViT 架构：把基础模型的 config.json / preprocessor_config.json 复制到产物目录，
    # 使训练产物目录保持可被视觉分析系统按 HuggingFace 目录方式加载
    if str(architecture).startswith("vit"):
        vit_src = Path(args.vit_dir).expanduser() if args.vit_dir \
            else model_library.resolve_vit_bird_525_dir()
        if vit_src.is_dir():
            for cfg_name in ("config.json", "preprocessor_config.json",
                             "preprocessor.json"):
                src_file = vit_src / cfg_name
                if src_file.is_file() and not (out_dir / cfg_name).exists():
                    try:
                        shutil.copy2(src_file, out_dir / cfg_name)
                    except OSError:
                        pass

    # 训练开始前从训练集随机抽 4 张样图路径，供正常结束后拼接预览图
    preview_samples: list[Path] = []
    if generate_preview:
        train_paths = [p for p, _ in train_set.samples]
        if train_paths:
            preview_samples = random.sample(train_paths, min(4, len(train_paths)))

    # ---- 续训恢复：载入断点中的模型/优化器/调度器状态与训练进度 ----
    history: list = []
    best_metric = None
    no_improve = 0
    start_epoch = 1
    if is_resume:
        try:
            model.load_state_dict(resume_state["model_state_dict"])
            if resume_state.get("optimizer_state_dict"):
                optimizer.load_state_dict(resume_state["optimizer_state_dict"])
            if scheduler is not None and resume_state.get("scheduler_state_dict"):
                scheduler.load_state_dict(resume_state["scheduler_state_dict"])
            start_epoch = int(resume_state.get("completed_epoch", 0)) + 1
            best_metric = resume_state.get("best_metric")
            no_improve = int(resume_state.get("no_improve", 0))
            history = list(resume_state.get("history") or [])
            log(f"已恢复断点状态：从第 {start_epoch} 轮继续训练"
                f"（历史记录 {len(history)} 轮）")
            # 续训已成功启动：清除「上次中断」提示文件，避免 GUI 重复引导续训
            if HAS_MODEL_LIBRARY:
                try:
                    stale = model_library.MODEL_LIBRARY_DIR / "last_interrupted.json"
                    if stale.is_file():
                        stale.unlink()
                except OSError:
                    pass
        except Exception as exc:
            log(f"[警告] 断点状态恢复失败，将按全新训练执行：{exc}")
            is_resume = False
            start_epoch, best_metric, no_improve, history = 1, None, 0, []
    if start_epoch > epochs:
        log(f"已完成轮数（{start_epoch - 1}）已达到目标轮数 {epochs}，直接进入结束评估。")

    # 启动信息：模式、最终生效参数、输出目录（全中文，供 GUI 与用户确认）
    mode_label = model_library.TRAINING_MODE_PRESETS[mode]["label"] if mode \
        else "默认模式（经典参数，不入库）"
    log(f"训练模式: {mode_label}")
    log(f"生效参数: epochs={epochs}, optimizer={optimizer_name}, "
        f"patience={patience if patience else '无'}, batch={batch_size}, lr={lr}, "
        f"amp={'开启' if amp_enabled else '关闭'}, workers={workers}, seed={args.seed}")
    log(f"输出目录: {out_dir}")
    log(f"开始训练：目标轮数 {epochs}，共 {len(classes)} 类")

    # 断点快照：记录重建训练条件所需的全部参数（续训时用于回填，保证数据一致）
    snapshot = {
        "data_dir": str(data_root),
        "base_model": args.base_model,
        "img_size": img_size,
        "seed": args.seed,
        "auto_split": bool(args.auto_split),
        "split_ratio": ",".join(f"{v:g}" for v in split_ratio) if split_ratio else None,
        "class_names": classes,
        "architecture": architecture,
        "epochs": epochs,
        "select_metric": select_metric,
    }

    def report_corrupt(stage: str) -> None:
        """汇总并打印损坏图片告警（按类别统计，最多列 10 类）。"""
        if not BirdDataset.corrupt_count:
            return
        total_corrupt = sum(BirdDataset.corrupt_count.values())
        log(f"[告警] {stage} 共遇到 {total_corrupt} 张损坏/无法读取的图片，已用占位图跳过：")
        for label_idx, cnt in sorted(
                BirdDataset.corrupt_count.items(), key=lambda x: -x[1])[:10]:
            log(f"    类别「{idx_to_label.get(label_idx, label_idx)}」: {cnt} 张")

    trained_epochs = max(0, start_epoch - 1)  # 已完成的轮数（含续训前完成的轮数）
    interrupted = False
    early_stopped = False
    try:
        for epoch in range(start_epoch, epochs + 1):
            trained_epochs = epoch
            model.train()
            t0 = time.time()
            # loss/正确数在 GPU 张量上累计，只在日志点才同步到 CPU，
            # 避免每个 batch 两次 .item() 把 CPU/GPU 流水线卡成串行
            running_loss_t = torch.zeros((), device=device)
            correct_t = torch.zeros((), dtype=torch.long, device=device)
            total = 0
            # 日志降频：至少每 200 个 batch 或最后一个才打印一次，避免刷爆界面
            log_every = max(1, min(200, len(train_loader) // 5 or 1))
            for i, (imgs, labels) in enumerate(train_loader, 1):
                # pin_memory + non_blocking：CPU→GPU 异步拷贝，与计算重叠
                imgs = imgs.to(device, non_blocking=True)
                labels = labels.to(device, non_blocking=True)
                optimizer.zero_grad()
                # 混合精度前向：autocast 关闭时等价普通前向
                with torch.cuda.amp.autocast(enabled=amp_enabled):
                    outputs = model(imgs)
                    loss = criterion(outputs, labels)
                # 混合精度反向与参数更新：GradScaler 关闭时自动等价普通反向
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
                running_loss_t += loss.detach() * imgs.size(0)
                correct_t += (outputs.argmax(1) == labels).sum()
                total += labels.size(0)
                if i % log_every == 0 or i == len(train_loader):
                    log(f"  [Epoch {epoch}/{epochs}] "
                        f"batch {i}/{len(train_loader)} "
                        f"loss={running_loss_t.item() / max(1, total):.4f} "
                        f"acc={correct_t.item() / max(1, total):.4f}")
            report_corrupt(f"Epoch {epoch} 训练")
            train_loss = running_loss_t.item() / max(1, total)
            train_acc = correct_t.item() / max(1, total)

            # ---- 验证：一次性计算全部指标（损失 / Top-1 / Top-5 / 宏F1） ----
            metrics = evaluate_model(model, val_loader, criterion, device, len(classes))
            report_corrupt(f"Epoch {epoch} 验证")
            dt = time.time() - t0
            current_lr = optimizer.param_groups[0]["lr"]
            log(f"[Epoch {epoch}/{epochs}] 完成：train_loss={train_loss:.4f} "
                f"train_acc={train_acc:.4f} val_loss={metrics['val_loss']:.4f} "
                f"top1={metrics['top1_acc']:.4f} top5={metrics['top5_acc']:.4f} "
                f"macro_f1={metrics['macro_f1']:.4f} lr={current_lr:.2e} 耗时 {dt:.1f}s")

            # ---- 最佳模型：按判据比较（越大越好/越小越好）；持平也保存（保持旧语义） ----
            metric_value = metrics.get(select_metric)
            prev_best = best_metric
            if metric_value is None:
                is_best = False
            elif prev_best is None:
                is_best = True
            elif higher_better:
                is_best = metric_value >= prev_best
            else:
                is_best = metric_value <= prev_best
            if is_best:
                best_metric = metric_value
                torch.save(model.state_dict(), out_dir / "best_model.pt")
                log(f"  已保存最佳模型（{METRIC_CN_NAMES.get(select_metric, select_metric)}"
                    f"={metric_value:.4f}）")
            # 早停计数：首轮建立基线或严格提升时重置，其余累加
            strict_better = metric_value is not None and (
                prev_best is None or
                (metric_value > prev_best if higher_better else metric_value < prev_best))
            no_improve = 0 if strict_better else no_improve + 1

            # ---- 逐轮记录：历史落盘 + 结构化指标行（供 GUI 指标表） ----
            history.append({
                "epoch": epoch,
                "train_loss": round(train_loss, 4),
                "train_acc": round(train_acc, 4),
                "val_loss": metrics["val_loss"],
                "top1_acc": metrics["top1_acc"],
                "top5_acc": metrics["top5_acc"],
                "macro_f1": metrics["macro_f1"],
                "lr": current_lr,
                "is_best": bool(is_best),
                "seconds": round(dt, 1),
            })
            log_metric({
                "epoch": epoch,
                "train_loss": round(train_loss, 4),
                "val_loss": metrics["val_loss"],
                "top1": metrics["top1_acc"],
                "top5": metrics["top5_acc"],
                "macro_f1": metrics["macro_f1"],
                "lr": current_lr,
                "is_best": bool(is_best),
                "seconds": round(dt, 1),
            })
            try:
                (out_dir / "training_history.json").write_text(
                    json.dumps(history, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except OSError:
                pass
            # 断点状态原子落盘，供中断后继续训练
            _save_resume_state(out_dir, {
                "completed_epoch": epoch,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "scheduler_state_dict": scheduler.state_dict() if scheduler else None,
                "best_metric": best_metric,
                "best_metric_name": select_metric,
                "no_improve": no_improve,
                "history": history,
                "snapshot": snapshot,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            # 学习率调度步进（在本轮训练结束后执行）
            if scheduler is not None:
                scheduler.step()
            # 早停判断：连续 patience 轮无提升则提前结束（patience 为 0/None 时不启用）
            if patience and no_improve >= patience:
                early_stopped = True
                log(f"早停触发：{METRIC_CN_NAMES.get(select_metric, select_metric)}"
                    f"连续 {patience} 轮未提升，提前结束训练。")
                break
    except KeyboardInterrupt:
        log("收到中断信号（Ctrl+C / 停止训练），正在保存当前模型后退出…")
        interrupted = True
    except Exception:
        log("训练过程发生异常，保存当前模型后退出：")
        traceback.print_exc()
        interrupted = True
    finally:
        # 统一兜底：无论正常/中断/异常都保存当前进度与标签，避免白跑
        try:
            (out_dir / "labels.json").write_text(
                json.dumps(label_to_idx, ensure_ascii=False, indent=2),
                encoding="utf-8")
            torch.save(model.state_dict(), out_dir / "last_model.pt")
            log(f"模型与标签已保存至: {out_dir}")
        except Exception as save_exc:
            log(f"保存模型时出错: {save_exc}")
        # 释放 GPU 显存，避免残留占用影响下次训练
        if device.type == "cuda":
            torch.cuda.empty_cache()

    if interrupted:
        # 写中断提示文件，供 GUI「从中断处续训」读取断点信息
        if HAS_MODEL_LIBRARY:
            try:
                (model_library.MODEL_LIBRARY_DIR / "last_interrupted.json").write_text(
                    json.dumps({
                        "out_dir": str(out_dir),
                        "model_name": out_dir.name,
                        "completed_epoch": trained_epochs,
                        "metric_name": select_metric,
                        "best_metric": best_metric,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
        log(f"训练提前结束。已完成 {trained_epochs} 轮，"
            f"最佳 {METRIC_CN_NAMES.get(select_metric, select_metric)}={best_metric}")
        log(f"断点已保存，可续训：--resume \"{out_dir}\"")
        return 130  # 128+SIGINT，标识为中断退出（中断/异常时不注册入库）

    # ---- 正常结束（含早停）：用最佳模型做完整评估，再写元信息并注册入库 ----
    end_note = "（早停）" if early_stopped else ""
    log(f"训练正常结束{end_note}。实际训练 {trained_epochs} 轮，"
        f"最佳 {METRIC_CN_NAMES.get(select_metric, select_metric)}={best_metric}")

    # 载入最佳模型，用于验证集与检测集的完整评估（生成评估报告）
    best_path = out_dir / "best_model.pt"
    if best_path.is_file():
        try:
            model.load_state_dict(torch.load(str(best_path), map_location="cpu",
                                             weights_only=True))
            log("已载入最佳模型用于结束评估。")
        except Exception as exc:
            log(f"[警告] 最佳模型载入失败，改用当前模型评估：{exc}")

    evaluation_report = {
        "metric_name": select_metric,
        "best_metric": best_metric,
        "architecture": architecture,
        "class_count": len(classes),
        "epochs": trained_epochs,
        "evaluated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    val_full = None
    try:
        val_full = evaluate_model(model, val_loader, criterion, device,
                                  len(classes), collect_predictions=True)
        evaluation_report["val"] = {k: v for k, v in val_full.items()
                                    if k not in ("labels", "preds")}
        log(f"验证集评估：top1={val_full['top1_acc']:.4f} "
            f"top5={val_full['top5_acc']:.4f} macro_f1={val_full['macro_f1']:.4f} "
            f"val_loss={val_full['val_loss']:.4f}")
    except Exception as exc:
        log(f"[警告] 验证集评估失败：{exc}")
        val_full = None
    test_full = None
    if counts.get("test", 0) > 0:
        try:
            test_full = evaluate_model(model, test_loader, criterion, device,
                                       len(classes), collect_predictions=True)
            evaluation_report["test"] = {k: v for k, v in test_full.items()
                                         if k not in ("labels", "preds")}
            log(f"检测集评估：top1={test_full['top1_acc']:.4f} "
                f"top5={test_full['top5_acc']:.4f} macro_f1={test_full['macro_f1']:.4f} "
                f"val_loss={test_full['val_loss']:.4f}")
        except Exception as exc:
            log(f"[警告] 检测集评估失败：{exc}")
            test_full = None
    else:
        log("检测集为空，跳过检测集评估（如需该指标，请为数据划分 test 集）。")

    # 每类明细报表：检测集优先，无检测集时用验证集
    detail_metrics = test_full or val_full
    detail_source = "test" if test_full else ("val" if val_full else None)
    if detail_metrics and detail_source:
        try:
            rows = per_class_metrics(detail_metrics["labels"],
                                     detail_metrics["preds"], classes)
            csv_lines = ["class,index,precision,recall,f1,support"]
            for row in rows:
                csv_lines.append(
                    f"\"{row['class']}\",{row['index']},{row['precision']},"
                    f"{row['recall']},{row['f1']},{row['support']}")
            (out_dir / "per_class_metrics.csv").write_text(
                "\n".join(csv_lines), encoding="utf-8-sig")
            evaluation_report["per_class_source"] = detail_source
            log(f"每类指标明细已保存: {out_dir / 'per_class_metrics.csv'}"
                f"（基于 {detail_source} 集）")
        except Exception as exc:
            log(f"[警告] 每类指标写入失败：{exc}")

    try:
        (out_dir / "evaluation_report.json").write_text(
            json.dumps(evaluation_report, ensure_ascii=False, indent=2),
            encoding="utf-8")
        log(f"评估报告已保存: {out_dir / 'evaluation_report.json'}")
    except Exception as exc:
        log(f"[警告] 评估报告写入失败：{exc}")

    def _metric_set(src) -> dict | None:
        """把评估结果裁剪为入库用的指标集合（Top-1/Top-5/宏F1/损失）。"""
        if not src:
            return None
        return {"top1_acc": src["top1_acc"], "top5_acc": src["top5_acc"],
                "macro_f1": src["macro_f1"], "val_loss": src["val_loss"]}

    if mode:
        # 仅模型库新流程写 meta.json（显式 --out-dir 时保持旧产物结构）
        if register_after_train:
            meta = {
                "class_count": len(classes),
                "architecture": architecture,
                "base_model": args.base_model,
                "metric_name": select_metric,
                "best_metric": best_metric,
                "best_val_acc": (val_full or {}).get("top1_acc", best_metric),
                "metrics": _metric_set(val_full),
                "test_metrics": _metric_set(test_full),
                "epochs": trained_epochs,   # 实际轮数（早停时小于设定轮数）
                "optimizer": optimizer_name,
                "patience": patience,
                "mode": mode,
                "batch": batch_size,
                "lr": lr,
                "img_size": args.img_size,
                "seed": args.seed,
                "strategy": strategy.get("label"),
                "label_smoothing": smoothing,
                "backbone_lr_mult": strategy.get("backbone_lr_mult"),
                "scheduler": sched_note,
                "split_mode": split_report.get("mode") if split_report else "existing",
                "split_ratio": split_report.get("ratio") if split_report else None,
                "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data_root": str(data_root),
            }
            try:
                (out_dir / "meta.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as meta_exc:
                log(f"[警告] meta.json 写入失败: {meta_exc}")
        # 注册入库：正常结束才注册；失败只警告不中断，训练产物已保存不受影响
        try:
            preview_path = None
            if generate_preview and preview_samples:
                preview_path = model_library.make_preview(
                    preview_samples, out_dir / "preview.jpg")
            # 入库主指标数值与 metric_name 保持一致（优先取结束评估的验证集结果）
            accuracy_value = (val_full or {}).get(select_metric, best_metric)
            record = model_library.register_model(
                out_dir, class_count=len(classes), accuracy=accuracy_value,
                mode=mode, source="本机训练", preview_path=preview_path,
                architecture=architecture, metric_name=select_metric,
                metrics=_metric_set(val_full))
            log(f"模型已注册入库: {record.get('name')}"
                f"（模型库目录: {model_library.MODEL_LIBRARY_DIR}）")
        except Exception as reg_exc:
            log(f"[警告] 模型注册入库失败（训练产物不受影响）: {reg_exc}")
    return 0


if __name__ == "__main__":
    # Windows 多进程 DataLoader 会重新导入本模块，必须加守护避免重复执行训练
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    sys.exit(main())
