# -*- coding: utf-8 -*-
"""
train_generic_det.py —— 通用检测底座微调（HuggingFace / 魔搭任意检测架构）

作者：天咏（明月照今日，风摇过两川。）

【功能】
以任意兼容 transformers AutoModelForObjectDetection 的开源模型为底座
（DETR / YOLOS / RT-DETR 等，来自 HuggingFace 或魔搭 ModelScope），在
部位检测数据集上微调为 15 部位检测模型（关键点按尺寸先验展开为中心框）。

【流程】
1. hub_resolver 解析底座（本地目录 → 魔搭 → HF 镜像）；
2. 分类头按 num_labels=15 重建（ignore_mismatched_sizes=True，
   HF 检测类别从 0 起、无背景类）；
3. 目标格式：class_labels(0..14) + boxes(cxcywh 归一化)；
4. 损失 = 模型内置损失（DETR 族为多损失加权和）；
5. 每轮评估 mAP@0.5；保存 best/last_generic_det.pth；
6. 自动登记进控制台共享模型库（architecture=hf_det_part）。

【用法】
    python train_generic_det.py --data_root "d:\\鸟类\\部位检测数据集" ^
        --base-model facebook/detr-resnet-50 --epochs 15
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset
from PIL import Image

from config_segpart import PART_NAMES
from dataset_segpart import load_annotations
from hub_resolver import resolve_base_model
from render_labels import points_to_boxes


def get_device() -> torch.device:
    """优先使用 GPU，不可用时回退 CPU。"""
    return torch.device('cuda' if torch.cuda.is_available() else 'cpu')


# ==================== 数据集 ====================
class GenericDetDataset(Dataset):
    """通用检测数据集：关键点框监督 + 底座配套 processor 归一化。"""

    def __init__(self, data_root: str, split: str, processor,
                 input_size: int = 512, max_samples=None, seed: int = 42):
        records = [r for r in load_annotations(str(Path(data_root) / 'annotations.csv'))
                   if r['split'] == split]
        if not records:
            records = load_annotations(str(Path(data_root) / 'annotations.csv'))
        if max_samples is not None and len(records) > max_samples:
            import random
            records = random.Random(seed).sample(records, max_samples)
        self.data_root = data_root
        self.records = records
        self.processor = processor
        self.input_size = input_size

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, idx: int) -> dict:
        rec = self.records[idx]
        image = Image.open(
            os.path.join(self.data_root, rec['image_path'])).convert('RGB')
        w, h = rec['orig_width'], rec['orig_height']
        size = self.input_size
        image = image.resize((size, size), Image.BILINEAR)
        pixel_values = self.processor(images=image, return_tensors='pt')[
            'pixel_values'][0]

        # 关键点 → cxcywh 归一化框（类别 0 起，与 resized 图共用归一化坐标）
        boxes, labels = [], []
        for part, x1, y1, x2, y2 in points_to_boxes(rec['points'], w, h):
            boxes.append([(x1 + x2) / 2 / w, (y1 + y2) / 2 / h,
                          (x2 - x1) / w, (y2 - y1) / h])
            labels.append(PART_NAMES.index(part))
        target = {
            'boxes': torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4),
            'class_labels': torch.tensor(labels, dtype=torch.long),
        }
        return {
            'pixel_values': pixel_values,
            'boxes': target['boxes'],                # cxcywh 归一化
            'class_labels': target['class_labels'],
            'boxes_xyxy_512': torch.tensor(
                [[b[0] * size - b[2] * size / 2, b[1] * size - b[3] * size / 2,
                  b[0] * size + b[2] * size / 2, b[1] * size + b[3] * size / 2]
                 for b in boxes], dtype=torch.float32).reshape(-1, 4),
            'image_id': rec['image_id'],
        }


def det_collate(batch: List[dict]) -> dict:
    """合并函数：目标框为变长张量，保持逐样本列表形式。"""
    return {
        'pixel_values': torch.stack([b['pixel_values'] for b in batch]),
        'boxes': [b['boxes'] for b in batch],
        'class_labels': [b['class_labels'] for b in batch],
        'boxes_xyxy_512': [b['boxes_xyxy_512'] for b in batch],
        'image_id': [b['image_id'] for b in batch],
    }


def make_hf_targets(boxes, class_labels) -> List[dict]:
    """把 collate 后的目标转为 HF 检测模型 forward(labels=...) 格式。"""
    return [{'boxes': b, 'class_labels': c}
            for b, c in zip(boxes, class_labels)]


# ==================== 评估：mAP@0.5 ====================
def _iou_matrix(boxes_a: torch.Tensor, boxes_b: torch.Tensor) -> torch.Tensor:
    """两组 xyxy 框的 IoU 矩阵 (N, M)。"""
    if boxes_a.numel() == 0 or boxes_b.numel() == 0:
        return torch.zeros((boxes_a.shape[0], boxes_b.shape[0]))
    area_a = (boxes_a[:, 2] - boxes_a[:, 0]) * (boxes_a[:, 3] - boxes_a[:, 1])
    area_b = (boxes_b[:, 2] - boxes_b[:, 0]) * (boxes_b[:, 3] - boxes_b[:, 1])
    lt = torch.max(boxes_a[:, None, :2], boxes_b[None, :, :2])
    rb = torch.min(boxes_a[:, None, 2:], boxes_b[None, :, 2:])
    wh = (rb - lt).clamp(min=0)
    inter = wh[..., 0] * wh[..., 1]
    return inter / (area_a[:, None] + area_b[None, :] - inter + 1e-9)


@torch.no_grad()
def evaluate_map50(model, processor, loader, device,
                   score_threshold: float = 0.05) -> float:
    """简化 mAP@0.5：逐类全点插值 AP，贪心 IoU 匹配。"""
    model.eval()
    # 汇总：每类 [(score, is_tp)] 与每类 GT 总数
    detections = {c: [] for c in range(len(PART_NAMES))}
    gt_count = {c: 0 for c in range(len(PART_NAMES))}
    post = getattr(processor, 'post_process_object_detection', None)

    for batch in loader:
        pixel_values = batch['pixel_values'].to(device)
        outputs = model(pixel_values=pixel_values)
        size = pixel_values.shape[-1]
        if post is not None:
            results = post(outputs, threshold=score_threshold,
                           target_sizes=[(size, size)] * pixel_values.shape[0])
        else:  # 无 processor 后处理时直接读原始输出（少数模型）
            results = [{'boxes': torch.zeros(0, 4), 'scores': torch.zeros(0),
                        'labels': torch.zeros(0, dtype=torch.long)}
                       for _ in range(pixel_values.shape[0])]

        for i, res in enumerate(results):
            gt_boxes = batch['boxes_xyxy_512'][i].to(device)
            gt_labels = batch['class_labels'][i].to(device)
            pred_boxes = res['boxes'].to(device)
            pred_scores = res['scores'].to(device)
            pred_labels = res['labels'].to(device)
            for c in range(len(PART_NAMES)):
                gt_c = gt_count[c]
                gt_count[c] += int((gt_labels == c).sum().item())
                mask = pred_labels == c
                if int(mask.sum()) == 0:
                    continue
                pb = pred_boxes[mask]
                ps = pred_scores[mask]
                gb = gt_boxes[gt_labels == c]
                iou = _iou_matrix(pb, gb)
                # 按置信度降序贪心匹配 GT
                order = torch.argsort(ps, descending=True)
                matched = set()
                for rank in order.tolist():
                    best_j, best_iou = -1, 0.5
                    for j in range(gb.shape[0]):
                        if j in matched:
                            continue
                        if iou[rank, j].item() > best_iou:
                            best_iou, best_j = iou[rank, j].item(), j
                    if best_j >= 0:
                        matched.add(best_j)
                        detections[c].append((float(ps[rank]), True))
                    else:
                        detections[c].append((float(ps[rank]), False))

    # 逐类 AP（全点插值）
    aps = []
    for c in range(len(PART_NAMES)):
        if gt_count[c] == 0:
            continue
        dets = sorted(detections[c], key=lambda x: -x[0])
        tp_flags = np.array([d[1] for d in dets], dtype=np.float64)
        if len(dets) == 0:
            aps.append(0.0)
            continue
        tps = np.cumsum(tp_flags)
        fps = np.cumsum(1.0 - tp_flags)
        recall = tps / max(1, gt_count[c])
        precision = tps / np.maximum(1e-9, tps + fps)
        # 全点插值 AP
        mrec = np.concatenate(([0.0], recall, [1.0]))
        mpre = np.concatenate(([0.0], precision, [0.0]))
        for k in range(len(mpre) - 1, 0, -1):
            mpre[k - 1] = max(mpre[k - 1], mpre[k])
        ap = float(np.sum((mrec[1:] - mrec[:-1]) * mpre[1:]))
        aps.append(ap)
    model.train()
    return float(np.mean(aps)) if aps else 0.0


# ==================== 产物与登记 ====================
def find_shared_model_library():
    """探测控制台共享模型库（环境变量优先，其次包内/开发版相对探测）。"""
    env = os.environ.get('BIRD_SHARED_MODEL_LIBRARY')
    if env and (Path(env) / 'registry.json').is_file():
        return Path(env)
    here = Path(__file__).resolve().parent
    for cand in (here.parent / 'data' / 'model_library',
                 *[(p / 'iNaturalist' / 'iNaturalist' / 'data' / 'model_library')
                   for p in [here.parent] + list(here.parents)]):
        if (cand / 'registry.json').is_file():
            return cand
    return None


def register_to_shared_library(out_dir: Path, map50: float, base_name: str,
                               hub_model_id: str, register: bool = True):
    """产物登记进控制台共享模型库（失败只提示，不影响产物）。"""
    if not register:
        print('已按参数跳过控制台模型库登记（--no-register）。')
        return
    lib_root = find_shared_model_library()
    if not lib_root:
        print(f'【提示】未探测到共享模型库，可手动导入：{out_dir}')
        return
    try:
        console_dir = lib_root.parent.parent
        if str(console_dir) not in sys.path:
            sys.path.insert(0, str(console_dir))
        import shutil
        import model_library as console_ml
        dir_name, _, _ = console_ml.next_model_name(base_name, root=lib_root)
        target = Path(lib_root) / dir_name
        target.mkdir(parents=True, exist_ok=True)
        for name in ('best_generic_det.pth', 'last_generic_det.pth',
                     'class_names.json', 'meta.json', 'evaluation_report.md'):
            src = out_dir / name
            if src.is_file():
                shutil.copy2(src, target / name)
        record = console_ml.register_model(
            target, class_count=len(PART_NAMES),
            accuracy=round(float(map50), 4),
            source=f'通用检测微调（底座 {hub_model_id}）',
            architecture='hf_det_part', metric_name='map50', root=lib_root)
        print(f'已登记进控制台模型库：{record.get("name")}（架构 hf_det_part；'
              f'「视觉分析 → 部位模型」下拉刷新后可见）')
    except Exception as exc:
        print(f'【警告】模型库登记失败：{exc}（可手动导入：{out_dir}）')


# ==================== 训练主流程 ====================
def run_training(args: dict) -> int:
    from transformers import AutoImageProcessor, AutoModelForObjectDetection

    data_root = args['data_root']
    base_model = args['base_model']
    epochs = int(args.get('epochs') or 15)
    batch_size = int(args.get('batch_size') or 2)
    accum = max(1, int(args.get('grad_accum') or 2))
    lr = float(args.get('lr') or 1e-4)
    input_size = int(args.get('input_size') or 512)
    base_name = args.get('model_name') or '通用部位检测模型'

    device = get_device()
    print(f'【环境】设备: {device}；底座: {base_model}；轮数: {epochs}；'
          f'批大小: {batch_size} × 累积 {accum}')

    resolved = resolve_base_model(base_model)
    try:
        processor = AutoImageProcessor.from_pretrained(resolved)
        model = AutoModelForObjectDetection.from_pretrained(
            resolved, num_labels=len(PART_NAMES),
            ignore_mismatched_sizes=True)
    except Exception as exc:
        print(f'【错误】底座加载失败：{exc}\n'
              f'请确认该模型兼容 AutoModelForObjectDetection '
              f'（检测类架构），或改用本地目录。')
        return 2
    for attr in ('do_resize', 'do_center_crop', 'do_pad', 'crop_size'):
        if hasattr(processor, attr):
            try:
                setattr(processor, attr, False)
            except Exception:
                pass

    out_dir = Path(__file__).resolve().parent / 'training_output' / 'generic_det'
    out_dir.mkdir(parents=True, exist_ok=True)

    train_set = GenericDetDataset(data_root, 'train', processor, input_size,
                                  max_samples=args.get('max_samples'))
    if len(train_set) < 4:
        print('【错误】训练样本不足 4 张，请检查数据目录。')
        return 2
    n_val = max(1, int(len(train_set) * 0.15))
    val_set = torch.utils.data.Subset(
        train_set, range(len(train_set) - n_val, len(train_set)))
    train_set = torch.utils.data.Subset(
        train_set, range(len(train_set) - n_val))
    train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True,
                              num_workers=0, collate_fn=det_collate, drop_last=True)
    val_loader = DataLoader(val_set, batch_size=batch_size, shuffle=False,
                            num_workers=0, collate_fn=det_collate)
    print(f'【数据】训练 {len(train_set)} 张 / 验证 {len(val_set)} 张')

    model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.0001)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    scaler = torch.amp.GradScaler('cuda', enabled=device.type == 'cuda')

    best_map = -1.0
    report_lines = []
    model.train()
    t0 = time.time()
    for epoch in range(1, epochs + 1):
        loss_sum, steps = 0.0, 0
        optimizer.zero_grad(set_to_none=True)
        for i, batch in enumerate(train_loader):
            pixel_values = batch['pixel_values'].to(device)
            targets = make_hf_targets(batch['boxes'], batch['class_labels'])
            targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
            with torch.amp.autocast(device_type='cuda',
                                    enabled=device.type == 'cuda'):
                outputs = model(pixel_values=pixel_values, labels=targets)
                loss = outputs.loss if outputs.loss is not None else \
                    sum(outputs.loss_dict.values())
            scaler.scale(loss / accum).backward()
            if (i + 1) % accum == 0:
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad(set_to_none=True)
            loss_sum += float(loss.item())
            steps += 1
        scheduler.step()

        map50 = evaluate_map50(model, processor, val_loader, device)
        line = f'【轮次 {epoch}/{epochs}】损失 {loss_sum / max(1, steps):.4f}；mAP@0.5 {map50:.4f}'
        print(line)
        report_lines.append(line)
        if map50 > best_map:
            best_map = map50
            torch.save({
                'model_state': {k: v.detach().cpu()
                                for k, v in model.state_dict().items()},
                'task_type': 'detection',
                'hub_model_id': base_model,
                'class_names': list(PART_NAMES),
                'map50': float(best_map), 'input_size': input_size,
                'epoch': epoch,
            }, str(out_dir / 'best_generic_det.pth'))

    torch.save({
        'model_state': {k: v.detach().cpu()
                        for k, v in model.state_dict().items()},
        'task_type': 'detection', 'hub_model_id': base_model,
        'class_names': list(PART_NAMES),
        'map50': float(max(best_map, 0.0)), 'input_size': input_size,
        'epoch': epochs,
    }, str(out_dir / 'last_generic_det.pth'))
    (out_dir / 'class_names.json').write_text(json.dumps(
        list(PART_NAMES), ensure_ascii=False, indent=2), encoding='utf-8')
    (out_dir / 'meta.json').write_text(json.dumps({
        'architecture': 'hf_det_part', 'task_type': 'detection',
        'metric_name': 'map50',
        'metrics': {'map50': round(float(best_map), 4)},
        'hub_model_id': base_model, 'class_count': len(PART_NAMES),
        'source': f'通用检测微调（底座 {base_model}）',
        'trained_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    (out_dir / 'evaluation_report.md').write_text(
        f'# 通用部位检测训练报告\n\n- 底座：{base_model}\n'
        f'- 最佳 mAP@0.5：{best_map:.4f}\n'
        f'- 总耗时：{(time.time() - t0) / 60:.1f} 分钟\n\n'
        + '\n'.join(report_lines) + '\n', encoding='utf-8')

    print(f'训练结束。最佳 mAP@0.5 {best_map:.4f}；产物目录：{out_dir}')
    register_to_shared_library(out_dir, best_map, base_name, base_model,
                               bool(args.get('register_shared', True)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='通用检测底座微调（部位检测）')
    parser.add_argument('--data_root', required=True, help='部位检测数据集根目录')
    parser.add_argument('--base-model', dest='base_model', required=True,
                        help='HF/魔搭检测模型 ID 或本地模型目录')
    parser.add_argument('--epochs', type=int, default=15)
    parser.add_argument('--batch-size', dest='batch_size', type=int, default=2)
    parser.add_argument('--grad-accum', dest='grad_accum', type=int, default=2)
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--input-size', dest='input_size', type=int, default=512)
    parser.add_argument('--model-name', dest='model_name',
                        default='通用部位检测模型', help='登记进模型库的基名')
    parser.add_argument('--max_samples', type=int, default=None,
                        help='冒烟测试：每 split 最多取样数')
    parser.add_argument('--no-register', dest='register_shared',
                        action='store_false')
    args = parser.parse_args()
    return run_training(vars(args))


if __name__ == '__main__':
    sys.exit(main())
