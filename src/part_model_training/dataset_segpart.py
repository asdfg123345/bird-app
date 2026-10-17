# -*- coding: utf-8 -*-
# ============================================================
# 项目签名 | Project Signature
# 作者：陈建宇，号天咏
# 题记：明月照今日，风遥照两川
# 项目：SegFormer 部位分割模型（v7 架构迁移版）
# ============================================================
"""
部位分割数据集：annotations.csv 点标注 → 每部位椭圆掩码

【数据来源】
部位检测数据集（d:/鸟类/部位检测数据集）仅有每部位一个归一化点标注
（<part>_x, <part>_y, <part>_visible），无框、无掩码。本模块依据鸟类
解剖学尺寸先验（config.PART_RADIUS_RATIO），在标注点周围绘制椭圆作为
该部位的监督掩码；不可见部位（visible=0 或坐标越界）不参与监督。

【输出】
- 图像：缩放到 input_size × input_size（双线性）
- 掩码：同尺寸类别索引图（最近邻，0=背景，1..15=部位）
- 点标注：归一化坐标列表（供训练期「点命中率」评估使用）
"""

import csv
import os
import random
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
from torch.utils.data import Dataset
from PIL import Image

from config_segpart import PART_NAMES, PART_RADIUS_RATIO


def load_annotations(csv_path: str) -> List[dict]:
    """读取 annotations.csv，返回逐图标注记录列表。

    每条记录包含：image_id、image_path、split、orig 尺寸、
    以及每部位 (x, y, visible) 归一化点标注。
    """
    records: List[dict] = []
    with open(csv_path, 'r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            record = {
                'image_id': row['image_id'],
                'image_path': row['image_path'],
                'split': row['split'],
                'orig_width': int(float(row['orig_width'])),
                'orig_height': int(float(row['orig_height'])),
                'points': {},  # 部位名 → (x, y) 归一化坐标（仅可见部位）
            }
            for part in PART_NAMES:
                try:
                    visible = int(float(row.get(f'{part}_visible', '0') or 0))
                    px = float(row.get(f'{part}_x', '0') or 0.0)
                    py = float(row.get(f'{part}_y', '0') or 0.0)
                except (TypeError, ValueError):
                    continue
                # 可见且坐标在 [0, 1] 有效范围内才采信
                if visible == 1 and 0.0 <= px <= 1.0 and 0.0 <= py <= 1.0:
                    record['points'][part] = (px, py)
            records.append(record)
    return records


def render_part_mask(width: int, height: int,
                     points: Dict[str, Tuple[float, float]],
                     num_classes: int) -> np.ndarray:
    """按点标注 + 尺寸先验渲染逐部位椭圆掩码（类别索引图）。

    Args:
        width, height: 渲染分辨率（与训练输入尺寸一致）
        points: 部位名 → 归一化中心点
        num_classes: 类别总数（背景 + 15 部位）

    Returns:
        (H, W) int32 掩码，0=背景，1..15=部位（顺序同 PART_NAMES）
    """
    from PIL import ImageDraw

    mask = Image.new('L', (width, height), 0)
    draw = ImageDraw.Draw(mask)
    short_edge = min(width, height)
    # 小部位先画、大部位后画，避免大块掩码盖住小块掩码
    ordered = sorted(points.items(),
                     key=lambda kv: PART_RADIUS_RATIO.get(kv[0], 0.08))
    for part, (px, py) in ordered:
        ratio = PART_RADIUS_RATIO.get(part, 0.08)
        cx, cy = px * width, py * height
        # 椭圆半径：短边比例 × 宽高方向修正（鸟体纵向略长）
        rx = max(2.0, ratio * short_edge)
        ry = max(2.0, ratio * short_edge * 1.2)
        class_idx = PART_NAMES.index(part) + 1  # 背景为 0
        draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=class_idx)
    return np.asarray(mask, dtype=np.int64)


class PartSegDataset(Dataset):
    """部位分割数据集（点标注 → 椭圆掩码监督）"""

    def __init__(self, data_root: str, split: str = 'train',
                 input_size: int = 512, max_samples: Optional[int] = None,
                 seed: int = 42):
        self.data_root = data_root
        self.input_size = input_size
        csv_path = os.path.join(data_root, 'annotations.csv')
        records = [r for r in load_annotations(csv_path)
                   if r['split'] == split]
        if not records:
            # 兼容无 split 标注的情况：全部按 train 处理
            records = [r for r in load_annotations(csv_path)]
        if max_samples is not None and len(records) > max_samples:
            rng = random.Random(seed)
            records = rng.sample(records, max_samples)
        self.records = records

        # ImageNet 归一化参数（与预训练 SegFormer 一致）
        self.mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self.std = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, idx: int):
        record = self.records[idx]
        img_path = os.path.join(self.data_root, record['image_path'])
        image = Image.open(img_path).convert('RGB')
        orig_w, orig_h = image.size

        # 图像缩放（双线性）+ 掩码在同分辨率渲染（避免两次插值误差）
        image = image.resize((self.input_size, self.input_size), Image.BILINEAR)
        mask = render_part_mask(self.input_size, self.input_size,
                                record['points'], len(PART_NAMES) + 1)

        img_np = np.asarray(image, dtype=np.float32) / 255.0
        img_np = (img_np - self.mean) / self.std
        img_tensor = torch.from_numpy(img_np.transpose(2, 0, 1)).float()
        mask_tensor = torch.from_numpy(mask).long()

        # 归一化点标注（供点命中率评估）
        points = {p: torch.tensor(v, dtype=torch.float32)
                  for p, v in record['points'].items()}

        return {
            'image': img_tensor,
            'mask': mask_tensor,
            'points': points,
            'orig_size': (orig_w, orig_h),
            'image_id': record['image_id'],
        }
