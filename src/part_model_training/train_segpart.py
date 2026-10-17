# -*- coding: utf-8 -*-
# ============================================================
# 项目签名 | Project Signature
# 作者：陈建宇，号天咏
# 题记：明月照今日，风遥照两川
# 项目：SegFormer 部位分割模型（v7 架构迁移版）
# ============================================================
"""
部位分割模型训练脚本（迁移学习）

【流程】
1. 加载 nvidia/segformer-b2-finetuned-ade-512-512 预训练权重（迁移学习，
   分类头按 ignore_mismatched_sizes 重建为 16 类）
2. 差异化学习率：编码器 ×0.1、解码器 ×1、分类头 ×5、MSFPA/BA-MSR ×1
3. 损失 = CE + Dice + 边界损失（辅助头损失 ×0.4 叠加）
4. 每轮评估 mIoU 与点命中率（标注点处预测类别是否命中）
5. 保存 best_segpart.pth / last_segpart.pth / class_names.json / meta.json
6. 自动登记进控制台共享模型库（architecture=segformer_part, metric_name=miou）

用法：
    python train_segpart.py --data_root "d:\鸟类\部位检测数据集"
    python train_segpart.py --data_root ... --epochs 2 --max_samples 30  # 冒烟
    python train_segpart.py --data_root ... --resume --epochs 20  # 断点续训（追加 20 轮）
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

from config_segpart import CONFIG_SEGPART, PART_NAMES
from dataset_segpart import PartSegDataset
from model_segpart import build_enhanced_segformer


# ==================== 工具函数 ====================
def get_device() -> torch.device:
    """优先使用 GPU，不可用时回退 CPU。"""
    return torch.device('cuda' if torch.cuda.is_available() else 'cpu')


def set_seed(seed: int) -> None:
    """固定随机种子，保证结果可复现。"""
    import random
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def build_optimizer(model, config):
    """按模块划分参数组，实现差异化学习率。"""
    encoder_params, decoder_params, classifier_params = [], [], []
    msfpa_params, ba_msr_params, other_params = [], [], []
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        if name.startswith('segformer.segformer.encoder'):
            encoder_params.append(param)
        elif 'decode_head' in name or 'aux_head' in name:
            decoder_params.append(param)
        elif 'classifier' in name:
            classifier_params.append(param)
        elif name.startswith('msfpa'):
            msfpa_params.append(param)
        elif name.startswith('ba_msr'):
            ba_msr_params.append(param)
        else:
            other_params.append(param)

    groups = [
        {'params': encoder_params, 'lr': config.base_lr * config.encoder_lr_scale},
        {'params': decoder_params, 'lr': config.base_lr * config.decoder_lr_scale},
        {'params': classifier_params, 'lr': config.base_lr * config.classifier_lr_scale},
        {'params': msfpa_params, 'lr': config.base_lr * config.msfpa_lr_scale},
        {'params': ba_msr_params, 'lr': config.base_lr * config.ba_msr_lr_scale},
    ]
    if other_params:
        groups.append({'params': other_params, 'lr': config.base_lr})
    return torch.optim.AdamW(groups, lr=config.base_lr,
                             weight_decay=config.weight_decay)


def poly_lr_lambda(iters_per_epoch: int, total_epochs: int,
                   warmup_iters: int, power: float):
    """Poly 学习率调度（含线性预热）。"""
    total_iters = iters_per_epoch * total_epochs

    def fn(step: int) -> float:
        if step < warmup_iters:
            return float(step + 1) / float(max(1, warmup_iters))
        progress = (step - warmup_iters) / max(1, total_iters - warmup_iters)
        return (1.0 - progress) ** power
    return fn


# ==================== 损失函数 ====================
def dice_loss(logits: torch.Tensor, target: torch.Tensor,
              num_classes: int) -> torch.Tensor:
    """多类 Dice 损失（忽略背景之外的类别平滑处理）。"""
    probs = F.softmax(logits.float(), dim=1)
    target_onehot = F.one_hot(target, num_classes).permute(0, 3, 1, 2).float()
    dims = (0, 2, 3)
    intersection = (probs * target_onehot).sum(dims)
    union = probs.sum(dims) + target_onehot.sum(dims)
    dice = (2.0 * intersection + 1e-5) / (union + 1e-5)
    return 1.0 - dice.mean()


def boundary_loss(logits: torch.Tensor, target: torch.Tensor,
                  sigma: float = 10.0) -> torch.Tensor:
    """边界损失：预测概率图与真值 one-hot 的 Sobel 边界响应做 L1 对齐。"""
    sobel_x = torch.tensor([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]],
                           dtype=torch.float32, device=logits.device).view(1, 1, 3, 3)
    sobel_y = torch.tensor([[-1, -2, -1], [0, 0, 0], [1, 2, 1]],
                           dtype=torch.float32, device=logits.device).view(1, 1, 3, 3)

    def soft_boundary(x: torch.Tensor) -> torch.Tensor:
        # x: (B, C, H, W) → (B, C, H, W) 各通道边界响应
        b, c, h, w = x.shape
        xp = F.pad(x.reshape(b * c, 1, h, w), (1, 1, 1, 1), mode='replicate')
        gx = F.conv2d(xp, sobel_x).reshape(b, c, h, w)
        gy = F.conv2d(xp, sobel_y).reshape(b, c, h, w)
        return torch.exp(-(gx ** 2 + gy ** 2) / (2 * sigma ** 2))

    pred_b = soft_boundary(F.softmax(logits.float(), dim=1))
    true_onehot = F.one_hot(target, logits.shape[1]).permute(0, 3, 1, 2).float()
    true_b = soft_boundary(true_onehot)
    return F.l1_loss(pred_b, true_b)


def compute_class_weights(train_set, num_classes: int,
                          max_samples: int = 100) -> torch.Tensor:
    """统计训练集掩码类别频率，按逆平方根加权（缓解背景主导的类别失衡）。

    背景≈77%、小部位（眼睛）仅≈0.02%，无权重 CE 会收敛到「全背景」塌缩解；
    逆平方根加权既放大小类别梯度，又不至于像逆频率那样过度爆炸。
    """
    n = len(train_set)
    if n <= max_samples:
        indices = list(range(n))
    else:  # 均匀抽样，避免逐图统计全量掩码
        indices = np.linspace(0, n - 1, max_samples).astype(int).tolist()
    counts = torch.zeros(num_classes, dtype=torch.float64)
    for idx in indices:
        mask = train_set[idx]['mask']
        counts += torch.bincount(mask.flatten(),
                                 minlength=num_classes).double()
    freq = counts / counts.sum().clamp(min=1)
    weights = 1.0 / torch.sqrt(freq.clamp(min=1e-8))
    weights = weights / weights.mean()  # 归一化到均值 1
    weights = weights.clamp(max=30.0)   # 上限防止个别类权重爆炸
    return weights.float()


def compute_loss(outputs: dict, mask: torch.Tensor, config,
                 class_weights: torch.Tensor = None):
    """组合损失：加权 CE + Dice + 边界损失 + 辅助头加权 CE。"""
    logits = outputs['logits']
    if logits.shape[2:] != mask.shape[1:]:
        logits = F.interpolate(logits, size=mask.shape[1:], mode='bilinear',
                               align_corners=False)

    loss_ce = F.cross_entropy(logits.float(), mask, weight=class_weights)
    loss_dice = dice_loss(logits, mask, config.num_classes)
    loss_bound = boundary_loss(logits, mask, config.boundary_sigma)
    total = (config.ce_weight * loss_ce
             + config.dice_weight * loss_dice
             + config.boundary_weight * loss_bound)

    aux_logits = outputs.get('aux_logits')
    if aux_logits is not None:
        if aux_logits.shape[2:] != mask.shape[1:]:
            aux_logits = F.interpolate(aux_logits, size=mask.shape[1:],
                                       mode='bilinear', align_corners=False)
        loss_aux = F.cross_entropy(aux_logits.float(), mask,
                                   weight=class_weights)
        total = total + config.aux_loss_weight * loss_aux
    else:
        loss_aux = torch.zeros((), device=mask.device)

    return total, {
        'ce': float(loss_ce.detach().cpu()),
        'dice': float(loss_dice.detach().cpu()),
        'boundary': float(loss_bound.detach().cpu()),
        'aux': float(loss_aux.detach().cpu()),
    }


# ==================== 评估 ====================
@torch.no_grad()
def evaluate(model, loader, device, num_classes: int):
    """评估 mIoU 与点命中率。"""
    model.eval()
    iou_sum = torch.zeros(num_classes, dtype=torch.float64)
    iou_count = torch.zeros(num_classes, dtype=torch.float64)
    point_hit, point_total = 0, 0

    for batch in loader:
        images = batch['image'].to(device)
        masks = batch['mask'].to(device)
        outputs = model(pixel_values=images)
        logits = outputs['logits']
        preds = F.interpolate(logits, size=masks.shape[1:], mode='bilinear',
                              align_corners=False).argmax(dim=1)

        # mIoU 统计（逐类别交并比）
        for cls in range(num_classes):
            pred_c = preds == cls
            true_c = masks == cls
            inter = (pred_c & true_c).sum().item()
            union = (pred_c | true_c).sum().item()
            if union > 0:
                iou_sum[cls] += inter / union
                iou_count[cls] += 1

        # 点命中率：标注点处预测类别是否等于该部位类别
        for i in range(images.shape[0]):
            points = batch['points'][i]
            for part, coord in points.items():
                cls_idx = PART_NAMES.index(part) + 1
                px = min(int(coord[0] * masks.shape[2]), masks.shape[2] - 1)
                py = min(int(coord[1] * masks.shape[1]), masks.shape[1] - 1)
                if int(preds[i, py, px].item()) == cls_idx:
                    point_hit += 1
                point_total += 1

    miou = float((iou_sum / iou_count.clamp(min=1)).mean().item())
    hit_rate = point_hit / max(1, point_total)
    return miou, hit_rate


# ==================== 产物与登记 ====================
def write_sidecar(out_dir: Path, best_miou: float) -> None:
    """写类别名与元信息文件（class_names.json / meta.json）。"""
    try:
        names = ['background'] + list(PART_NAMES)
        (out_dir / 'class_names.json').write_text(
            json.dumps(names, ensure_ascii=False, indent=2), encoding='utf-8')
        meta = {
            'architecture': 'segformer_part',
            'task_type': 'segmentation',
            'metric_name': 'miou',
            'metrics': {'miou': round(float(best_miou), 4)},
            'class_count': len(names),
            'part_names': list(PART_NAMES),
            'source': 'v7部位分割模型',
            'trained_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        (out_dir / 'meta.json').write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as exc:
        print(f'【警告】类别/元信息文件写入失败：{exc}')


def find_shared_model_library() -> Path | None:
    """探测控制台共享模型库根目录（环境变量优先，其次上级目录相对探测）。"""
    env = os.environ.get('BIRD_SHARED_MODEL_LIBRARY')
    if env and (Path(env) / 'registry.json').is_file():
        return Path(env)
    here = Path(__file__).resolve().parent
    # 发布包布局：本模块位于 <打包源码>/part_model_training/，库在
    # <打包源码>/data/model_library/（优先于开发版探测，适配包内运行）
    pkg_candidate = here.parent / 'data' / 'model_library'
    if (pkg_candidate / 'registry.json').is_file():
        return pkg_candidate
    # 开发版布局：本项目位于 d:\鸟类\，控制台为同级 iNaturalist\iNaturalist
    for parent in [here.parent] + list(here.parents):
        candidate = parent / 'iNaturalist' / 'iNaturalist' / 'data' / 'model_library'
        if (candidate / 'registry.json').is_file():
            return candidate
    return None


def register_to_shared_library(out_dir: Path, best_miou: float,
                               base_name: str, register: bool = True) -> None:
    """把部位分割产物登记进控制台共享模型库（失败只提示，不影响产物）。"""
    if not register:
        print('已按参数跳过控制台模型库登记（--no-register）。')
        return
    lib_root = find_shared_model_library()
    if not lib_root:
        print('【提示】未探测到控制台共享模型库，已跳过自动登记；'
              f'可手动导入：{out_dir}')
        return
    console_dir = lib_root.parent.parent
    try:
        if str(console_dir) not in sys.path:
            sys.path.insert(0, str(console_dir))
        import model_library as console_ml
    except Exception as exc:
        print(f'【提示】加载控制台模型库模块失败，已跳过自动登记（{exc}）；'
              f'可手动导入：{out_dir}')
        return
    try:
        import shutil

        dir_name, _date_str, _version = console_ml.next_model_name(
            base_name, root=lib_root)
        target = Path(lib_root) / dir_name
        target.mkdir(parents=True, exist_ok=True)
        for name in ('best_segpart.pth', 'last_segpart.pth',
                     'class_names.json', 'meta.json', 'evaluation_report.md'):
            src = out_dir / name
            if src.is_file():
                shutil.copy2(src, target / name)
        record = console_ml.register_model(
            target, class_count=len(PART_NAMES) + 1,
            accuracy=round(float(best_miou), 4), source='v7部位分割模型',
            architecture='segformer_part', metric_name='miou',
            root=lib_root)
        print(f'已登记进控制台模型库：{record.get("name")}'
              f'（架构 segformer_part；控制台「视觉分析 → 部位模型」下拉可见，'
              f'重启控制台或在面板点「刷新」后生效）')
    except Exception as exc:
        print(f'【警告】登记进控制台模型库失败：{exc}（可手动导入：{out_dir}）')


# ==================== 训练主流程 ====================
def part_collate(batch: list) -> dict:
    """自定义合并函数：points 为逐样本字典（键集合不同），保持列表形式。"""
    tensor_keys = [k for k in batch[0] if k not in ('points',)]
    collated = torch.utils.data.default_collate(
        [{k: b[k] for k in tensor_keys} for b in batch])
    collated['points'] = [b['points'] for b in batch]
    return collated


def run_training(args: dict) -> int:
    config = CONFIG_SEGPART
    if args.get('data_root'):
        config.data_root = args['data_root']
    if args.get('input_size'):
        config.input_size = int(args['input_size'])
    if args.get('epochs'):
        config.epochs = int(args['epochs'])
    if args.get('batch_size'):
        config.batch_size = int(args['batch_size'])
    accum = max(1, int(args.get('grad_accum')
                        or config.gradient_accumulation))
    config.validate()
    set_seed(config.seed)

    device = get_device()
    print(f'【环境】设备: {device}；输入尺寸: {config.input_size}；'
          f'轮数: {config.epochs}；批大小: {config.batch_size}')

    out_dir = Path(config.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 数据集：训练集 + 验证集（从 train split 划出末段作验证）
    full_train = PartSegDataset(config.data_root, split='train',
                                input_size=config.input_size)
    val_ratio = 0.15
    val_count = max(1, int(len(full_train) * val_ratio)) if len(full_train) > 8 else 1
    train_set = PartSegDataset(config.data_root, split='train',
                               input_size=config.input_size,
                               max_samples=args.get('max_samples'),
                               seed=config.seed)
    if args.get('max_samples'):
        # 冒烟/子集模式：验证集取同批数据末段，避免额外加载整库
        n_val = min(val_count, max(1, len(train_set) // 5))
        val_set = torch.utils.data.Subset(train_set,
                                          range(len(train_set) - n_val, len(train_set)))
        train_set = torch.utils.data.Subset(train_set,
                                            range(len(train_set) - n_val))
    else:
        val_set = PartSegDataset(config.data_root, split='test',
                                 input_size=config.input_size)

    train_loader = DataLoader(train_set, batch_size=config.batch_size,
                              shuffle=True, num_workers=config.num_workers,
                              drop_last=True, collate_fn=part_collate)
    val_loader = DataLoader(val_set, batch_size=config.batch_size,
                            shuffle=False, num_workers=config.num_workers,
                            collate_fn=part_collate)

    print(f'【数据】训练 {len(train_set)} 张，验证 {len(val_set)} 张')

    # 类别权重：逆平方根频率加权，防止背景主导的塌缩（背景≈77%）
    class_weights = compute_class_weights(
        train_set, config.num_classes).to(device)
    w_min, w_max = float(class_weights.min()), float(class_weights.max())
    print(f'【类别权重】背景 {float(class_weights[0]):.2f}；'
          f'范围 [{w_min:.2f}, {w_max:.2f}]（均值归一）')

    # 模型（迁移学习：预训练权重 + 16 类分类头重建）
    model = build_enhanced_segformer(config).to(device)
    optimizer = build_optimizer(model, config)
    # 调度器以「累积步」为节拍（每 accum 个微批推进一步优化器）
    iters_per_epoch = max(1, -(-len(train_loader) // accum))  # 向上取整
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer, poly_lr_lambda(iters_per_epoch, config.epochs,
                                  config.warmup_iters, config.poly_power))
    scaler = torch.amp.GradScaler('cuda', enabled=config.use_amp and device.type == 'cuda')

    print(f'【显存优化】批大小 {config.batch_size} × 梯度累积 {accum} '
          f'= 等效批大小 {config.batch_size * accum}')

    best_miou = -1.0
    # 断点续训：热启动权重（优化器与调度器重置，轮次续编号，best 沿用历史最佳）
    start_epoch = 1
    if args.get('resume'):
        resume_path = Path(args['resume'])
        if resume_path.is_file():
            ckpt = torch.load(str(resume_path), map_location='cpu',
                              weights_only=False)
            model.load_state_dict(ckpt.get('model_state', ckpt), strict=True)
            start_epoch = int(ckpt.get('epoch', 0)) + 1
            best_path = Path(config.output_dir) / 'best_segpart.pth'
            if best_path.is_file():
                best_ckpt = torch.load(str(best_path), map_location='cpu',
                                       weights_only=False)
                best_miou = float(best_ckpt.get('miou', -1.0))
            print(f'【续训】已加载 {resume_path}（已完成 {start_epoch - 1} 轮，'
                  f'历史最佳 mIoU {best_miou:.4f}）；本次从第 {start_epoch} 轮继续，'
                  f'追加 {config.epochs} 轮')
        else:
            print(f'【警告】续训文件不存在：{resume_path}，按全新训练执行')
    report_lines = [
        '# 部位分割模型训练报告',
        '',
        f'- 训练时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'- 数据根目录：{config.data_root}',
        f'- 预训练权重：{config.pretrained_model_name}',
        f'- 训练样本：{len(train_set)}；验证样本：{len(val_set)}',
        f'- 输入尺寸：{config.input_size}；批大小：{config.batch_size}；'
        f'轮数：{config.epochs}',
        f'- 基础学习率：{config.base_lr}；权重衰减：{config.weight_decay}',
        '',
        '| 轮次 | 平均损失 | mIoU | 点命中率 |',
        '| --- | --- | --- | --- |',
    ]

    for epoch in range(start_epoch, start_epoch + config.epochs):
        model.train()
        epoch_loss, batch_count = 0.0, 0
        t0 = time.time()
        for i, batch in enumerate(train_loader):
            images = batch['image'].to(device)
            masks = batch['mask'].to(device)
            with torch.amp.autocast('cuda', enabled=config.use_amp and device.type == 'cuda'):
                outputs = model(pixel_values=images)
                loss, parts = compute_loss(outputs, masks, config,
                                           class_weights)
            # 梯度累积：损失按累积步数折算，攒够步数再更新参数
            scaler.scale(loss / accum).backward()
            if (i + 1) % accum == 0 or (i + 1) == len(train_loader):
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad(set_to_none=True)
                scheduler.step()
            epoch_loss += float(loss.detach().cpu()) / accum
            batch_count += 1
        avg_loss = epoch_loss / max(1, batch_count)

        miou, hit_rate = evaluate(model, val_loader, device, config.num_classes)
        if device.type == 'cuda':
            torch.cuda.empty_cache()  # 释放评估期碎片，降低显存峰值
        elapsed = time.time() - t0
        print(f'【轮次 {epoch}/{start_epoch + config.epochs - 1}】损失 {avg_loss:.4f}；'
              f'mIoU {miou:.4f}；点命中率 {hit_rate:.4f}；耗时 {elapsed:.1f}s')
        report_lines.append(
            f'| {epoch} | {avg_loss:.4f} | {miou:.4f} | {hit_rate:.4f} |')

        # 保存最优（按 mIoU）；最后一轮另存 last_segpart.pth
        is_best = miou > best_miou
        if is_best:
            best_miou = miou
        if is_best or not config.save_best_only:
            torch.save({
                'model_state': {k: v.detach().cpu()
                                for k, v in model.state_dict().items()},
                'class_names': ['background'] + list(PART_NAMES),
                'num_classes': config.num_classes,
                'miou': round(float(best_miou), 4),
                'input_size': config.input_size,
                'pretrained_model_name': config.pretrained_model_name,
                'msfpa_hidden_dim': config.msfpa_hidden_dim,
                'msfpa_dilations': list(config.msfpa_dilations),
                'ba_msr_alpha_init': config.ba_msr_alpha_init,
                'epoch': epoch,
            }, str(out_dir / 'best_segpart.pth'))
        torch.save({
            'model_state': {k: v.detach().cpu()
                            for k, v in model.state_dict().items()},
            'class_names': ['background'] + list(PART_NAMES),
            'num_classes': config.num_classes,
            'miou': round(float(miou), 4),
            'input_size': config.input_size,
            'epoch': epoch,
        }, str(out_dir / 'last_segpart.pth'))

    report_lines += [
        '',
        f'- 最佳 mIoU：{best_miou:.4f}',
    ]
    (out_dir / 'evaluation_report.md').write_text(
        '\n'.join(report_lines), encoding='utf-8')
    write_sidecar(out_dir, best_miou)

    print(f'训练结束。最佳 mIoU：{best_miou:.4f}；产物目录：{out_dir}')
    register_to_shared_library(out_dir, best_miou,
                               str(args.get('model_name') or 'v7部位分割模型'),
                               bool(args.get('register_shared', True)))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='部位分割模型训练（v7 增强 SegFormer，迁移学习）')
    parser.add_argument('--data_root', default=None,
                        help='部位检测数据集根目录（含 annotations.csv）')
    parser.add_argument('--epochs', type=int, default=None,
                        help='训练轮数（续训时为追加轮数）')
    parser.add_argument('--resume', default=None, nargs='?',
                        const='training_output/last_segpart.pth',
                        help='断点续训：热启动权重（不带路径时默认 training_output/last_segpart.pth）')
    parser.add_argument('--batch-size', dest='batch_size', type=int, default=None,
                        help='批大小（显存紧张时调小，配合 --grad-accum 保持等效批大小）')
    parser.add_argument('--grad-accum', dest='grad_accum', type=int, default=None,
                        help='梯度累积步数（默认 1；显存不足时调大，如批大小 2 + 累积 2 = 等效批 4）')
    parser.add_argument('--input-size', dest='input_size', type=int, default=None,
                        help='输入尺寸（默认 512）')
    parser.add_argument('--max_samples', type=int, default=None,
                        help='只用前 N 张做子集训练（冒烟验证用）')
    parser.add_argument('--model-name', dest='model_name', default='v7部位分割模型',
                        help='登记进控制台模型库时的模型基名')
    parser.add_argument('--no-register', dest='register_shared', action='store_false',
                        default=True, help='训练结束后不自动登记进控制台模型库')
    return parser


if __name__ == '__main__':
    sys.exit(run_training(vars(build_arg_parser().parse_args())))
