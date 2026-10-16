#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 模块：鸟类图像分类模型训练脚本（可被 GUI 启动并实时显示终端进程）
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-27

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
_v4b2b067f = ("train", "val", "test")
_vaa115322 = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# 模型输出根目录（相对脚本所在目录，避免硬编码盘符，也不污染数据集目录）
_vfca465fc = Path(__file__).resolve().parent
_vb4b585b9 = _vfca465fc / "data" / "model_output"

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


# ---- 混合精度 API 兼容层：优先 torch>=2.3 的 torch.amp 新接口，
# 旧版环境回退 torch.cuda.amp（已弃用但仍可用），对调用方统一封装 ----
if HAS_TORCH and hasattr(torch.amp, "autocast") and hasattr(torch.amp, "GradScaler"):
    def _amp_autocast(enabled: bool):
        return torch.amp.autocast("cuda", enabled=enabled)

    def _amp_grad_scaler(enabled: bool):
        return torch.amp.GradScaler("cuda", enabled=enabled)
elif HAS_TORCH and hasattr(torch.cuda, "amp"):
    def _amp_autocast(enabled: bool):
        return torch.cuda.amp.autocast(enabled=enabled)

    def _amp_grad_scaler(enabled: bool):
        return torch.cuda.amp.GradScaler(enabled=enabled)
else:
    _amp_autocast = None
    _amp_grad_scaler = None


# 日志文件路径（main() 确定输出目录后设置；None 表示仅打印到标准输出）
_vb8844621: Path | None = None


def _vaf3a6814(line: str) -> None:
    print(line, flush=True)
    path = _vb8844621
    if path is None:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _v7552da09(msg: str) -> None:
    _vaf3a6814(f"[{datetime.now():%H:%M:%S}] {msg}")


def _vccf31420(payload: dict) -> None:
    try:
        body = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    _vaf3a6814("[METRIC] " + body)


# ============================================================
# 设备资源探测辅助（Task 2：适应模式 / 高速模式批大小推导）
# ============================================================

def _v676987b6() -> int | None:
    if not (HAS_TORCH and torch.cuda.is_available()):
        return None
    try:
        return int(torch.cuda.get_device_properties(0).total_memory)
    except Exception:
        return None


def _v352bfdca() -> tuple[int, int] | None:
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


def _v1a5be7f8(path: Path) -> int:
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


def _v9919f9a3() -> bool:
    return bool(HAS_TORCH and torch.cuda.is_available()
                and _amp_autocast is not None
                and _amp_grad_scaler is not None)


def _v9f3ed460(mode: str, architecture: str = "resnet50",
                      amp: bool = True) -> tuple[int, list[str]]:
    lines: list[str] = []
    gpu_mem = _v676987b6()
    if gpu_mem is None:
        lines.append("未检测到 GPU，按 CPU 安全参数训练，速度较慢")
        lines.append("CPU 安全回退: batch=8, workers=0")
        return 8, lines

    # 每样本显存估算按架构区分；FP32 训练时占用约为混合精度的 2 倍。
    # 实测校准（RTX 4060 / torch 2.13 / 224px）：ResNet50+AMP 峰值约 42~45MB/样本
    # （含激活+梯度+优化器状态），ViT 注意力占用更高约 70MB/样本
    arch_key = str(architecture or "resnet50")
    per_sample_gpu_bytes = 45 * 1024 * 1024        # ResNet 系（resnet50/resnest101）
    if arch_key.startswith("vit"):
        per_sample_gpu_bytes = 70 * 1024 * 1024    # ViT 注意力图占用更高
    if not amp:
        per_sample_gpu_bytes *= 2
    sample_mb = per_sample_gpu_bytes // 1024 // 1024
    lines.append(f"GPU 显存: {gpu_mem / 1024 ** 3:.2f} GB"
                 f"（架构 {arch_key}，混合精度={'开' if amp else '关'}，"
                 f"每样本按 {sample_mb}MB 估算）")
    mem_info = _v352bfdca()
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


def _v25be5372(mode: str | None) -> tuple[int, list[str]]:
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
    # 防止 Windows 下多进程同时加载 torch 触发 OpenBLAS/系统内存分配失败。
    # 进程总量按三个 DataLoader 常驻池计算：训练池 w 个 + 验证/检测池各 min(w, 2) 个
    # （评估加载器每轮仅迭代一次，worker 数上限 2，避免与训练池同规模常驻翻倍）
    mem_info = _v352bfdca()
    if mem_info is not None:
        _, avail_mem = mem_info
        per_worker_bytes = 800 * 1024 * 1024
        total_cap = max(1, int(avail_mem * 0.6 // per_worker_bytes))

        def _pool_total(w: int) -> int:
            return w + 2 * min(w, 2)

        if _pool_total(workers) > total_cap:
            capped = workers
            while capped > 1 and _pool_total(capped) > total_cap:
                capped -= 1
            note += (f"；系统可用内存 {avail_mem / 1024 ** 3:.2f}GB，"
                     f"按三池进程总量上限 {total_cap} 限制到 {capped}")
            workers = capped
    return workers, [f"数据加载进程: {note}，得 workers={workers}"
                     f"（验证/检测池各 min(workers, 2)）"]


# 批大小实测探测的显存预算比例：探测用启发式卷积算法（关闭 cudnn 自动寻优）
# 测得的峰值，余量留给寻优工作区、WDDM 桌面占用、显存碎片与评估阶段
_vb477c630 = 0.72


def _vf7442ed9(model, device, batch_size: int, img_size: int,
                     num_classes: int, amp_enabled: bool, logger):
    if device.type != "cuda":
        return batch_size
    gpu_total = _v676987b6() or 0
    if gpu_total <= 0:
        return batch_size
    # 优化器状态（Adam/AdamW 每参数 8 字节）探测时尚未分配，从预算中扣除
    param_bytes = sum(p.numel() * p.element_size() for p in model.parameters())
    budget = int(gpu_total * _vb477c630) - param_bytes * 2
    criterion = nn.CrossEntropyLoss()
    scaler = _amp_grad_scaler(enabled=amp_enabled)
    # 探测期关闭 cudnn 自动寻优：寻优会遍历多种算法工作区，污染峰值测量
    prev_benchmark = torch.backends.cudnn.benchmark
    torch.backends.cudnn.benchmark = False

    def _try_once(batch: int) -> bool:
        imgs = labels = outputs = loss = None
        try:
            torch.cuda.reset_peak_memory_stats()
            imgs = torch.randn(batch, 3, img_size, img_size, device=device)
            labels = torch.randint(0, max(1, num_classes), (batch,), device=device)
            with _amp_autocast(enabled=amp_enabled):
                outputs = model(imgs)
                loss = criterion(outputs, labels)
            scaler.scale(loss).backward()   # 仅分配梯度，不执行参数更新
            peak = torch.cuda.max_memory_allocated()
            if peak > budget:
                logger(f"[探测] batch={batch} 峰值显存 {peak / 1024 ** 3:.2f}GB "
                       f"超预算 {budget / 1024 ** 3:.2f}GB，减半重试")
                return False
            return True
        except RuntimeError as exc:
            # OOM 新版为 torch.cuda.OutOfMemoryError（RuntimeError 子类），
            # 旧版为普通 RuntimeError 按报错文本识别；其余运行时错误照常抛出
            if "out of memory" not in str(exc).lower():
                raise
            logger(f"[探测] batch={batch} 显存不足（OOM），减半重试")
            return False
        finally:
            model.zero_grad(set_to_none=True)
            imgs = labels = outputs = loss = None
            torch.cuda.empty_cache()

    try:
        tried = max(2, int(batch_size))
        last_failed = None
        while tried >= 2:
            if _try_once(tried):
                # 通过后与最近失败值取中点精调一次，缓解减半过冲
                if last_failed is not None:
                    mid = (tried + last_failed) // 2
                    if mid > tried and _try_once(mid):
                        tried = mid
                logger(f"[探测] 批大小实测通过: batch={tried}"
                       f"（预算 {budget / 1024 ** 3:.2f}GB）")
                return tried
            last_failed = tried
            tried //= 2
        logger("[告警] 批大小探测：减至 2 仍未通过显存实测，按 batch=2 继续（训练可能较慢）")
        return 2
    finally:
        torch.backends.cudnn.benchmark = prev_benchmark


def _v626bf2dd(base_model: str) -> str:
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


def _va92493b0(root: Path) -> Path:
    current = root
    for _ in range(4):
        if any((current / s).is_dir() for s in _v4b2b067f):
            return current
        children = [c for c in current.iterdir() if c.is_dir()]
        if len(children) != 1:
            return current
        current = children[0]
    return current


def _v415004a4(root: Path) -> dict[str, dict[str, list[Path]]]:
    grouped: dict[str, dict[str, list[Path]]] = {}
    for split in _v4b2b067f:
        split_dir = root / split
        if not split_dir.is_dir():
            continue
        for class_dir in sorted(split_dir.iterdir()):
            if not class_dir.is_dir():
                continue
            images = [p for p in class_dir.rglob("*")
                      if p.is_file() and p.suffix.lower() in _vaa115322]
            if images:
                grouped.setdefault(class_dir.name, {}).setdefault(split, []).extend(images)
    return grouped


class _v24b56252(Dataset):

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
            _v24b56252.corrupt_count[label] = \
                _v24b56252.corrupt_count.get(label, 0) + 1
            img = Image.new("RGB", (224, 224), (0, 0, 0))
        if self.transform:
            img = self.transform(img)
        return img, label


def _v31994b5a(grouped: dict[str, dict[str, list[Path]]], split: str,
                  label_to_idx: dict[str, int]) -> list[tuple[Path, int]]:
    samples: list[tuple[Path, int]] = []
    for class_name, splits in grouped.items():
        idx = label_to_idx[class_name]
        for path in splits.get(split, []):
            samples.append((path, idx))
    return samples


# ============================================================
# 分架构训练策略注册表（不同网络用不同优化策略）
# 说明：训练模式（adaptive/low/high/custom）负责「资源推导与批大小」等设备适配，
#       轮数/优化器/耐心值/学习率由用户自定义（GUI 提供推荐值）；
#       本注册表负责「怎么优化」——优化器、学习率分配、调度器、增强、标签平滑。
#       两者正交叠加：模式定资源，策略定手法。
# ============================================================
# ViT 系列（鸟类专域起点 / 通用 HuggingFace ViT）共用同一套迁移策略
_v2a73f25b = {
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

_v0e456636 = {
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
    "vit_bird_525": _v2a73f25b,
    "vit_hf": _v2a73f25b,
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


def _v8450d335(architecture) -> dict:
    return _v0e456636.get(str(architecture or "resnet50"),
                                      _v0e456636["resnet50"])


# ============================================================
# 评估指标注册表：架构 → 主指标与可选判据
# 说明：当前训练器实现图像分类，各分类架构主指标为 Top-1 准确率；
#       分割(mIoU)/检测(mAP) 条目为结构预留，待相应训练管线接入。
# ============================================================
_veeec11af = {
    "primary": "top1_acc",
    "options": ["top1_acc", "top5_acc", "macro_f1", "val_loss"],
}
_v19b8353d = {
    "resnet50": _veeec11af,
    "vit_bird_525": _veeec11af,
    "vit_hf": _veeec11af,
    "resnest101": _veeec11af,
    "segformer_seg": {"primary": "miou", "options": ["miou"]},   # 预留（当前不训练分割）
    "generic": _veeec11af,
}

# 判据方向：True = 越大越好，False = 越小越好
_v19dfe9f4 = {
    "top1_acc": True, "val_acc": True, "top5_acc": True,
    "macro_f1": True, "miou": True, "mAP": True, "val_loss": False,
}

# 指标中文名（日志与报告展示）
_v9cfd5ca0 = {
    "top1_acc": "Top-1 准确率", "val_acc": "Top-1 准确率",
    "top5_acc": "Top-5 准确率", "macro_f1": "宏平均F1",
    "val_loss": "验证损失", "miou": "mIoU", "mAP": "mAP",
}


def _v18745dc3(architecture) -> dict:
    return _v19b8353d.get(str(architecture or "resnet50"),
                                    _veeec11af)


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


def _v5c758620(text) -> tuple | None:
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


def _v8a5998b2(root: Path) -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = {}
    if any((root / s).is_dir() for s in _v4b2b067f):
        for split in _v4b2b067f:
            split_dir = root / split
            if not split_dir.is_dir():
                continue
            for class_dir in sorted(split_dir.iterdir()):
                if not class_dir.is_dir():
                    continue
                images = [p for p in class_dir.rglob("*")
                          if p.is_file() and p.suffix.lower() in _vaa115322]
                if images:
                    grouped.setdefault(class_dir.name, []).extend(images)
    else:
        for class_dir in sorted(root.iterdir()):
            if not class_dir.is_dir():
                continue
            images = [p for p in class_dir.rglob("*")
                      if p.is_file() and p.suffix.lower() in _vaa115322]
            if images:
                grouped[class_dir.name] = images
    return grouped


def _v237fd1e0(grouped_flat: dict[str, list[Path]],
                             ratio, seed: int) -> tuple:
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
        stats[class_name] = {s: len(assign[s]) for s in _v4b2b067f}
    return result, stats


def _v1a383fe9(model, architecture: str, base_lr: float,
                       weight_decay: float, backbone_lr_mult: float) -> tuple:
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


def _v893552ef(optimizer, strategy: dict, epochs: int) -> tuple:
    warmup = int(strategy.get("warmup_epochs") or 0)
    kind = strategy.get("scheduler")
    if kind != "cosine" and warmup <= 0:
        return None, "恒定学习率（无调度器）"
    import math as _math

    def lr_lambda(epoch_index: int) -> float:
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


def _v1787aa9e(strategy: dict, img_size: int) -> tuple:
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


def _v33f56a82(labels: list, preds: list, num_classes: int) -> float:
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


def _v7c500f91(labels: list, preds: list, class_names: list) -> list:
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


def _vb256abb6(model, loader, criterion, device, num_classes: int,
                   collect_predictions: bool = False) -> dict:
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
        "macro_f1": round(_v33f56a82(all_labels, all_preds, num_classes), 4),
        "count": total,
    }
    if collect_predictions:
        metrics["labels"] = all_labels
        metrics["preds"] = all_preds
    return metrics


def _v604d7147(out_dir: Path, payload: dict) -> None:
    target = out_dir / "resume_state.pth"
    tmp = out_dir / "resume_state.tmp"
    try:
        torch.save(payload, str(tmp))
        os.replace(str(tmp), str(target))
    except Exception as exc:  # 断点保存失败不影响训练主流程
        _v7552da09(f"[警告] 断点状态保存失败（不影响本次训练）：{exc}")
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _vd89f50d8(base_model: str, class_count: int, vit_dir=None):
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


def _v6f5fe057() -> int:
    parser = argparse.ArgumentParser(
        description="鸟类图像分类模型训练（ResNet50 微调，GPU 优先）")
    parser.add_argument("--data-dir", required=True, help="数据根目录（含 train/val/test）")
    parser.add_argument("--epochs", type=int, default=None,
                        help="训练轮数（默认 15；--mode 时未传则用模式预设值）")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="批大小（默认 32；adaptive/high/custom 模式未传时按设备自适应推导）")
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
        _v7552da09("缺少训练依赖，请先安装：")
        for m in missing:
            _v7552da09(f"  - {m}")
        _v7552da09("CUDA(GPU)版安装示例：pip install torch torchvision --index-url "
            "https://download.pytorch.org/whl/cu126")
        return 2

    # ---- Task 2：训练模式预设检查与参数归一化 ----
    # 取值优先级：命令行显式传值 > 模式预设值 > 旧默认值。
    mode = args.mode
    preset = None
    if mode:
        if not HAS_MODEL_LIBRARY:
            _v7552da09("训练模式依赖同目录的 model_library.py（共享模型库模块），未找到该文件。")
            return 2
        preset = model_library.TRAINING_MODE_PRESETS.get(mode)
        if preset is None:
            _v7552da09(f"未知的训练模式: {mode}（可选 adaptive/low/high/custom）")
            return 1
        # 说明：所有模式的轮数/优化器/耐心值/学习率均不再预设固定值，
        # 由用户显式传入；未传入时回退下方 pick 的内置默认值（各模式行为一致）

    def pick(explicit, preset_value, legacy_value):
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
    # 批大小单独归一化：adaptive/high/custom 预设的 batch=None 表示「按设备推导」，
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
        _v7552da09(f"参数错误：训练轮数必须为正整数，当前为 {epochs}。")
        return 1
    if batch_size is not None and batch_size <= 0:
        _v7552da09(f"参数错误：批大小必须为正整数，当前为 {batch_size}。")
        return 1
    if patience is not None and patience < 0:
        _v7552da09(f"参数错误：早停耐心值不能为负数，当前为 {patience}。")
        return 1
    if workers is not None and workers < 0:
        _v7552da09(f"参数错误：数据加载线程数不能为负数，当前为 {workers}。")
        return 1

    # ---- 自动划分比例归一化 ----
    split_ratio = _v5c758620(args.split_ratio) if args.split_ratio else None
    if args.auto_split:
        if split_ratio is None:
            split_ratio = (8.0, 1.0, 1.0)      # 未指定时用均衡标准级 8:1:1
        _v7552da09(f"自动划分比例（训练:验证:检测）= "
            f"{split_ratio[0]:g}:{split_ratio[1]:g}:{split_ratio[2]:g}")
    elif args.split_ratio:
        _v7552da09("提示：--split-ratio 需与 --auto-split 同时使用，本次已忽略该参数。")

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
                _v7552da09(f"[警告] 断点文件读取失败，将按全新训练执行：{exc}")
                resume_state = None
        else:
            _v7552da09(f"[警告] 断点目录缺少 resume_state.pth（{resume_file}），将按全新训练执行。")
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
                restored = _v5c758620(snapshot.get("split_ratio"))
                if restored:
                    split_ratio = restored
            _v7552da09(f"续训模式：读取断点（已完成 "
                f"{resume_state.get('completed_epoch', 0)} 轮，"
                f"最佳 {_v9cfd5ca0.get(resume_state.get('best_metric_name'), '指标')}"
                f"={resume_state.get('best_metric')}）")

    # ---- 扫描数据集 ----
    data_root = _va92493b0(Path(args.data_dir).resolve())
    if not data_root.exists() or not data_root.is_dir():
        _v7552da09(f"数据目录不存在或不是文件夹: {data_root}")
        return 1
    split_report = None
    if args.auto_split and split_ratio:
        flat = _v8a5998b2(data_root)
        if not flat:
            _v7552da09(f"未在 {data_root} 下找到任何类别目录与图片。")
            return 1
        grouped, split_stats = _v237fd1e0(flat, split_ratio, args.seed)
        split_report = {
            "mode": "auto_split",
            "ratio": list(split_ratio),
            "seed": args.seed,
            "data_root": str(data_root),
            "per_class": split_stats,
        }
        _v7552da09(f"已按 {split_ratio[0]:g}:{split_ratio[1]:g}:{split_ratio[2]:g} "
            f"完成类内自适应分层划分（含小类保底）。")
    else:
        grouped = _v415004a4(data_root)
        if not grouped:
            _v7552da09(f"未在 {data_root} 下找到 train/val/test 划分目录或任何图片。")
            return 1

    classes = sorted(grouped)
    label_to_idx = {name: i for i, name in enumerate(classes)}
    idx_to_label = {i: name for name, i in label_to_idx.items()}
    counts = {s: sum(len(v.get(s, [])) for v in grouped.values()) for s in _v4b2b067f}
    _v7552da09(f"数据根目录: {data_root}")
    _v7552da09(f"类别数: {len(classes)}")
    for s in _v4b2b067f:
        _v7552da09(f"  {s}: {counts[s]} 张")
    if args.list_only:
        _v7552da09("（list-only 模式：仅统计，不训练）")
        return 0
    if counts["train"] == 0:
        _v7552da09("训练集为 0 张，无法训练。")
        return 1
    # 验证集为空会导致评估指标全为 0（损失/准确率分母被兜底为 1），
    # 且无法驱动早停与最佳模型选择，属致命数据问题，必须明确报错并给出解决路径，
    # 避免静默训练出无效产物、让用户误判为公式错误。
    if counts["val"] == 0:
        _v7552da09("[错误] 验证集为 0 张，无法计算评估指标（损失/Top-1/Top-5/宏F1 会显示为 0）。")
        _v7552da09("       请二选一解决：")
        _v7552da09("       1) 在数据目录下补充 val 子目录（结构：val/类别名/图片）；")
        _v7552da09("       2) 改用自动划分（--auto-split，按比例从训练集切出验证/检测集）。")
        return 1
    # 检测集为空不影响训练主流程，仅结束评估无意义，给出警告即可。
    if counts["test"] == 0:
        _v7552da09("[警告] 检测集为 0 张，训练结束时的检测集评估指标将显示为 0。")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type == "cuda":
        # 输入尺寸固定为 224×224，开启 cudnn 自动寻优，让卷积选择最快算法
        torch.backends.cudnn.benchmark = True

    # ---- 可复现性：固定随机种子 ----
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(args.seed)

    _v7552da09(f"计算设备: {device}" +
        (f"（GPU: {torch.cuda.get_device_name(0)}）" if device.type == "cuda" else ""))

    # ---- Task 2：批大小自适应推导 / workers 归一化 / AMP 开关归一化 ----
    # AMP 开关：命令行 --amp 显式开启 > 模式预设（True/"auto" 在有 GPU 时启用）> 关闭
    # 注意：AMP 需在批大小推导之前确定（FP32 与混合精度的每样本显存估算不同）
    has_gpu = device.type == "cuda"
    amp_enabled = False
    if args.amp:
        amp_enabled = has_gpu and _v9919f9a3()
    elif mode and preset["amp"] in (True, "auto"):
        amp_enabled = has_gpu and _v9919f9a3()
    if args.amp and not amp_enabled:
        _v7552da09("提示：当前无可用 GPU 或环境不支持混合精度，已忽略 --amp（AMP 仅在 CUDA 上生效）。")
    elif mode and not args.amp and preset["amp"] is True and not amp_enabled:
        _v7552da09("提示：当前无可用 GPU，已忽略该模式的混合精度（AMP）预设。")

    # 批大小：按模式 + 架构 + AMP 状态推导（命令行显式 --batch-size 最优先）
    # batch_auto_derived 标记本值是否为自动推导（显式指定/续训回填时为 False），
    # 用于后续决定是否需要做显存实测探测
    batch_auto_derived = False
    if batch_size is None:
        # 仅 adaptive/high 预设的 batch 为 None（表示按设备资源自适应推导）
        arch_hint = _v626bf2dd(args.base_model)
        batch_size, probe_lines = _v9f3ed460(mode, arch_hint, amp_enabled)
        batch_auto_derived = True
        for line in probe_lines:
            _v7552da09(line)

    # workers：命令行显式正数最优先；未指定或 <=0 时按模式自动推导
    # （脚本有 __main__ 守护，Windows 下多进程加载安全；纯 CPU 训练保持主进程加载）
    if workers is None or workers <= 0:
        if device.type != "cuda":
            workers = 0
        else:
            workers, worker_lines = _v25be5372(mode)
            for line in worker_lines:
                _v7552da09(line)

    # ---- 模型：按基础模型选择构建（ImageNet ResNet50 / 鸟类ViT / 库内微调） ----
    try:
        model, architecture, base_note = _vd89f50d8(
            args.base_model, len(classes), vit_dir=args.vit_dir)
    except RuntimeError as exc:
        _v7552da09(f"[错误] 基础模型加载失败：{exc}")
        return 1
    model.to(device)
    _v7552da09(f"基础模型: {base_note}")
    _v7552da09(f"网络架构: {architecture}，分类头类别数: {len(classes)}")

    # ---- 分架构训练策略：优化器结构/学习率分配/调度器/增强/标签平滑 ----
    strategy = _v8450d335(architecture)
    metrics_cfg = _v18745dc3(architecture)
    select_metric = args.select_metric or metrics_cfg.get("primary") or "top1_acc"
    higher_better = _v19dfe9f4.get(select_metric, True)
    # 策略里的优化器仅在用户/模式未显式指定优化器时生效（显式指定优先）
    if not args.optimizer and not (mode and preset and preset.get("optimizer")):
        optimizer_name = strategy.get("optimizer", optimizer_name)
    _v7552da09(f"训练策略: {strategy.get('label')} —— {strategy.get('description')}")
    _v7552da09(f"最佳模型判据: {_v9cfd5ca0.get(select_metric, select_metric)}"
        f"（{'越大越好' if higher_better else '越小越好'}）")

    # ---- 断点续训：回填训练超参（优化器/学习率/批大小/耐心值/AMP/workers）----
    # GUI 续训仅传 --epochs，若不回填，patience/batch/amp 等会回退默认值；
    # 且优化器类型一旦变化（如 SGD→AdamW），优化器状态恢复会失败并静默重置为全新训练
    if isinstance(resume_state, dict):
        snap = resume_state.get("snapshot") or {}
        restored_params = []
        if snap.get("optimizer") and snap["optimizer"] != optimizer_name:
            optimizer_name = snap["optimizer"]
            restored_params.append(f"optimizer={optimizer_name}")
        if snap.get("lr"):
            lr = float(snap["lr"])
            restored_params.append(f"lr={lr:g}")
        if snap.get("batch_size"):
            batch_size = int(snap["batch_size"])
            batch_auto_derived = False
            restored_params.append(f"batch={batch_size}")
        if snap.get("patience"):
            patience = int(snap["patience"])
            restored_params.append(f"patience={patience}")
        if snap.get("amp_enabled") is not None:
            amp_enabled = bool(snap["amp_enabled"])
            restored_params.append(f"amp={'开启' if amp_enabled else '关闭'}")
        if snap.get("workers") is not None:
            workers = int(snap["workers"])
            restored_params.append(f"workers={workers}")
        if restored_params:
            _v7552da09(f"续训回填：{'，'.join(restored_params)}")

    # ---- 批大小实测探测：估算仅作起点，虚拟前向+反向实测显存后最终确定 ----
    # 仅 GPU + 自动推导 + 全新训练时探测；Windows WDDM 下显存不足不报 OOM
    # 而是静默换页到系统内存（GPU 利用率 100% 但速度骤降），故探测同时检查
    # OOM 异常与实际峰值显存两道防线（见 probe_batch_size）
    if batch_auto_derived and device.type == "cuda" and resume_state is None:
        batch_size = _vf7442ed9(model, device, batch_size, args.img_size,
                                      len(classes), amp_enabled, _v7552da09)

    # ---- 数据增强与加载（增强强度由策略决定） ----
    img_size = args.img_size
    train_tf, eval_tf = _v1787aa9e(strategy, img_size)

    train_set = _v24b56252(_v31994b5a(grouped, "train", label_to_idx), train_tf)
    val_set = _v24b56252(_v31994b5a(grouped, "val", label_to_idx), eval_tf)
    test_set = _v24b56252(_v31994b5a(grouped, "test", label_to_idx), eval_tf)
    # Windows 多进程 DataLoader 需 if __name__ == "__main__" 守护，故此处直接提示
    if workers > 0 and sys.platform.startswith("win"):
        _v7552da09("提示：Windows 下多线程数据加载可能不稳定，如遇异常请将线程设为 0 重试。")
    pin = (device.type == "cuda")
    # 训练加载器：多进程时常驻并预取下一批，避免每轮重建进程与 GPU 空等数据
    train_extra = {"persistent_workers": True, "prefetch_factor": 4} \
        if workers > 0 else {}
    # 评估加载器（验证/测试）：评估吞吐不是瓶颈，进程数降为 min(workers, 2)；
    # 训练池 + 两个评估池的总量已由 derive_auto_workers 按可用内存统一约束
    eval_workers = min(workers, 2)
    eval_extra = {"persistent_workers": True, "prefetch_factor": 4} \
        if eval_workers > 0 else {}
    train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True,
                              num_workers=workers, pin_memory=pin, **train_extra)
    val_loader = DataLoader(val_set, batch_size=batch_size, shuffle=False,
                            num_workers=eval_workers, pin_memory=pin, **eval_extra)
    test_loader = DataLoader(test_set, batch_size=batch_size, shuffle=False,
                             num_workers=eval_workers, pin_memory=pin, **eval_extra)

    # 损失函数：按策略启用标签平滑（0 表示关闭，等价原 CrossEntropyLoss）
    smoothing = float(strategy.get("label_smoothing") or 0.0)
    criterion = nn.CrossEntropyLoss(label_smoothing=smoothing)

    # 参数组：按策略拆分骨干/分类头（差异化学习率）
    param_groups, groups_note = _v1a383fe9(
        model, architecture, lr, float(strategy.get("weight_decay") or 0.0),
        float(strategy.get("backbone_lr_mult") or 1.0))
    _v7552da09(f"参数组: {groups_note}")
    if optimizer_name == "sgd":
        optimizer = torch.optim.SGD(param_groups, lr=lr, momentum=0.9)
    elif optimizer_name == "adam":
        optimizer = torch.optim.Adam(param_groups, lr=lr)
    else:
        optimizer = torch.optim.AdamW(param_groups, lr=lr)
    # 学习率调度器：线性升温 + 余弦退火（按策略）
    scheduler, sched_note = _v893552ef(optimizer, strategy, epochs)
    _v7552da09(f"学习率调度: {sched_note}")
    # 混合精度梯度缩放器（enabled=False 时全部操作自动退化为普通前向/反向；
    # 经兼容层创建：新版 torch.amp.GradScaler("cuda")，旧版回退 torch.cuda.amp）
    scaler = _amp_grad_scaler(enabled=amp_enabled)

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
        out_dir = Path(args.out_dir).resolve() if args.out_dir else _vb4b585b9
        # 显式指定输出目录 + 模式化训练：仍注册入库（预览图不生成，保持旧产物结构）
        register_after_train = bool(mode)

    # 磁盘安全检查：模式化训练时输出磁盘剩余不足 5GB 拒绝训练（安全优先）
    if mode:
        disk_free = _v1a5be7f8(out_dir)
        _v7552da09(f"输出磁盘剩余空间: {disk_free / 1024 ** 3:.2f} GB")
        if disk_free < 5 * 1024 ** 3:
            _v7552da09("[错误] 输出磁盘剩余空间不足 5GB，为避免训练中断导致产物损坏，已拒绝训练。"
                "请清理磁盘空间后重试。")
            return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 启用训练日志文件（双写 stdout + 文件；续训时接续同一日志，新训练重置） ----
    global _vb8844621
    _vb8844621 = out_dir / "train_log.txt"
    if not is_resume:
        try:
            _vb8844621.write_text("", encoding="utf-8")
        except OSError as exc:
            _v7552da09(f"[警告] 训练日志文件创建失败（不影响训练）：{exc}")
            _vb8844621 = None
    _v7552da09(f"训练日志文件: {_vb8844621 if _vb8844621 else '（未启用）'}")

    # 自动划分报告落盘（在内存中完成划分，不移动/复制用户原始文件）
    if split_report is not None:
        try:
            (out_dir / "split_report.json").write_text(
                json.dumps(split_report, ensure_ascii=False, indent=2),
                encoding="utf-8")
            _v7552da09(f"数据划分报告已保存: {out_dir / 'split_report.json'}")
        except OSError as exc:
            _v7552da09(f"[警告] 数据划分报告写入失败（不影响训练）：{exc}")

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
            # AMP 梯度缩放器状态恢复（旧断点无此字段时跳过，保持初始缩放因子）
            if amp_enabled and resume_state.get("scaler_state_dict"):
                scaler.load_state_dict(resume_state["scaler_state_dict"])
            start_epoch = int(resume_state.get("completed_epoch", 0)) + 1
            best_metric = resume_state.get("best_metric")
            no_improve = int(resume_state.get("no_improve", 0))
            history = list(resume_state.get("history") or [])
            _v7552da09(f"已恢复断点状态：从第 {start_epoch} 轮继续训练"
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
            _v7552da09(f"[警告] 断点状态恢复失败，将按全新训练执行：{exc}")
            is_resume = False
            start_epoch, best_metric, no_improve, history = 1, None, 0, []
    if start_epoch > epochs:
        _v7552da09(f"已完成轮数（{start_epoch - 1}）已达到目标轮数 {epochs}，直接进入结束评估。")

    # 启动信息：模式、最终生效参数、输出目录（全中文，供 GUI 与用户确认）
    mode_label = model_library.TRAINING_MODE_PRESETS[mode]["label"] if mode \
        else "默认模式（经典参数，不入库）"
    _v7552da09(f"训练模式: {mode_label}")
    _v7552da09(f"生效参数: epochs={epochs}, optimizer={optimizer_name}, "
        f"patience={patience if patience else '无'}, batch={batch_size}, lr={lr}, "
        f"amp={'开启' if amp_enabled else '关闭'}, workers={workers}, seed={args.seed}")
    _v7552da09(f"输出目录: {out_dir}")
    _v7552da09(f"开始训练：目标轮数 {epochs}，共 {len(classes)} 类")

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
        # 训练超参一并入快照：续训时回填，防止 GUI 只传 --epochs 时参数回退默认值
        "optimizer": optimizer_name,
        "lr": lr,
        "batch_size": batch_size,
        "patience": patience,
        "amp_enabled": amp_enabled,
        "workers": workers,
    }

    def report_corrupt(stage: str) -> None:
        if not _v24b56252.corrupt_count:
            return
        total_corrupt = sum(_v24b56252.corrupt_count.values())
        _v7552da09(f"[告警] {stage} 共遇到 {total_corrupt} 张损坏/无法读取的图片，已用占位图跳过：")
        for label_idx, cnt in sorted(
                _v24b56252.corrupt_count.items(), key=lambda x: -x[1])[:10]:
            _v7552da09(f"    类别「{idx_to_label.get(label_idx, label_idx)}」: {cnt} 张")

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
                # 混合精度前向：autocast 关闭时等价普通前向（经兼容层创建）
                with _amp_autocast(enabled=amp_enabled):
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
                    _v7552da09(f"  [Epoch {epoch}/{epochs}] "
                        f"batch {i}/{len(train_loader)} "
                        f"loss={running_loss_t.item() / max(1, total):.4f} "
                        f"acc={correct_t.item() / max(1, total):.4f}")
            report_corrupt(f"Epoch {epoch} 训练")
            train_loss = running_loss_t.item() / max(1, total)
            train_acc = correct_t.item() / max(1, total)

            # ---- 验证：一次性计算全部指标（损失 / Top-1 / Top-5 / 宏F1） ----
            metrics = _vb256abb6(model, val_loader, criterion, device, len(classes))
            report_corrupt(f"Epoch {epoch} 验证")
            dt = time.time() - t0
            current_lr = optimizer.param_groups[0]["lr"]
            _v7552da09(f"[Epoch {epoch}/{epochs}] 完成：train_loss={train_loss:.4f} "
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
                _v7552da09(f"  已保存最佳模型（{_v9cfd5ca0.get(select_metric, select_metric)}"
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
            _vccf31420({
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
            _v604d7147(out_dir, {
                "completed_epoch": epoch,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "scheduler_state_dict": scheduler.state_dict() if scheduler else None,
                # AMP 梯度缩放器状态：续训恢复，防止中断前后缩放因子不一致
                "scaler_state_dict": scaler.state_dict() if amp_enabled else None,
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
                _v7552da09(f"早停触发：{_v9cfd5ca0.get(select_metric, select_metric)}"
                    f"连续 {patience} 轮未提升，提前结束训练。")
                break
    except KeyboardInterrupt:
        _v7552da09("收到中断信号（Ctrl+C / 停止训练），正在保存当前模型后退出…")
        interrupted = True
    except Exception:
        _v7552da09("训练过程发生异常，保存当前模型后退出：")
        traceback.print_exc()
        interrupted = True
    finally:
        # 统一兜底：无论正常/中断/异常都保存当前进度与标签，避免白跑
        try:
            (out_dir / "labels.json").write_text(
                json.dumps(label_to_idx, ensure_ascii=False, indent=2),
                encoding="utf-8")
            torch.save(model.state_dict(), out_dir / "last_model.pt")
            _v7552da09(f"模型与标签已保存至: {out_dir}")
        except Exception as save_exc:
            _v7552da09(f"保存模型时出错: {save_exc}")
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
        _v7552da09(f"训练提前结束。已完成 {trained_epochs} 轮，"
            f"最佳 {_v9cfd5ca0.get(select_metric, select_metric)}={best_metric}")
        _v7552da09(f"断点已保存，可续训：--resume \"{out_dir}\"")
        return 130  # 128+SIGINT，标识为中断退出（中断/异常时不注册入库）

    # ---- 正常结束（含早停）：用最佳模型做完整评估，再写元信息并注册入库 ----
    end_note = "（早停）" if early_stopped else ""
    _v7552da09(f"训练正常结束{end_note}。实际训练 {trained_epochs} 轮，"
        f"最佳 {_v9cfd5ca0.get(select_metric, select_metric)}={best_metric}")

    # 载入最佳模型，用于验证集与检测集的完整评估（生成评估报告）
    best_path = out_dir / "best_model.pt"
    if best_path.is_file():
        try:
            model.load_state_dict(torch.load(str(best_path), map_location="cpu",
                                             weights_only=True))
            _v7552da09("已载入最佳模型用于结束评估。")
        except Exception as exc:
            _v7552da09(f"[警告] 最佳模型载入失败，改用当前模型评估：{exc}")

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
        val_full = _vb256abb6(model, val_loader, criterion, device,
                                  len(classes), collect_predictions=True)
        evaluation_report["val"] = {k: v for k, v in val_full.items()
                                    if k not in ("labels", "preds")}
        _v7552da09(f"验证集评估：top1={val_full['top1_acc']:.4f} "
            f"top5={val_full['top5_acc']:.4f} macro_f1={val_full['macro_f1']:.4f} "
            f"val_loss={val_full['val_loss']:.4f}")
    except Exception as exc:
        _v7552da09(f"[警告] 验证集评估失败：{exc}")
        val_full = None
    test_full = None
    if counts.get("test", 0) > 0:
        try:
            test_full = _vb256abb6(model, test_loader, criterion, device,
                                       len(classes), collect_predictions=True)
            evaluation_report["test"] = {k: v for k, v in test_full.items()
                                         if k not in ("labels", "preds")}
            _v7552da09(f"检测集评估：top1={test_full['top1_acc']:.4f} "
                f"top5={test_full['top5_acc']:.4f} macro_f1={test_full['macro_f1']:.4f} "
                f"val_loss={test_full['val_loss']:.4f}")
        except Exception as exc:
            _v7552da09(f"[警告] 检测集评估失败：{exc}")
            test_full = None
    else:
        _v7552da09("检测集为空，跳过检测集评估（如需该指标，请为数据划分 test 集）。")

    # 每类明细报表：检测集优先，无检测集时用验证集
    detail_metrics = test_full or val_full
    detail_source = "test" if test_full else ("val" if val_full else None)
    if detail_metrics and detail_source:
        try:
            rows = _v7c500f91(detail_metrics["labels"],
                                     detail_metrics["preds"], classes)
            csv_lines = ["class,index,precision,recall,f1,support"]
            for row in rows:
                csv_lines.append(
                    f"\"{row['class']}\",{row['index']},{row['precision']},"
                    f"{row['recall']},{row['f1']},{row['support']}")
            (out_dir / "per_class_metrics.csv").write_text(
                "\n".join(csv_lines), encoding="utf-8-sig")
            evaluation_report["per_class_source"] = detail_source
            _v7552da09(f"每类指标明细已保存: {out_dir / '_v7c500f91.csv'}"
                f"（基于 {detail_source} 集）")
        except Exception as exc:
            _v7552da09(f"[警告] 每类指标写入失败：{exc}")

    try:
        (out_dir / "evaluation_report.json").write_text(
            json.dumps(evaluation_report, ensure_ascii=False, indent=2),
            encoding="utf-8")
        _v7552da09(f"评估报告已保存: {out_dir / 'evaluation_report.json'}")
    except Exception as exc:
        _v7552da09(f"[警告] 评估报告写入失败：{exc}")

    def _metric_set(src) -> dict | None:
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
                _v7552da09(f"[警告] meta.json 写入失败: {meta_exc}")
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
            _v7552da09(f"模型已注册入库: {record.get('name')}"
                f"（模型库目录: {model_library.MODEL_LIBRARY_DIR}）")
        except Exception as reg_exc:
            _v7552da09(f"[警告] 模型注册入库失败（训练产物不受影响）: {reg_exc}")
    return 0


if __name__ == "__main__":
    # Windows 多进程 DataLoader 会重新导入本模块，必须加守护避免重复执行训练
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    sys.exit(_v6f5fe057())
