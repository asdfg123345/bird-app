# -*- coding: utf-8 -*-
# ============================================================
# 项目签名 | Project Signature
# 作者：陈建宇，号天咏
# 题记：明月照今日，风遥照两川
# 项目：SegFormer 部位分割模型（v7 架构迁移版）
# ============================================================
"""
部位分割推理脚本：图像 → 逐部位外接框

【流程】
1. 加载 best_segpart.pth，按 checkpoint 中记录的规模重建 v7 增强 SegFormer
2. 整图前向 → softmax → 逐类别掩码（argmax 类别图 + 概率阈值过滤）
3. 每类别掩码取外接框；wing/eye/leg 等对称部位取左右并集
4. 输出：{部位英文名: (x1, y1, x2, y2, 置信度)}，坐标为原图像素

用法：
    python infer_segpart.py --weights training_output/best_segpart.pth \
        --image "图片路径.jpg"
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

from config_segpart import CONFIG_SEGPART, PART_NAMES, SYMMETRIC_PARTS
from model_segpart import build_enhanced_segformer


def get_device() -> torch.device:
    """优先使用 GPU，不可用时回退 CPU。"""
    return torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class SegPartPredictor:
    """部位分割推理器：加载权重 → 分割 → 逐部位框（含对称并集）。"""

    def __init__(self, weights_path: str, device: Optional[torch.device] = None):
        self.device = device or get_device()
        ckpt = torch.load(str(weights_path), map_location='cpu',
                          weights_only=False)
        self.checkpoint = ckpt
        self.input_size = int(ckpt.get('input_size', 512))

        # 按 checkpoint 记录的结构参数临时覆盖配置后重建模型
        config = CONFIG_SEGPART
        config.input_size = self.input_size
        if ckpt.get('msfpa_hidden_dim'):
            config.msfpa_hidden_dim = int(ckpt['msfpa_hidden_dim'])
        if ckpt.get('msfpa_dilations'):
            config.msfpa_dilations = list(ckpt['msfpa_dilations'])
        if ckpt.get('ba_msr_alpha_init') is not None:
            config.ba_msr_alpha_init = float(ckpt['ba_msr_alpha_init'])
        self.model = build_enhanced_segformer(config).to(self.device)

        state = ckpt.get('model_state', ckpt)
        state = {str(k)[7:] if str(k).startswith('module.') else str(k): v
                 for k, v in state.items()}
        self.model.load_state_dict(state, strict=True)
        self.model.eval()

        self.mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self.std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        self.min_area_ratio = 1e-4  # 掩码面积占全图比例下限，滤除噪点

    @torch.no_grad()
    def predict_parts(self, pil_image: Image.Image,
                      prob_threshold: float = 0.5
                      ) -> Dict[str, Tuple[float, float, float, float, float]]:
        """整图分割 → 逐部位外接框（像素坐标 + 掩码内平均置信度）。

        Args:
            pil_image: RGB 图像
            prob_threshold: 类别概率阈值（低于此概率的像素不计入掩码）

        Returns:
            {部位英文名: (x1, y1, x2, y2, conf)}；对称部位已合并
        """
        orig_w, orig_h = pil_image.size
        image = pil_image.convert('RGB').resize(
            (self.input_size, self.input_size), Image.BILINEAR)
        img_np = np.asarray(image, dtype=np.float32) / 255.0
        img_np = (img_np - self.mean) / self.std
        tensor = torch.from_numpy(img_np.transpose(2, 0, 1)).unsqueeze(0)
        tensor = tensor.to(self.device)

        outputs = self.model(pixel_values=tensor)
        logits = outputs['logits']  # (1, C, H/4, W/4)
        probs = F.softmax(logits.float(), dim=1)
        probs = F.interpolate(probs, size=(orig_h, orig_w), mode='bilinear',
                              align_corners=False)[0]  # (C, H, W)
        pred = probs.argmax(dim=0)  # (H, W)

        boxes: Dict[str, Tuple[float, float, float, float, float]] = {}
        min_area = self.min_area_ratio * orig_w * orig_h
        for idx, part in enumerate(PART_NAMES, start=1):
            mask = (pred == idx) & (probs[idx] >= prob_threshold)
            area = int(mask.sum().item())
            if area < min_area:
                continue
            ys, xs = torch.nonzero(mask, as_tuple=True)
            x1, x2 = float(xs.min().item()), float(xs.max().item())
            y1, y2 = float(ys.min().item()), float(ys.max().item())
            conf = float(probs[idx][mask].mean().item())
            boxes[part] = (x1, y1, x2, y2, conf)

        # 对称部位并集：wing/eye/leg 合并左右实例
        merged: Dict[str, Tuple[float, float, float, float, float]] = {}
        consumed = set()
        for sym, members in SYMMETRIC_PARTS.items():
            present = [boxes[m] for m in members if m in boxes]
            consumed.update(m for m in members if m in boxes)
            if present:
                merged[sym] = (
                    min(b[0] for b in present), min(b[1] for b in present),
                    max(b[2] for b in present), max(b[3] for b in present),
                    float(np.mean([b[4] for b in present])),
                )
        for part, box in boxes.items():
            if part not in consumed:
                merged[part] = box
        return merged


def main() -> int:
    parser = argparse.ArgumentParser(description='部位分割推理（v7 增强 SegFormer）')
    parser.add_argument('--weights', default='training_output/best_segpart.pth',
                        help='权重文件路径（best_segpart.pth / last_segpart.pth）')
    parser.add_argument('--image', required=True, help='待推理图片路径')
    parser.add_argument('--threshold', type=float, default=0.5,
                        help='类别概率阈值（默认 0.5）')
    args = parser.parse_args()

    if not Path(args.weights).is_file():
        print(f'【错误】权重文件不存在：{args.weights}')
        return 1
    if not Path(args.image).is_file():
        print(f'【错误】图片不存在：{args.image}')
        return 1

    predictor = SegPartPredictor(args.weights)
    image = Image.open(args.image)
    parts = predictor.predict_parts(image, prob_threshold=args.threshold)

    print(f'检出部位 {len(parts)} 个：')
    for part, (x1, y1, x2, y2, conf) in sorted(parts.items()):
        print(f'  {part:<12s} 框=({x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f}) '
              f'置信度={conf:.3f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
