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


def log(msg: str) -> None:
    """向标准输出打印带时间戳的日志（GUI 子进程按行实时捕获）。"""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


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


def derive_auto_batch(mode: str) -> tuple[int, list[str]]:
    """按设备资源自适应推导批大小（adaptive/high 共用，仅显存预算比例不同）。

    推导规则（安全优先）：
        - 显存预算：adaptive 用显存的 55%，high 用 80%；每样本约按 12MB 估算
          （224px 输入的 ResNet50 微调经验值）；
        - 结果限制：adaptive 夹在 [4, 64]，high 夹在 [8, 128]；
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

    lines.append(f"GPU 显存: {gpu_mem / 1024 ** 3:.2f} GB")
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
    per_sample_gpu_bytes = 12 * 1024 * 1024   # 每样本约 12MB（224px ResNet50 估算）
    low_bound, high_bound = (4, 64) if mode == "adaptive" else (8, 128)
    batch_gpu = int(gpu_mem * budget_ratio // per_sample_gpu_bytes)
    batch_gpu = max(low_bound, min(high_bound, batch_gpu))
    batch = batch_gpu
    lines.append(f"显存预算: 按显存的 {budget_ratio:.0%} 推导，每样本约 12MB，"
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
    parser.add_argument("--amp", action="store_true",
                        help="启用混合精度训练（仅 GPU 生效；高速模式预设自动开启）")
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

    # ---- 扫描数据集 ----
    data_root = resolve_effective_root(Path(args.data_dir).resolve())
    if not data_root.exists() or not data_root.is_dir():
        log(f"数据目录不存在或不是文件夹: {data_root}")
        return 1
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

    # ---- 可复现性：固定随机种子 ----
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(args.seed)

    log(f"计算设备: {device}" +
        (f"（GPU: {torch.cuda.get_device_name(0)}）" if device.type == "cuda" else ""))

    # ---- Task 2：批大小自适应推导 / workers 归一化 / AMP 开关归一化 ----
    if batch_size is None:
        # 仅 adaptive/high 预设的 batch 为 None（表示按设备资源自适应推导）
        batch_size, probe_lines = derive_auto_batch(mode)
        for line in probe_lines:
            log(line)
    # workers：Windows 默认 0（多进程加载在 Windows 下不稳定）；无 GPU 回退时也用 0
    if workers is None:
        if mode and device.type != "cuda":
            workers = 0
        elif mode and sys.platform.startswith("win"):
            workers = 0
        else:
            workers = 4

    # AMP 开关：命令行 --amp 显式开启 > 模式预设（True/"auto" 在有 GPU 时启用）> 关闭
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

    # ---- 数据增强与加载 ----
    img_size = args.img_size
    train_tf = transforms.Compose([
        transforms.Resize((img_size, img_size)),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])
    eval_tf = transforms.Compose([
        transforms.Resize((img_size, img_size)),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])

    train_set = BirdDataset(build_samples(grouped, "train", label_to_idx), train_tf)
    val_set = BirdDataset(build_samples(grouped, "val", label_to_idx), eval_tf)
    # Windows 多进程 DataLoader 需 if __name__ == "__main__" 守护，故此处直接提示
    if workers > 0 and sys.platform.startswith("win"):
        log("提示：Windows 下多线程数据加载可能不稳定，如遇异常请将线程设为 0 重试。")
    train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True,
                              num_workers=workers, pin_memory=(device.type == "cuda"))
    val_loader = DataLoader(val_set, batch_size=batch_size, shuffle=False,
                            num_workers=workers, pin_memory=(device.type == "cuda"))

    # ---- 模型：微调预训练 ResNet50，替换末层为类别数 ----
    weights = models.ResNet50_Weights.DEFAULT
    model = models.resnet50(weights=weights)
    in_features = model.fc.in_features
    model.fc = nn.Linear(in_features, len(classes))
    model.to(device)

    criterion = nn.CrossEntropyLoss()
    # 按名称构建优化器（sgd 附带动量 0.9；adamw/adam 为标准 Adam 系）
    if optimizer_name == "sgd":
        optimizer = torch.optim.SGD(model.parameters(), lr=lr, momentum=0.9)
    elif optimizer_name == "adam":
        optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    else:
        optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    # 混合精度梯度缩放器（enabled=False 时全部操作自动退化为普通前向/反向）
    scaler = torch.cuda.amp.GradScaler(enabled=amp_enabled)

    # ---- 输出目录：模式化训练走共享模型库自动取号；否则沿用旧默认目录 ----
    register_after_train = False      # 训练正常结束后是否注册入库
    generate_preview = False          # 是否生成预览图（仅模型库新流程）
    if mode and not args.out_dir:
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

    # 训练开始前从训练集随机抽 4 张样图路径，供正常结束后拼接预览图
    preview_samples: list[Path] = []
    if generate_preview:
        train_paths = [p for p, _ in train_set.samples]
        if train_paths:
            preview_samples = random.sample(train_paths, min(4, len(train_paths)))

    best_val_acc = 0.0
    # 启动信息：模式、最终生效参数、输出目录（全中文，供 GUI 与用户确认）
    mode_label = model_library.TRAINING_MODE_PRESETS[mode]["label"] if mode \
        else "默认模式（经典参数，不入库）"
    log(f"训练模式: {mode_label}")
    log(f"生效参数: epochs={epochs}, optimizer={optimizer_name}, "
        f"patience={patience if patience else '无'}, batch={batch_size}, lr={lr}, "
        f"amp={'开启' if amp_enabled else '关闭'}, workers={workers}, seed={args.seed}")
    log(f"输出目录: {out_dir}")
    log(f"开始训练：目标轮数 {epochs}，共 {len(classes)} 类")

    def report_corrupt(stage: str) -> None:
        """汇总并打印损坏图片告警（按类别统计，最多列 10 类）。"""
        if not BirdDataset.corrupt_count:
            return
        total_corrupt = sum(BirdDataset.corrupt_count.values())
        log(f"[告警] {stage} 共遇到 {total_corrupt} 张损坏/无法读取的图片，已用占位图跳过：")
        for label_idx, cnt in sorted(
                BirdDataset.corrupt_count.items(), key=lambda x: -x[1])[:10]:
            log(f"    类别「{idx_to_label.get(label_idx, label_idx)}」: {cnt} 张")

    trained_epochs = 0   # 实际完成的轮数（早停时小于设定轮数）
    no_improve = 0       # 验证精度连续无提升的轮数（早停计数器）
    interrupted = False
    early_stopped = False
    try:
        for epoch in range(1, epochs + 1):
            trained_epochs = epoch
            model.train()
            running_loss, correct, total = 0.0, 0, 0
            t0 = time.time()
            # 日志降频：至少每 200 个 batch 或最后一个才打印一次，避免刷爆界面
            log_every = max(1, min(200, len(train_loader) // 5 or 1))
            for i, (imgs, labels) in enumerate(train_loader, 1):
                imgs, labels = imgs.to(device), labels.to(device)
                optimizer.zero_grad()
                # 混合精度前向：autocast 关闭时等价普通前向
                with torch.cuda.amp.autocast(enabled=amp_enabled):
                    outputs = model(imgs)
                    loss = criterion(outputs, labels)
                # 混合精度反向与参数更新：GradScaler 关闭时自动等价普通反向
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
                running_loss += loss.item() * imgs.size(0)
                correct += (outputs.argmax(1) == labels).sum().item()
                total += labels.size(0)
                if i % log_every == 0 or i == len(train_loader):
                    log(f"  [Epoch {epoch}/{epochs}] "
                        f"batch {i}/{len(train_loader)} "
                        f"loss={running_loss / max(1, total):.4f} "
                        f"acc={correct / max(1, total):.4f}")
            report_corrupt(f"Epoch {epoch} 训练")
            train_loss = running_loss / max(1, total)
            train_acc = correct / max(1, total)

            # ---- 验证 ----
            model.eval()
            val_loss, val_correct, val_total = 0.0, 0, 0
            with torch.no_grad():
                for imgs, labels in val_loader:
                    imgs, labels = imgs.to(device), labels.to(device)
                    outputs = model(imgs)
                    val_loss += criterion(outputs, labels).item() * imgs.size(0)
                    val_correct += (outputs.argmax(1) == labels).sum().item()
                    val_total += labels.size(0)
            report_corrupt(f"Epoch {epoch} 验证")
            val_loss /= max(1, val_total)
            val_acc = val_correct / max(1, val_total)
            dt = time.time() - t0
            log(f"[Epoch {epoch}/{epochs}] 完成：train_loss={train_loss:.4f} "
                f"train_acc={train_acc:.4f} val_loss={val_loss:.4f} val_acc={val_acc:.4f} "
                f"耗时 {dt:.1f}s")

            # 最佳模型保存：>= 持平也保存（保持旧语义）；早停计数按严格提升判定
            if val_acc >= best_val_acc:
                best_val_acc = val_acc
                torch.save(model.state_dict(), out_dir / "best_model.pt")
                log(f"  已保存最佳模型（val_acc={best_val_acc:.4f}）")
            # 早停计数：第一轮建立基线不计入；之后仅严格提升才重置计数
            if epoch == 1 or val_acc > best_val_acc:
                no_improve = 0
            else:
                no_improve += 1
            # 早停判断：连续 patience 轮无提升则提前结束（patience 为 0/None 时不启用）
            if patience and no_improve >= patience:
                early_stopped = True
                log(f"早停触发：验证精度连续 {patience} 轮未提升，提前结束训练。")
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
        log(f"训练提前结束。最佳验证准确率: {best_val_acc:.4f}")
        return 130  # 128+SIGINT，标识为中断退出（中断/异常时不注册入库）

    # ---- 正常结束（含早停）：写元信息、生成预览图并注册入库 ----
    end_note = "（早停）" if early_stopped else ""
    log(f"训练正常结束{end_note}。实际训练 {trained_epochs} 轮，"
        f"最佳验证准确率: {best_val_acc:.4f}")

    if mode:
        # 仅模型库新流程写 meta.json（显式 --out-dir 时保持旧产物结构）
        if register_after_train:
            meta = {
                "class_count": len(classes),
                "best_val_acc": best_val_acc,
                "epochs": trained_epochs,   # 实际轮数（早停时小于设定轮数）
                "optimizer": optimizer_name,
                "patience": patience,
                "mode": mode,
                "batch": batch_size,
                "lr": lr,
                "img_size": args.img_size,
                "seed": args.seed,
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
            record = model_library.register_model(
                out_dir, class_count=len(classes), accuracy=best_val_acc,
                mode=mode, source="本机训练", preview_path=preview_path)
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
