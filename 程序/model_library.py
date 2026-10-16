#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 模块：共享模型库模块（模型注册表、命名取号、预览图、训练模式预设）
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-10-11

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

# 可选依赖：Pillow（仅预览图功能使用，缺失时相关函数自动降级）
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ============================================================
# 路径常量（相对脚本所在目录推导，不硬编码盘符）
# ============================================================
_vfca465fc = Path(__file__).resolve().parent
MODEL_LIBRARY_DIR = _vfca465fc / "data" / "model_library"   # 模型库根目录
_vdd14865b = MODEL_LIBRARY_DIR / "registry.json"        # 注册表
_v0c667d37 = MODEL_LIBRARY_DIR / "settings.json"        # 设置（视觉系统目录定位用，本任务只预留读写）

# 注册表结构版本号（未来结构变更时可据此迁移）
_v6aeaef1d = 1

# 模型目录命名规则：基名_M月D日_NNN（月/日不补零，版本号固定三位补零）
_ve947289e = re.compile(r"^(?P<base>.+)_(?P<month>\d{1,2})月(?P<day>\d{1,2})日_(?P<ver>\d{3})$")

# ============================================================
# 训练模式预设（GUI 与训练脚本共用；纯数据常量，无 torch 依赖）
# 说明：batch 为 None 表示「按设备自适应/由训练脚本推导」；
#       amp 取值：True/False 强制开关，"auto" 表示自动判断。
#       epochs/optimizer/patience/lr 为 None 表示「模式不预设固定值，
#       由用户在 GUI/命令行自定义；未填写时训练脚本回退内置默认值」。
# ============================================================
TRAINING_MODE_PRESETS = {
    "adaptive": {"label": "适应模式（ad）", "epochs": None, "optimizer": None,
                 "patience": None, "batch": None, "lr": None, "amp": "auto",
                 "workers_hint": "自动（按CPU核数2~8）",
                 "description": "读取设备配置（GPU显存/内存/磁盘）自适应推导批大小等资源参数；轮数/优化器/耐心值/学习率由用户自定义；安全优先，效果优先，允许以时间换效果"},
    "low":      {"label": "低速模式", "epochs": None, "optimizer": None,
                 "patience": None, "batch": 8, "lr": None, "amp": True,
                 "workers_hint": "2（低占用后台加载）",
                 "description": "低资源占用，训练慢但稳定，适合配置较低或需后台轻度训练；混合精度降低显存占用；轮数/优化器/耐心值/学习率由用户自定义"},
    "high":     {"label": "高速模式", "epochs": None, "optimizer": None,
                 "patience": None, "batch": None, "lr": None, "amp": True,
                 "workers_hint": "自动（按CPU核数4~12）",
                 "description": "大批量+混合精度，快速出结果，资源占用较高；轮数/优化器/耐心值/学习率由用户自定义"},
    "custom":   {"label": "自定义模式", "epochs": None, "optimizer": None,
                 "patience": None, "batch": None, "lr": None, "amp": False,
                 "workers_hint": "随线程设置（0=自动）",
                 "description": "用户自行设置轮数/优化器/耐心值/批大小/学习率/混合精度；未填写的参数回退训练脚本内置默认值"},
}

# 训练模式key到界面显示名的映射（方便 GUI 下拉框直接使用）
TRAINING_MODE_LABELS = {key: value["label"] for key, value in TRAINING_MODE_PRESETS.items()}


# ============================================================
# 各模式可自定义参数的推荐值计算（GUI 使用；纯数据函数，无 torch 依赖）
# 输入：模式 key、架构 key、数据集图片总数、是否有 GPU、GPU 显存 GB
# 输出：{"epochs", "optimizer", "patience", "lr", "reasons": [中文说明...]}
# 说明：各模式均不预设固定值，推荐值按「模式基调 × 架构 × 数据量 × 硬件」
#       四个维度逐步修正，GUI 据此自动填表并展示推导理由，用户可改。
# ============================================================
def recommend_train_params(mode: str, architecture: str,
                            dataset_count: int | None,
                            has_gpu: bool, gpu_mem_gb: float = 0.0) -> dict:
    arch_key = str(architecture or "resnet50")
    reasons = []

    # 1) 模式基调：训练节奏的出发点
    if mode == "low":
        epochs, patience, optimizer, lr = 15, 6, "adamw", 1e-3
        reasons.append("低速模式基调：轻量节奏，减少训练时长")
    elif mode == "high":
        epochs, patience, optimizer, lr = 10, 4, "adamw", 1e-3
        reasons.append("高速模式基调：短轮数快出结果，耐心值收紧")
    else:
        # 适应模式与自定义模式共用均衡基调
        epochs, patience, optimizer, lr = 30, 10, "adamw", 1e-3
        reasons.append("均衡基调：30轮+耐心值10，安全优先、效果优先")

    # 2) 架构修正：不同网络收敛特性不同
    if arch_key.startswith("vit"):
        # ViT 对学习率敏感（骨干自动 0.1× 分层），注意力模型收敛略慢
        epochs = round(epochs * 1.2)
        patience = max(patience, 8)
        reasons.append(f"ViT 架构：轮数上调 20% 至 {epochs}，耐心值至少 8")
    elif arch_key == "resnest101":
        # ResNeSt101 配套策略为 SGD（动量0.9）+差异化学习率，SGD 需更大学习率
        optimizer, lr = "sgd", 0.01
        epochs = round(epochs * 1.1)
        reasons.append("ResNeSt101 架构：推荐 SGD 优化器+学习率 0.01，轮数上调 10%")
    else:
        reasons.append("ResNet 系架构：标准迁移学习参数即可")

    # 3) 数据量修正：小数据多轮、大数据少轮
    if dataset_count is not None and dataset_count > 0:
        if dataset_count < 5000:
            epochs = round(epochs * 1.5)
            patience = round(patience * 1.5)
            reasons.append(f"小数据集（{dataset_count} 张）：轮数上调 50% 至 {epochs}，"
                           f"耐心值上调至 {patience}，防止过早停止")
        elif dataset_count > 50000:
            epochs = max(6, round(epochs * 0.7))
            reasons.append(f"大数据集（{dataset_count} 张）：单轮信息量大，轮数下调 30% 至 {epochs}")
        else:
            reasons.append(f"中等数据集（{dataset_count} 张）：维持基准轮数")

    # 4) 硬件修正：无 GPU 时截断轮数（CPU 训练过慢），小显存提示资源紧张
    if not has_gpu:
        capped = min(epochs, 15)
        if capped < epochs:
            reasons.append(f"未检测到 GPU：轮数由 {epochs} 截断至 {capped}（CPU 训练过慢）")
            epochs = capped
        reasons.append("无 GPU：批大小等资源参数由训练脚本按 CPU 安全值推导")
    elif gpu_mem_gb and gpu_mem_gb < 6:
        reasons.append(f"GPU 显存仅 {gpu_mem_gb:.1f}GB：资源偏紧，"
                       "批大小将由实测探测收敛到安全值")

    return {"epochs": max(1, epochs), "optimizer": optimizer,
            "patience": max(0, patience), "lr": lr, "reasons": reasons}

# ============================================================
# 架构注册表与模型识别（多架构模型库支持）
# 说明：
#   task_type           模型任务类型（classification/segmentation/other）；
#   inference_supported 能否接入鸟类视觉分析系统的物种分类推理管线，
#                       False 表示仅作入库管理（界面标注「仅管理」）；
#   adapter             视觉系统侧适配器标识（None 表示无可用适配器）。
# ============================================================
ARCHITECTURE_REGISTRY = {
    "resnet50": {
        "display_name": "ResNet50（torchvision）",
        "task_type": "classification",
        "inference_supported": True,
        "adapter": "resnet50",
    },
    "vit_bird_525": {
        "display_name": "ViT 525类鸟类（HuggingFace）",
        "task_type": "classification",
        "inference_supported": True,
        "adapter": "vit_hf",
    },
    "vit_hf": {
        "display_name": "ViT（HuggingFace格式）",
        "task_type": "classification",
        "inference_supported": True,
        "adapter": "vit_hf",
    },
    "resnest101": {
        "display_name": "ResNeSt101（timm）",
        "task_type": "classification",
        "inference_supported": True,
        "adapter": "resnest101",
    },
    "faster_rcnn": {
        "display_name": "Faster R-CNN 部位检测器",
        "task_type": "detection",
        "inference_supported": True,
        "adapter": "faster_rcnn",
    },
    "vit_mmpretrain": {
        "display_name": "ViT（mmpretrain格式，仅管理）",
        "task_type": "classification",
        "inference_supported": False,
        "adapter": None,
    },
    "segformer_seg": {
        "display_name": "SegFormer（语义分割，仅管理）",
        "task_type": "segmentation",
        "inference_supported": False,
        "adapter": None,
    },
    "generic": {
        "display_name": "通用模型（LLM/RNN等，仅管理）",
        "task_type": "other",
        "inference_supported": False,
        "adapter": None,
    },
}

# 候选权重文件的后缀（外部模型导入扫描用）
_vd00b8918 = (".pt", ".pth", ".safetensors", ".bin")


def get_architecture_info(architecture) -> dict:
    return ARCHITECTURE_REGISTRY.get(str(architecture or ""), ARCHITECTURE_REGISTRY["generic"])


def _v6e6cb72a(weights) -> list:
    if isinstance(weights, dict):
        keys = [str(k) for k in weights.keys()]
    else:
        keys = [str(k) for k in weights]
    return [k[7:] if k.startswith("module.") else k for k in keys]


def detect_architecture(weights) -> str:
    keys = _v6e6cb72a(weights)
    if not keys:
        return "generic"
    # 语义分割优先（键名特征最显著）
    if any(k.startswith("segformer.") for k in keys):
        return "segformer_seg"
    # 目标检测（torchvision Faster R-CNN：RPN + RoI 头 + 框预测器）
    if any("roi_heads.box_predictor" in k for k in keys):
        return "faster_rcnn"
    # mmpretrain ViT：backbone 下的融合 QKV
    if any(k.startswith("backbone.") and ".attn.qkv." in k for k in keys):
        return "vit_mmpretrain"
    # ResNeSt 的 SplitAttention：conv2.fc1 / conv2.fc2（ResNet 无此结构）
    if any(re.search(r"\.conv2\.fc[12]\.(weight|bias)$", k) for k in keys):
        return "resnest101"
    # HuggingFace ViT：vit. 骨干 + classifier. 分类头
    if any(k.startswith("vit.") for k in keys) and any(k.startswith("classifier.") for k in keys):
        return "vit_hf"
    # torchvision 风格卷积网络（ResNet 等）
    if any(k.endswith("fc.weight") for k in keys):
        return "resnet50"
    return "generic"


def unwrap_state_dict(ckpt) -> dict:
    if isinstance(ckpt, dict):
        for key in ("state_dict", "model_state_dict", "model_state",
                    "model", "weights"):
            inner = ckpt.get(key)
            if isinstance(inner, dict) and inner:
                return inner
    return ckpt


def find_candidate_weights(model_dir) -> list:
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        return []
    ordered_names = ("best_model.pt", "last_model.pt", "best_detector.pth",
                     "last_detector.pth", "model.safetensors")
    found: list = []
    for name in ordered_names:
        candidate = model_dir / name
        if candidate.is_file():
            found.append(candidate)
    for item in sorted(model_dir.iterdir()):
        if not item.is_file() or item.suffix.lower() not in _vd00b8918:
            continue
        if item.name in ordered_names or item.name.endswith(".tmp"):
            continue
        found.append(item)
    return found


def has_hf_config(model_dir) -> bool:
    model_dir = Path(model_dir)
    if not (model_dir / "config.json").is_file():
        return False
    return (model_dir / "model.safetensors").is_file() or \
        bool(list(model_dir.glob("*.bin")))


def _v4f69ab97(path) -> dict:
    mapping: dict = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n\r")
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    parts = line.split(",")
                    if len(parts) < 2:
                        continue
                idx_text, name = parts[0].strip(), parts[1].strip()
                if not name:
                    continue
                try:
                    mapping[name] = int(idx_text)
                except ValueError:
                    continue
    except OSError:
        return {}
    return mapping


def build_labels_auto(model_dir, class_count=None) -> dict:
    model_dir = Path(model_dir)
    # 1. 已存在的 labels.json
    existing = model_dir / "labels.json"
    if existing.is_file():
        try:
            with open(existing, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return {str(k): int(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError):
            pass
    # 2. HuggingFace config.json 的 id2label
    cfg_path = model_dir / "config.json"
    if cfg_path.is_file():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            id2label = cfg.get("id2label") if isinstance(cfg, dict) else None
            if isinstance(id2label, dict) and id2label:
                mapping = {}
                for idx, name in id2label.items():
                    try:
                        mapping[str(name)] = int(idx)
                    except (TypeError, ValueError):
                        continue
                if mapping:
                    return mapping
        except (OSError, ValueError):
            pass
    # 3. 列表格式类别文件（class_names.json / classes.json）
    for list_name in ("class_names.json", "classes.json"):
        list_path = model_dir / list_name
        if list_path.is_file():
            try:
                with open(list_path, "r", encoding="utf-8") as f:
                    names = json.load(f)
                if isinstance(names, list) and names:
                    return {str(n): i for i, n in enumerate(names)}
            except (OSError, ValueError):
                pass
    # 4. label_mapping.txt（TSV）
    for txt_name in ("label_mapping.txt", "labels.txt", "classes.txt"):
        txt_path = model_dir / txt_name
        if txt_path.is_file():
            mapping = _v4f69ab97(txt_path)
            if mapping:
                return mapping
    # 5. 兜底占位
    if class_count and int(class_count) > 0:
        return {f"类别_{i}": i for i in range(int(class_count))}
    return {}


def _v050cde72(weights, labels=None) -> int | None:
    if isinstance(labels, dict) and labels:
        return len(labels)
    if isinstance(weights, dict):
        for key in ("classifier.weight", "fc.weight",
                    "roi_heads.box_predictor.cls_score.weight"):
            for k, v in weights.items():
                if str(k).endswith(key) and hasattr(v, "shape") and len(v.shape) >= 1:
                    return int(v.shape[0])
    return None


def validate_import_source(model_dir) -> tuple:
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        return False, f"模型目录不存在：{model_dir}", []
    if any((model_dir / n).is_file() for n in ("best_model.pt", "last_model.pt")):
        return True, "检测到训练产物格式（best_model.pt / last_model.pt）", \
            find_candidate_weights(model_dir)
    if has_hf_config(model_dir):
        return True, "检测到 HuggingFace 模型目录（config.json + 权重文件）", \
            find_candidate_weights(model_dir)
    candidates = find_candidate_weights(model_dir)
    if candidates:
        return True, f"检测到权重文件：{candidates[0].name}", candidates
    return False, "所选目录中未找到可识别的权重文件（.pt/.pth/.safetensors/.bin）", []


# 评估指标显示名（模型库列表与详情展示用）
_vd94fddd8 = {
    "top1_acc": "Top-1",
    "val_acc": "Top-1",       # 旧记录字段名，语义等同 Top-1 准确率
    "top5_acc": "Top-5",
    "macro_f1": "宏F1",
    "val_loss": "验证损失",
    "miou": "mIoU",
    "map50": "mAP@0.5",
    "map": "mAP",
    "mAP": "mAP",
}


def format_metric_value(metric_name, value) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    name = metric_name or "val_acc"
    label = _vd94fddd8.get(name, name)
    if name in ("val_loss", "loss"):
        return f"{label} {value:.4f}"
    return f"{label} {value * 100:.2f}%"


# ============================================================
# 数据集划分预设与判据选项（GUI 与训练脚本共用的纯数据常量）
# 说明：GUI 只读这些常量做下拉与提示；训练脚本 model_trainer.py 内有同名
#       权威实现（SPLIT_PRESETS / EVAL_METRIC_REGISTRY），两者需保持同步。
# ============================================================
SPLIT_PRESETS = {
    "existing":    {"label": "使用目录既有划分（默认）", "ratio": None},
    "balanced":    {"label": "均衡标准级 8:1:1", "ratio": [8.0, 1.0, 1.0]},
    "train_first": {"label": "训练优先级 9:0.5:0.5", "ratio": [9.0, 0.5, 0.5]},
    "val_heavy":   {"label": "验证强化级 7:1.5:1.5", "ratio": [7.0, 1.5, 1.5]},
    "custom":      {"label": "自定义比例", "ratio": None},
}
SPLIT_PRESET_LABELS = {key: value["label"] for key, value in SPLIT_PRESETS.items()}

# 最佳模型判据选项（键 → 中文名），供 GUI 下拉使用
SELECT_METRIC_OPTIONS = {
    "top1_acc": "Top-1 准确率（默认）",
    "top5_acc": "Top-5 准确率",
    "macro_f1": "宏平均 F1",
    "val_loss": "验证损失（越小越好）",
}

# 架构 → 训练策略摘要（与训练脚本 ARCH_TRAINING_STRATEGY 保持一致的展示文案）
_vd555eb39 = {
    "resnet50": "ResNet50 标准策略：等学习率 + 余弦退火（与原训练行为一致）",
    "vit_bird_525": "ViT 迁移策略：骨干0.1×差异化学习率 + 2轮升温 + 余弦退火 + 标签平滑0.1",
    "vit_hf": "ViT 迁移策略：骨干0.1×差异化学习率 + 2轮升温 + 余弦退火 + 标签平滑0.1",
    "resnest101": "ResNeSt101 策略：SGD(动量0.9) + 差异化学习率 + 1轮升温 + 余弦退火",
}


def get_strategy_summary(architecture) -> str:
    return _vd555eb39.get(str(architecture or "resnet50"),
                                     _vd555eb39["resnet50"])

# ============================================================
# 内置预置物种模型（不进注册表，只读展示；供模型库列表与训练基础模型选择）
# 说明：内置条目不参与删除/激活，path 对 ViT 指向权重目录（可不存在，
#       由训练/视觉侧按同名常量再探测）；architecture 供训练脚本重建网络。
# ============================================================
# 525 类鸟类 ViT 权重目录解析（按优先级）：
#   1. 环境变量 BIRD_VIT_525_DIR；2. 脚本上两级目录下的 bird_species_image_detection
#      （源码运行即项目根 d:\鸟类\bird_species_image_detection，不硬编码盘符）
VIT_525_ENV_VAR = "BIRD_VIT_525_DIR"


def resolve_vit_bird_525_dir() -> Path:
    env_value = os.environ.get(VIT_525_ENV_VAR, "").strip()
    if env_value:
        return Path(env_value)
    return _vfca465fc.parent.parent / "bird_species_image_detection"


def _v66a6a36a() -> list:
    return [
        {
            "name": "resnet50_imagenet",
            "base_name": "resnet50",
            "date_str": "—",
            "version": 0,
            "path": "",
            "class_count": 1000,
            "accuracy": None,
            "trained_at": "",
            "mode": None,
            "source": "内置预置",
            "preview_path": None,
            "architecture": "resnet50",
            "note": "ResNet50（ImageNet 预训练权重），从零训练即微调 ImageNet",
            "builtin": True,
        },
        {
            "name": "vit_bird_525",
            "base_name": "vit_bird_525",
            "date_str": "—",
            "version": 0,
            "path": str(resolve_vit_bird_525_dir()),
            "class_count": 525,
            "accuracy": None,
            "trained_at": "",
            "mode": None,
            "source": "内置预置",
            "preview_path": None,
            "architecture": "vit_bird_525",
            "note": "525 类鸟类 ViT（本地预置权重），鸟类专域起点，迁移效果通常优于 ImageNet",
            "builtin": True,
        },
    ]


def _vc6b294e7(name, root=None):
    for record in _v66a6a36a():
        if record.get("name") == name:
            return record
    return None


# ============================================================
# 内部辅助函数
# ============================================================
def _v34cb505e(root) -> Path:
    if root is None:
        return MODEL_LIBRARY_DIR
    return Path(root)


def _v4d29c976() -> dict:
    return {"version": _v6aeaef1d, "active_species_model": None, "models": []}


def _v7115a095(path: Path):
    try:
        if not path.is_file():
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        counter = 1
        while backup.exists():  # 同一秒内多次备份时追加序号避免覆盖
            backup = path.with_name(f"{path.name}.bak.{stamp}_{counter}")
            counter += 1
        shutil.copy2(path, backup)
        return backup
    except OSError:
        return None


def _v3615e66d(size: int):
    if not HAS_PIL:
        return None
    windir = os.environ.get("WINDIR") or os.environ.get("winDir")
    if not windir:
        return None
    fonts_dir = Path(windir) / "Fonts"
    for name in ("msyh.ttc", "msyh.ttf", "simhei.ttf", "simsun.ttc", "simkai.ttf"):
        try:
            return ImageFont.truetype(str(fonts_dir / name), size)
        except Exception:
            continue
    return None


# ============================================================
# 注册表读写
# ============================================================
def _vf298d7c9(root=None) -> dict:
    root_dir = _v34cb505e(root)
    reg_path = root_dir / "registry.json"
    if not reg_path.exists():
        reg = _v4d29c976()
        _v59322f41(reg, root_dir)  # 首次访问即落盘，方便用户直接看到库位置
        return reg
    try:
        with open(reg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("models", []), list):
            raise ValueError("注册表结构不合法")
        # 自动补齐缺失字段，兼容手工编辑过的注册表
        data.setdefault("version", _v6aeaef1d)
        data.setdefault("active_species_model", None)
        data.setdefault("models", [])
        return data
    except (OSError, ValueError):
        # JSON 解析失败/读取失败/结构不合法：备份原件后重建
        _v7115a095(reg_path)
        reg = _v4d29c976()
        _v59322f41(reg, root_dir)
        return reg


def _v59322f41(reg, root=None) -> bool:
    root_dir = _v34cb505e(root)
    reg_path = root_dir / "registry.json"
    try:
        root_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = reg_path.with_name(reg_path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, reg_path)
        return True
    except OSError:
        return False


# ============================================================
# 模型命名与注册
# ============================================================
def next_model_name(base_name, date=None, root=None):
    if date is None:
        date = datetime.now()
    date_str = f"{date.month}月{date.day}日"  # 月/日不补零
    root_dir = _v34cb505e(root)
    reg = _vf298d7c9(root_dir)

    max_version = 0
    # 第一步：扫描注册表中同基名同日期的记录
    for item in reg.get("models", []):
        if not isinstance(item, dict):
            continue
        if item.get("base_name") != base_name or item.get("date_str") != date_str:
            continue
        try:
            max_version = max(max_version, int(item.get("version", 0)))
        except (TypeError, ValueError):
            continue
    # 第二步：同时扫描磁盘上已存在的同模式目录（注册表丢失时仍能续号）
    pattern = re.compile(rf"^{re.escape(base_name)}_{re.escape(date_str)}_(\d{{3}})$")
    try:
        if root_dir.is_dir():
            for child in root_dir.iterdir():
                matched = pattern.match(child.name)
                if matched:
                    max_version = max(max_version, int(matched.group(1)))
    except OSError:
        pass

    version = max_version + 1
    dir_name = f"{base_name}_{date_str}_{version:03d}"
    return dir_name, date_str, version


def register_model(path, class_count, accuracy=None, mode=None,
                   source="本机训练", preview_path=None, architecture="resnet50",
                   root=None, metric_name="val_acc", metrics=None,
                   task_type=None) -> dict:
    model_dir = Path(path)
    if not model_dir.is_dir():
        raise ValueError(f"模型目录不存在，无法注册：{model_dir}")

    arch_key = architecture or "resnet50"
    arch_info = get_architecture_info(arch_key)

    dir_name = model_dir.name
    matched = _ve947289e.match(dir_name)
    if matched:
        base_name = matched.group("base")
        date_str = f"{matched.group('month')}月{matched.group('day')}日"
        version = int(matched.group("ver"))
    else:
        # 目录名不符合命名规则时用目录名兜底，日期取目录修改时间
        base_name = dir_name
        version = 1
        try:
            mtime = datetime.fromtimestamp(model_dir.stat().st_mtime)
            date_str = f"{mtime.month}月{mtime.day}日"
        except OSError:
            now = datetime.now()
            date_str = f"{now.month}月{now.day}日"

    record = {
        "name": dir_name,
        "base_name": base_name,
        "date_str": date_str,
        "version": version,
        "path": str(model_dir.resolve()),  # 存绝对路径字符串，供训练/视觉系统直接加载
        "class_count": int(class_count),
        "accuracy": accuracy,
        "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "source": source,
        "preview_path": str(preview_path) if preview_path else None,
        "architecture": arch_key,
        "metric_name": metric_name or "val_acc",
        "metrics": dict(metrics) if isinstance(metrics, dict) else None,
        "task_type": task_type or arch_info.get("task_type", "classification"),
        "inference_supported": bool(arch_info.get("inference_supported", False)),
    }

    reg = _vf298d7c9(root)
    models = reg.setdefault("models", [])
    for index, item in enumerate(models):
        if isinstance(item, dict) and item.get("name") == dir_name:
            models[index] = record  # 同名重复注册时覆盖旧记录
            break
    else:
        models.append(record)
    _v59322f41(reg, root)
    return record


# ============================================================
# 查询 / 删除 / 激活
# ============================================================
def list_models(root=None, include_builtin=False) -> list:
    reg = _vf298d7c9(root)
    models = [item for item in reg.get("models", []) if isinstance(item, dict)]
    models.sort(key=lambda item: item.get("trained_at") or "", reverse=True)
    if include_builtin:
        return _v66a6a36a() + models
    return models


def get_model(name, root=None):
    reg = _vf298d7c9(root)
    for item in reg.get("models", []):
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return _vc6b294e7(name, root)


def remove_model(name, root=None) -> bool:
    reg = _vf298d7c9(root)
    models = reg.get("models", [])
    target = None
    for item in models:
        if isinstance(item, dict) and item.get("name") == name:
            target = item
            break
    if target is None:
        return False

    model_path = target.get("path")
    if model_path:
        shutil.rmtree(model_path, ignore_errors=True)  # 容错删除，不抛异常

    models.remove(target)
    if reg.get("active_species_model") == name:
        reg["active_species_model"] = None
    _v59322f41(reg, root)
    return True


def set_active_species_model(name, root=None) -> bool:
    record = get_model(name, root)
    if record is None:
        raise ValueError(f"模型不存在，无法设为激活模型：{name}")
    if record.get("builtin"):
        raise ValueError("内置预置模型不可设为激活模型（请在「视觉分析」页直接选择使用）。")
    reg = _vf298d7c9(root)
    reg["active_species_model"] = name
    _v59322f41(reg, root)
    return True


def get_active_species_model(root=None):
    reg = _vf298d7c9(root)
    name = reg.get("active_species_model")
    if not name:
        return None
    return get_model(name, root)


# ============================================================
# 预览图生成
# ============================================================
def make_preview(sample_paths, out_path, tile=112):
    if not HAS_PIL:
        print("提示：未安装 Pillow 库，无法生成模型预览图（可执行 pip install pillow）")
        return None

    # 四个象限的补位纯色（深浅不一的蓝灰色，便于区分）
    block_colors = [(58, 72, 100), (66, 82, 112), (52, 64, 92), (72, 90, 122)]
    canvas = Image.new("RGB", (tile * 2, tile * 2), (36, 48, 72))
    draw = ImageDraw.Draw(canvas)
    samples = list(sample_paths or [])[:4]

    for index in range(4):
        box_left = (index % 2) * tile
        box_top = (index // 2) * tile
        # 先铺补位色块，样图可用时再叠加图片
        draw.rectangle([box_left, box_top, box_left + tile - 1, box_top + tile - 1],
                       fill=block_colors[index])
        if index >= len(samples):
            continue
        img = None
        try:
            with Image.open(samples[index]) as opened:
                img = opened.convert("RGB")
        except Exception:
            img = None  # 路径不存在或格式不支持时保留色块
        if img is not None:
            img.thumbnail((tile, tile))  # 等比缩放到不超过象限尺寸
            offset_x = box_left + (tile - img.width) // 2
            offset_y = box_top + (tile - img.height) // 2
            canvas.paste(img, (offset_x, offset_y))

    out = Path(out_path)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out)
        return out
    except (OSError, ValueError):
        return None


def make_default_preview(out_path):
    if not HAS_PIL:
        print("提示：未安装 Pillow 库，无法生成默认预览图（可执行 pip install pillow）")
        return None

    size = 224
    canvas = Image.new("RGB", (size, size), (16, 32, 64))
    draw = ImageDraw.Draw(canvas)

    drew_text = False
    font = _v3615e66d(max(12, size // 8))
    if font is not None:
        try:
            draw.text((size / 2, size / 2), "无预览",
                      fill=(186, 205, 235), font=font, anchor="mm")
            drew_text = True
        except Exception:
            drew_text = False

    if not drew_text:
        # 图形占位：外边框 + 极简小鸟（椭圆身体、圆头、三角嘴、尾巴、眼睛）
        edge = 8
        draw.rectangle([edge, edge, size - edge, size - edge],
                       outline=(64, 88, 132), width=2)
        body_box = [size * 0.24, size * 0.46, size * 0.64, size * 0.72]
        draw.ellipse(body_box, outline=(150, 175, 215), width=3)
        head_r = size * 0.10
        head_cx = body_box[2] - head_r * 0.2
        head_cy = body_box[1] - head_r * 0.9
        draw.ellipse([head_cx - head_r, head_cy - head_r,
                      head_cx + head_r, head_cy + head_r],
                     outline=(150, 175, 215), width=3)
        draw.polygon([(head_cx + head_r, head_cy - head_r * 0.25),        # 三角嘴
                      (head_cx + head_r + size * 0.08, head_cy),
                      (head_cx + head_r, head_cy + head_r * 0.25)],
                     fill=(214, 178, 106))
        eye_r = max(1.5, size * 0.015)
        eye_cx, eye_cy = head_cx + head_r * 0.15, head_cy - head_r * 0.3
        draw.ellipse([eye_cx - eye_r, eye_cy - eye_r, eye_cx + eye_r, eye_cy + eye_r],
                     fill=(220, 230, 245))
        draw.polygon([(body_box[0], size * 0.55),                         # 尾巴
                      (body_box[0] - size * 0.12, size * 0.47),
                      (body_box[0], size * 0.66)],
                     fill=(120, 145, 190))

    out = Path(out_path)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out)
        return out
    except (OSError, ValueError):
        return None


# ============================================================
# 设置文件读写（settings.json，后续视觉系统目录定位用）
# ============================================================
def read_settings(root=None) -> dict:
    path = _v34cb505e(root) / "settings.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _v7115a095(path)
        return {}


def write_settings(settings, root=None) -> bool:
    path = _v34cb505e(root) / "settings.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return True
    except (OSError, TypeError, ValueError):
        return False
