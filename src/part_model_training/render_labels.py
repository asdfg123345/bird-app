# -*- coding: utf-8 -*-
"""
render_labels.py —— 关键点标注 → 多格式监督标签转换器

作者：天咏（明月照今日，风摇过两川。）

【数据来源】
部位检测数据集的 annotations.csv（每部位一个归一化点标注 +
可见性标志）。本模块把点标注按鸟类解剖学尺寸先验
（config_segpart.PART_RADIUS_RATIO）展开为中心框，输出三种格式：

1. YOLO 归一化标签（cls cx cy w h）+ verified_list.txt —— 管线 B
   Faster R-CNN 训练（trainer_detector.py 直接可用）；
2. COCO json（绝对像素框）—— 通用 HF 检测模型训练
   （train_generic_det.py 转为 cxcywh 归一化目标）；
3. 逐图框列表（内存接口）—— train_generic_det.py 数据集直接调用。

分割掩码不在本模块渲染：train_segpart.py / train_generic_seg.py
训练时在线调用 dataset_segpart.render_part_mask。
"""

import csv
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config_segpart import PART_NAMES, PART_RADIUS_RATIO


def points_to_boxes(points: Dict[str, Tuple[float, float]],
                    width: int, height: int
                    ) -> List[Tuple[str, float, float, float, float]]:
    """点标注 → 原图像素框列表。

    Args:
        points: 部位名 → 归一化中心点 (x, y)
        width, height: 原图尺寸（像素）

    Returns:
        [(部位名, x1, y1, x2, y2), ...] 像素坐标外接框
    """
    short_edge = min(width, height)
    boxes = []
    for part, (px, py) in points.items():
        ratio = PART_RADIUS_RATIO.get(part, 0.08)
        cx, cy = px * width, py * height
        rx = max(2.0, ratio * short_edge)
        ry = max(2.0, ratio * short_edge * 1.2)
        x1 = max(0.0, cx - rx); x2 = min(float(width), cx + rx)
        y1 = max(0.0, cy - ry); y2 = min(float(height), cy + ry)
        if x2 - x1 >= 1.0 and y2 - y1 >= 1.0:
            boxes.append((part, x1, y1, x2, y2))
    return boxes


def iter_records(data_root: str, split: str = 'train'
                 ) -> List[dict]:
    """读取 annotations.csv 中指定 split 的记录（复用 dataset_segpart 解析）。"""
    from dataset_segpart import load_annotations
    csv_path = Path(data_root) / 'annotations.csv'
    records = [r for r in load_annotations(str(csv_path)) if r['split'] == split]
    if not records:
        # 无 split 列时按全量处理（与 dataset_segpart 兼容逻辑一致）
        records = load_annotations(str(csv_path))
    return records


def to_yolo(data_root: str, out_dir: str, split: str = 'train',
            max_samples: Optional[int] = None) -> Path:
    """关键点 → YOLO 标签 + verified_list（管线 B 训练格式）。

    Returns:
        输出目录 Path（其下含 labels\\*.txt 与 verified_list.txt）
    """
    out = Path(out_dir)
    labels_dir = out / 'labels'
    labels_dir.mkdir(parents=True, exist_ok=True)

    records = iter_records(data_root, split)
    if max_samples is not None and len(records) > max_samples:
        records = records[:max_samples]

    names: List[str] = []
    for rec in records:
        w, h = rec['orig_width'], rec['orig_height']
        lines = []
        for part, x1, y1, x2, y2 in points_to_boxes(rec['points'], w, h):
            cls_id = PART_NAMES.index(part)
            lines.append(f"{cls_id} {(x1 + x2) / 2 / w:.6f} "
                         f"{(y1 + y2) / 2 / h:.6f} {(x2 - x1) / w:.6f} "
                         f"{(y2 - y1) / h:.6f}")
        stem = Path(rec['image_path']).stem
        (labels_dir / f"{stem}.txt").write_text(
            "\n".join(lines), encoding="utf-8")
        names.append(Path(rec['image_path']).name)

    (out / 'verified_list.txt').write_text(
        "\n".join(names), encoding="utf-8")
    print(f"【转换】YOLO 标签 {len(names)} 张 → {labels_dir}")
    return out


def to_coco(data_root: str, out_path: str, split: str = 'train',
            max_samples: Optional[int] = None) -> Path:
    """关键点 → COCO json（通用检测模型训练格式，框为绝对像素 xywh）。

    Returns:
        输出 json 文件 Path
    """
    records = iter_records(data_root, split)
    if max_samples is not None and len(records) > max_samples:
        records = records[:max_samples]

    images, annotations = [], []
    ann_id = 1
    for img_id, rec in enumerate(records, start=1):
        w, h = rec['orig_width'], rec['orig_height']
        images.append({"id": img_id, "file_name": rec['image_path'],
                       "width": w, "height": h})
        for part, x1, y1, x2, y2 in points_to_boxes(rec['points'], w, h):
            annotations.append({
                "id": ann_id, "image_id": img_id,
                "category_id": PART_NAMES.index(part) + 1,  # COCO 类别从 1 起
                "bbox": [round(x1, 2), round(y1, 2),
                         round(x2 - x1, 2), round(y2 - y1, 2)],
                "area": round((x2 - x1) * (y2 - y1), 2),
                "iscrowd": 0,
            })
            ann_id += 1

    coco = {
        "images": images,
        "annotations": annotations,
        "categories": [{"id": i + 1, "name": p}
                       for i, p in enumerate(PART_NAMES)],
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        json.dump(coco, f, ensure_ascii=False)
    print(f"【转换】COCO 标注 {len(images)} 张 / {len(annotations)} 框 → {out}")
    return out


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="关键点标注转 YOLO/COCO 标签")
    parser.add_argument("--data_root", required=True, help="部位检测数据集根目录")
    parser.add_argument("--out", default=None, help="输出目录（YOLO）或 json 路径（COCO）")
    parser.add_argument("--format", choices=["yolo", "coco"], default="yolo")
    parser.add_argument("--split", default="train")
    args = parser.parse_args()
    if args.format == "yolo":
        to_yolo(args.data_root, args.out or "converted_labels")
    else:
        to_coco(args.data_root, args.out or "converted.json")
