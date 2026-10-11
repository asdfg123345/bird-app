#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 模块：共享模型库模块（模型注册表、命名取号、预览图、训练模式预设）
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-10-11
"""共享模型库模块：「模型界面整合」工程的地基。

集中管理本机训练产出的鸟类分类模型，后续训练脚本、GUI、视觉分析系统
统一通过本模块读写模型库，避免各自维护散落的模型路径。功能包括：

    1. 注册表（registry.json）：记录每个模型的名称、路径、类别数、
       准确率、训练时间、训练模式等元信息，并维护「激活物种模型」指针；
       文件缺失自动重建，文件损坏自动备份后重建，全程不抛异常；
    2. 模型命名取号：按 基名_M月D日_NNN 规则自动生成不重复的目录名；
    3. 预览图：把最多 4 张样例图拼成 2×2 缩略图，或生成默认占位图
       （未安装 Pillow 时打印中文提示并返回 None，不抛异常）；
    4. 训练模式预设：GUI 与训练脚本共用的纯数据常量（不依赖 torch）。

用法示例：
    import model_library as ml
    name, date_str, version = ml.next_model_name("resnet50")
    record = ml.register_model(model_dir, class_count=200, accuracy=0.86, mode="low")
    ml.set_active_species_model(record["name"])
"""

from __future__ import annotations


# ============================================================
# 冻结环境适配：打包为 exe 后，数据目录固定到 exe 所在目录
# ============================================================
def _frozen_base(src_file):
    """打包后返回 exe 所在目录；源码运行返回脚本所在目录。"""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(src_file).resolve().parent


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
BASE_DIR = _frozen_base(__file__)
MODEL_LIBRARY_DIR = BASE_DIR / "data" / "model_library"   # 模型库根目录
REGISTRY_PATH = MODEL_LIBRARY_DIR / "registry.json"        # 注册表
SETTINGS_PATH = MODEL_LIBRARY_DIR / "settings.json"        # 设置（视觉系统目录定位用，本任务只预留读写）

# 注册表结构版本号（未来结构变更时可据此迁移）
REGISTRY_VERSION = 1

# 模型目录命名规则：基名_M月D日_NNN（月/日不补零，版本号固定三位补零）
_NAME_PATTERN = re.compile(r"^(?P<base>.+)_(?P<month>\d{1,2})月(?P<day>\d{1,2})日_(?P<ver>\d{3})$")

# ============================================================
# 训练模式预设（GUI 与训练脚本共用；纯数据常量，无 torch 依赖）
# 说明：batch 为 None 表示「按设备自适应/由训练脚本推导」；
#       amp 取值：True/False 强制开关，"auto" 表示自动判断。
# ============================================================
TRAINING_MODE_PRESETS = {
    "adaptive": {"label": "适应模式（ad）", "epochs": 30, "optimizer": "adamw",
                 "patience": 10, "batch": None, "lr": 1e-3, "amp": "auto",
                 "description": "读取设备配置（GPU显存/内存/磁盘），在安全限制内自适应配置；安全优先，效果优先，允许以时间换效果"},
    "low":      {"label": "低速模式", "epochs": 20, "optimizer": "adamw",
                 "patience": 6, "batch": 8, "lr": 1e-3, "amp": False,
                 "description": "低资源占用，训练慢但稳定，适合配置较低或需后台轻度训练"},
    "high":     {"label": "高速模式", "epochs": 10, "optimizer": "adamw",
                 "patience": 4, "batch": None, "lr": 1e-3, "amp": True,
                 "description": "大批量+混合精度，快速出结果，资源占用较高"},
    "custom":   {"label": "自定义模式", "epochs": None, "optimizer": "adamw",
                 "patience": None, "batch": None, "lr": 1e-3, "amp": False,
                 "description": "用户自行设置轮数/优化器/耐心值/批大小/学习率"},
}

# 训练模式key到界面显示名的映射（方便 GUI 下拉框直接使用）
TRAINING_MODE_LABELS = {key: value["label"] for key, value in TRAINING_MODE_PRESETS.items()}


# ============================================================
# 内部辅助函数
# ============================================================
def _resolve_root(root) -> Path:
    """把 root 参数归一化为模型库根目录路径（缺省用内置模型库目录）。"""
    if root is None:
        return MODEL_LIBRARY_DIR
    return Path(root)


def _new_registry() -> dict:
    """构造一份初始注册表结构。"""
    return {"version": REGISTRY_VERSION, "active_species_model": None, "models": []}


def _backup_file(path: Path):
    """把指定文件备份为 <原文件名>.bak.<时间戳>。

    成功返回备份文件路径；文件不存在或备份失败返回 None（不抛异常）。
    """
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


def _load_cjk_font(size: int):
    """尝试加载一个支持中文的系统字体；找不到返回 None。

    通过环境变量 WINDIR 定位字体目录，不硬编码盘符；
    非 Windows 环境或字体缺失时返回 None，由调用方退化为图形元素。
    """
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
def load_registry(root=None) -> dict:
    """读取模型注册表，保证总能返回可用的注册表结构（不抛异常）。

    行为约定：
        - 文件不存在：返回初始结构并立即落盘；
        - 文件损坏：先把原文件备份为 registry.json.bak.<时间戳>，
          再重建初始结构并落盘；
        - 结构不完整（缺字段）：自动补默认值后返回。
    """
    root_dir = _resolve_root(root)
    reg_path = root_dir / "registry.json"
    if not reg_path.exists():
        reg = _new_registry()
        save_registry(reg, root_dir)  # 首次访问即落盘，方便用户直接看到库位置
        return reg
    try:
        with open(reg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("models", []), list):
            raise ValueError("注册表结构不合法")
        # 自动补齐缺失字段，兼容手工编辑过的注册表
        data.setdefault("version", REGISTRY_VERSION)
        data.setdefault("active_species_model", None)
        data.setdefault("models", [])
        return data
    except (OSError, ValueError):
        # JSON 解析失败/读取失败/结构不合法：备份原件后重建
        _backup_file(reg_path)
        reg = _new_registry()
        save_registry(reg, root_dir)
        return reg


def save_registry(reg, root=None) -> bool:
    """把注册表写入磁盘（先写临时文件再原子替换，避免写一半损坏）。

    成功返回 True；目录创建或写入失败返回 False（不抛异常）。
    """
    root_dir = _resolve_root(root)
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
    """推算下一个可用的模型目录名，返回 (目录名, 日期串, 版本号)。

    命名规则：基名_M月D日_NNN（月/日不补零，版本号三位补零，
    如 resnet50_10月11日_001）。取号逻辑：对同 base_name 同日期，
    扫描注册表记录与磁盘上已存在的同模式目录，取最大版本号 + 1。

    参数：
        base_name: 模型基名（如 resnet50）
        date:      datetime/date 对象；None 表示取当天
        root:      模型库根目录；None 表示用内置模型库目录
    """
    if date is None:
        date = datetime.now()
    date_str = f"{date.month}月{date.day}日"  # 月/日不补零
    root_dir = _resolve_root(root)
    reg = load_registry(root_dir)

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
                   source="本机训练", preview_path=None, root=None) -> dict:
    """把一个已存在的模型目录登记进注册表，返回该模型的记录字典。

    会自动从目录名解析 base_name/日期/版本号；目录名不符合
    基名_M月D日_NNN 规则时，用目录名兜底作为 base_name（版本号记 1）。

    参数：
        path:        已存在的模型目录路径（不存在时抛 ValueError）
        class_count: 模型类别数
        accuracy:    验证集准确率（0~1，可为 None）
        mode:        训练模式 key（见 TRAINING_MODE_PRESETS）
        source:      模型来源，默认「本机训练」
        preview_path: 预览图路径（可为 None）
        root:        模型库根目录；None 表示用内置模型库目录
    """
    model_dir = Path(path)
    if not model_dir.is_dir():
        raise ValueError(f"模型目录不存在，无法注册：{model_dir}")

    dir_name = model_dir.name
    matched = _NAME_PATTERN.match(dir_name)
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
    }

    reg = load_registry(root)
    models = reg.setdefault("models", [])
    for index, item in enumerate(models):
        if isinstance(item, dict) and item.get("name") == dir_name:
            models[index] = record  # 同名重复注册时覆盖旧记录
            break
    else:
        models.append(record)
    save_registry(reg, root)
    return record


# ============================================================
# 查询 / 删除 / 激活
# ============================================================
def list_models(root=None) -> list:
    """列出注册表中的全部模型记录（按训练时间倒序，最新的在前）。"""
    reg = load_registry(root)
    models = [item for item in reg.get("models", []) if isinstance(item, dict)]
    models.sort(key=lambda item: item.get("trained_at") or "", reverse=True)
    return models


def get_model(name, root=None):
    """按名称查询模型记录；找不到返回 None。"""
    reg = load_registry(root)
    for item in reg.get("models", []):
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def remove_model(name, root=None) -> bool:
    """删除指定模型：连同模型目录与注册表记录一起清除。

    目录删除采用容错策略（可能已被手动删除或被占用）；若删除的是
    当前激活模型，则把 active_species_model 置回 None。成功返回 True，
    模型不存在返回 False。
    """
    reg = load_registry(root)
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
    save_registry(reg, root)
    return True


def set_active_species_model(name, root=None) -> bool:
    """把指定模型设为「激活物种模型」（视觉系统默认加载它）。

    模型不存在时抛 ValueError（中文消息）。成功返回 True。
    """
    if get_model(name, root) is None:
        raise ValueError(f"模型不存在，无法设为激活模型：{name}")
    reg = load_registry(root)
    reg["active_species_model"] = name
    save_registry(reg, root)
    return True


def get_active_species_model(root=None):
    """返回当前激活模型的记录字典；尚未设置或已被删除时返回 None。"""
    reg = load_registry(root)
    name = reg.get("active_species_model")
    if not name:
        return None
    return get_model(name, root)


# ============================================================
# 预览图生成
# ============================================================
def make_preview(sample_paths, out_path, tile=112):
    """把最多 4 张样例图片拼成 2×2 缩略预览图并保存。

    样图不足或读取失败时，对应象限用纯色块补位。
    未安装 Pillow 时打印中文提示并返回 None（不抛异常）。

    参数：
        sample_paths: 样例图片路径列表（最多取前 4 张）
        out_path:     输出图片路径
        tile:         单个象限边长（像素），默认 112，整图为 2×tile 见方
    返回：
        成功返回输出路径（Path 对象），失败返回 None。
    """
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
    """生成一张程序化的默认占位缩略图（深蓝底）。

    优先尝试用系统中文字体写「无预览」二字；找不到中文字体时
    退化为图形元素（极简小鸟图案），保证任何环境都能出图。
    未安装 Pillow 时打印中文提示并返回 None（不抛异常）。
    """
    if not HAS_PIL:
        print("提示：未安装 Pillow 库，无法生成默认预览图（可执行 pip install pillow）")
        return None

    size = 224
    canvas = Image.new("RGB", (size, size), (16, 32, 64))
    draw = ImageDraw.Draw(canvas)

    drew_text = False
    font = _load_cjk_font(max(12, size // 8))
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
    """读取 settings.json；文件缺失或损坏时返回空字典（损坏会先备份）。"""
    path = _resolve_root(root) / "settings.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _backup_file(path)
        return {}


def write_settings(settings, root=None) -> bool:
    """把设置字典写入 settings.json（先写临时文件再原子替换）。

    成功返回 True；序列化或写入失败返回 False（不抛异常）。
    """
    path = _resolve_root(root) / "settings.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return True
    except (OSError, TypeError, ValueError):
        return False
