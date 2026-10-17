# -*- coding: utf-8 -*-
"""
train_generic_seg.py —— 通用分割底座微调（HuggingFace / 魔搭任意分割架构）

作者：天咏（明月照今日，风摇过两川。）

【功能】
以任意兼容 transformers AutoModelForSemanticSegmentation 的开源模型为底座
（SegFormer / DPT / Mask2Former / OneFormer / BEiT 等，来自 HuggingFace 或
魔搭 ModelScope），在部位检测数据集上微调为 16 类（背景 + 15 部位）分割模型。

【流程】
1. hub_resolver 解析底座（本地目录 → 魔搭 → HF 镜像）；
2. AutoImageProcessor 负责图像归一化（自动关闭其 resize/crop，
   图像统一预缩放到 --input-size）；
3. 分类头按 num_labels=16 重建（ignore_mismatched_sizes=True）；
4. 损失 = 模型内置损失（含标签监督）+ Dice（经典逐像素头适用；
   查询式头如 Mask2Former 自动跳过 Dice）；
5. 每轮评估 mIoU 与点命中率；保存 best/last_generic_seg.pth；
6. 自动登记进控制台共享模型库（architecture=hf_seg_part, metric_name=miou）。

【用法】
    python train_generic_seg.py --data_root "d:\\鸟类\\部位检测数据集" ^
        --base-model nvidia/segformer-b0-finetuned-ade-512-512 --epochs 15
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
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset
from PIL import Image

from config_segpart import PART_NAMES
from dataset_segpart import load_annotations, render_part_mask
from hub_resolver import resolve_base_model


def get_device() -> torch.device:
    """优先使用 GPU，不可用时回退 CPU。"""
    return torch.device('cuda' if torch.cuda.is_available() else 'cpu')


# ==================== 数据集 ====================
class GenericSegDataset(Dataset):
    """通用分割数据集：关键点掩码监督 + 底座配套 processor 归一化。"""

    def __init__(self, data_root: str, split: str, processor,
                 render_size: int = 512, max_samples=None, seed: int = 42):
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
        self.render_size = render_size

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, idx: int) -> dict:
        rec = self.records[idx]
        image = Image.open(
            os.path.join(self.data_root, rec['image_path'])).convert('RGB')
        size = self.render_size
        image = image.resize((size, size), Image.BILINEAR)
        pixel_values = self.processor(images=image, return_tensors='pt')[
            'pixel_values'][0]
        mask = torch.from_numpy(render_part_mask(
            size, size, rec['points'], len(PART_NAMES) + 1)).long()
        return {
            'pixel_values': pixel_values,
            'mask': mask,
            'points': rec['points'],          # 供点命中率评估
            'image_id': rec['image_id'],
        }


def seg_collate(batch: List[dict]) -> dict:
    """合并函数：points 为逐样本字典（键集合不同），保持列表形式。"""
    collated = torch.utils.data.default_collate(
        [{k: b[k] for k in ('pixel_values', 'mask', 'image_id')} for b in batch])
    collated['points'] = [b['points'] for b in batch]
    return collated


# ==================== 损失与评估 ====================
def dice_loss(logits: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    """多类 Dice 损失（经典逐像素分割头适用）。"""
    num_classes = logits.shape[1]
    probs = F.softmax(logits.float(), dim=1)
    target_onehot = F.one_hot(target, num_classes).permute(0, 3, 1, 2).float()
    dims = (0, 2, 3)
    inter = (probs * target_onehot).sum(dims)
    union = probs.sum(dims) + target_onehot.sum(dims)
    return 1.0 - ((2.0 * inter + 1e-5) / (union + 1e-5)).mean()


def predict_seg_maps(model, outputs, masks: torch.Tensor,
                     query_based: bool) -> torch.Tensor:
    """把模型输出统一为 (B, H, W) 类别图（经典头插值；查询头走后处理）。"""
    if query_based:
        maps = model.post_process_semantic_segmentation(
            outputs, target_sizes=[masks.shape[1:]] * masks.shape[0])
        return torch.stack(maps).to(masks.device)
    logits = outputs.logits
    if logits.shape[-2:] != masks.shape[-2:]:
        logits = F.interpolate(logits.float(), size=masks.shape[-2:],
                               mode='bilinear', align_corners=False)
    return logits.argmax(dim=1)


@torch.no_grad()
def evaluate(model, loader, device, query_based: bool):
    """评估 mIoU 与点命中率（标注点处预测类别是否命中）。"""
    model.eval()
    num_classes = len(PART_NAMES) + 1
    iou_sum = torch.zeros(num_classes, dtype=torch.float64)
    iou_count = torch.zeros(num_classes, dtype=torch.float64)
    hit, total = 0, 0
    for batch in loader:
        pixel_values = batch['pixel_values'].to(device)
        masks = batch['mask'].to(device)
        outputs = model(pixel_values=pixel_values)
        preds = predict_seg_maps(model, outputs, masks, query_based)
        for cls in range(num_classes):
            pred_c, true_c = preds == cls, masks == cls
            union = (pred_c | true_c).sum().item()
            if union > 0:
                iou_sum[cls] += (pred_c & true_c).sum().item() / union
                iou_count[cls] += 1
        for i, points in enumerate(batch['points']):
            for part, (px, py) in points.items():
                cls_idx = PART_NAMES.index(part) + 1
                x = min(int(px * masks.shape[2]), masks.shape[2] - 1)
                y = min(int(py * masks.shape[1]), masks.shape[1] - 1)
                if int(preds[i, y, x].item()) == cls_idx:
                    hit += 1
                total += 1
    model.train()
    miou = float((iou_sum / iou_count.clamp(min=1)).mean().item())
    return miou, hit / max(1, total)


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


def register_to_shared_library(out_dir: Path, miou: float, base_name: str,
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
        for name in ('best_generic_seg.pth', 'last_generic_seg.pth',
                     'class_names.json', 'meta.json', 'evaluation_report.md'):
            src = out_dir / name
            if src.is_file():
                shutil.copy2(src, target / name)
        record = console_ml.register_model(
            target, class_count=len(PART_NAMES) + 1,
            accuracy=round(float(miou), 4),
            source=f'通用分割微调（底座 {hub_model_id}）',
            architecture='hf_seg_part', metric_name='miou', root=lib_root)
        print(f'已登记进控制台模型库：{record.get("name")}（架构 hf_seg_part；'
              f'「视觉分析 → 部位模型」下拉刷新后可见）')
    except Exception as exc:
        print(f'【警告】模型库登记失败：{exc}（可手动导入：{out_dir}）')


# ==================== 训练主流程 ====================
def run_training(args: dict) -> int:
    from transformers import (AutoImageProcessor,
                              AutoModelForSemanticSegmentation)

    data_root = args['data_root']
    base_model = args['base_model']
    epochs = int(args.get('epochs') or 15)
    batch_size = int(args.get('batch_size') or 2)
    accum = max(1, int(args.get('grad_accum') or 2))
    lr = float(args.get('lr') or 6e-5)
    input_size = int(args.get('input_size') or 512)
    base_name = args.get('model_name') or '通用部位分割模型'

    device = get_device()
    print(f'【环境】设备: {device}；底座: {base_model}；轮数: {epochs}；'
          f'批大小: {batch_size} × 累积 {accum}')

    # 底座解析 + 模型构建（分类头重建为 16 类）
    resolved = resolve_base_model(base_model)
    try:
        processor = AutoImageProcessor.from_pretrained(resolved)
        model = AutoModelForSemanticSegmentation.from_pretrained(
            resolved, num_labels=len(PART_NAMES) + 1,
            ignore_mismatched_sizes=True)
    except Exception as exc:
        print(f'【错误】底座加载失败：{exc}\n'
              f'请确认该模型兼容 AutoModelForSemanticSegmentation '
              f'（分割类架构），或改用本地目录。')
        return 2
    # 关闭 processor 自带缩放/裁剪：图像已统一预缩放到 input_size
    for attr in ('do_resize', 'do_center_crop', 'do_random_crop',
                 'do_pad', 'crop_size'):
        if hasattr(processor, attr):
            try:
                setattr(processor, attr, False)
            except Exception:
                pass
    query_based = hasattr(model, 'post_process_semantic_segmentation')
    print(f'【底座】已加载 {base_model}；头类型: '
          f'{"查询式（Mask2Former 族）" if query_based else "经典逐像素头"}')

    out_dir = Path(__file__).resolve().parent / 'training_output' / 'generic_seg'
    out_dir.mkdir(parents=True, exist_ok=True)

    # 数据集：train 末段 15% 作验证（与 v7 训练一致）
    train_set = GenericSegDataset(data_root, 'train', processor, input_size,
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
                              num_workers=0, collate_fn=seg_collate, drop_last=True)
    val_loader = DataLoader(val_set, batch_size=batch_size, shuffle=False,
                            num_workers=0, collate_fn=seg_collate)
    print(f'【数据】训练 {len(train_set)} 张 / 验证 {len(val_set)} 张')

    # 空间对齐探针：processor 输出若非 input_size，按实际尺寸重渲染掩码
    probe = train_set[0]
    spatial = tuple(probe['pixel_values'].shape[-2:])
    if spatial != (input_size, input_size):
        print(f'【提示】底座 processor 固定输出 {spatial}，掩码按该尺寸渲染')
        base_ds = train_set.dataset if isinstance(train_set, torch.utils.data.Subset) \
            else train_set
        base_ds.render_size = min(spatial)
        del probe, train_set, val_set, train_loader, val_loader
        train_set = GenericSegDataset(data_root, 'train', processor,
                                      min(spatial),
                                      max_samples=args.get('max_samples'))
        n_val = max(1, int(len(train_set) * 0.15))
        val_set = torch.utils.data.Subset(
            train_set, range(len(train_set) - n_val, len(train_set)))
        train_set = torch.utils.data.Subset(
            train_set, range(len(train_set) - n_val))
        train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True,
                                  num_workers=0, collate_fn=seg_collate, drop_last=True)
        val_loader = DataLoader(val_set, batch_size=batch_size, shuffle=False,
                                num_workers=0, collate_fn=seg_collate)

    model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.05)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    scaler = torch.amp.GradScaler('cuda', enabled=device.type == 'cuda')

    best_miou, best_hit = -1.0, 0.0
    report_lines = []
    model.train()
    t0 = time.time()
    for epoch in range(1, epochs + 1):
        loss_sum, steps = 0.0, 0
        optimizer.zero_grad(set_to_none=True)
        for i, batch in enumerate(train_loader):
            pixel_values = batch['pixel_values'].to(device)
            masks = batch['mask'].to(device)
            with torch.amp.autocast(device_type='cuda',
                                    enabled=device.type == 'cuda'):
                outputs = model(pixel_values=pixel_values, labels=masks)
                loss = outputs.loss
                if not query_based:  # 经典头叠加 Dice
                    logits = outputs.logits
                    if logits.shape[-2:] != masks.shape[-2:]:
                        logits = F.interpolate(
                            logits.float(), size=masks.shape[-2:],
                            mode='bilinear', align_corners=False)
                    loss = loss + 0.3 * dice_loss(logits, masks)
            scaler.scale(loss / accum).backward()
            if (i + 1) % accum == 0:
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad(set_to_none=True)
            loss_sum += float(loss.item())
            steps += 1
        scheduler.step()

        miou, hit = evaluate(model, val_loader, device, query_based)
        line = (f'【轮次 {epoch}/{epochs}】损失 {loss_sum / max(1, steps):.4f}；'
                f'mIoU {miou:.4f}；点命中率 {hit * 100:.2f}%')
        print(line)
        report_lines.append(line)
        if miou > best_miou:
            best_miou, best_hit = miou, hit
            torch.save({
                'model_state': {k: v.detach().cpu()
                                for k, v in model.state_dict().items()},
                'task_type': 'segmentation',
                'hub_model_id': base_model,
                'class_names': ['background'] + list(PART_NAMES),
                'miou': float(best_miou), 'input_size': input_size,
                'query_based': bool(query_based),
                'epoch': epoch,
            }, str(out_dir / 'best_generic_seg.pth'))

    # 末轮权重 + 元信息
    torch.save({
        'model_state': {k: v.detach().cpu()
                        for k, v in model.state_dict().items()},
        'task_type': 'segmentation', 'hub_model_id': base_model,
        'class_names': ['background'] + list(PART_NAMES),
        'miou': float(max(best_miou, 0.0)), 'input_size': input_size,
        'query_based': bool(query_based), 'epoch': epochs,
    }, str(out_dir / 'last_generic_seg.pth'))
    (out_dir / 'class_names.json').write_text(json.dumps(
        ['background'] + list(PART_NAMES), ensure_ascii=False, indent=2),
        encoding='utf-8')
    (out_dir / 'meta.json').write_text(json.dumps({
        'architecture': 'hf_seg_part', 'task_type': 'segmentation',
        'metric_name': 'miou',
        'metrics': {'miou': round(float(best_miou), 4)},
        'hub_model_id': base_model, 'class_count': len(PART_NAMES) + 1,
        'source': f'通用分割微调（底座 {base_model}）',
        'trained_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    (out_dir / 'evaluation_report.md').write_text(
        f'# 通用部位分割训练报告\n\n- 底座：{base_model}\n'
        f'- 最佳 mIoU：{best_miou:.4f}（点命中率 {best_hit * 100:.2f}%）\n'
        f'- 总耗时：{(time.time() - t0) / 60:.1f} 分钟\n\n'
        + '\n'.join(report_lines) + '\n', encoding='utf-8')

    print(f'训练结束。最佳 mIoU {best_miou:.4f}；产物目录：{out_dir}')
    register_to_shared_library(out_dir, best_miou, base_name, base_model,
                               bool(args.get('register_shared', True)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='通用分割底座微调（部位分割）')
    parser.add_argument('--data_root', required=True, help='部位检测数据集根目录')
    parser.add_argument('--base-model', dest='base_model', required=True,
                        help='HF/魔搭分割模型 ID 或本地模型目录')
    parser.add_argument('--epochs', type=int, default=15)
    parser.add_argument('--batch-size', dest='batch_size', type=int, default=2)
    parser.add_argument('--grad-accum', dest='grad_accum', type=int, default=2)
    parser.add_argument('--lr', type=float, default=6e-5)
    parser.add_argument('--input-size', dest='input_size', type=int, default=512)
    parser.add_argument('--model-name', dest='model_name',
                        default='通用部位分割模型', help='登记进模型库的基名')
    parser.add_argument('--max_samples', type=int, default=None,
                        help='冒烟测试：每 split 最多取样数')
    parser.add_argument('--no-register', dest='register_shared',
                        action='store_false')
    args = parser.parse_args()
    return run_training(vars(args))


if __name__ == '__main__':
    sys.exit(main())
