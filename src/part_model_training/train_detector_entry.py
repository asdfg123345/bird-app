# -*- coding: utf-8 -*-
"""
train_detector_entry.py —— 管线 B 部位检测训练编排入口（发布包版）

作者：天咏（明月照今日，风摇过两川。）

【流程】
1. 关键点标注 → YOLO 标签 + verified_list（render_labels.to_yolo，
   转换即视为已校验，可直接进入第三步训练）；
2. 组装参数调用 trainer_detector.run_detector_training（Faster R-CNN）；
3. 产物 best/last_detector.pth 自动登记进控制台共享模型库。

【用法】
    python train_detector_entry.py --data_root "d:\\鸟类\\部位检测数据集" ^
        --epochs 15 --model-name 部位检测器
"""

import argparse
import sys
from pathlib import Path

from render_labels import iter_records, to_yolo

# 本模块所在目录（产物也落在这里的 training_output\ 下）
_MODULE_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description='管线 B 部位检测训练（关键点数据自动转换）')
    parser.add_argument('--data_root', required=True, help='部位检测数据集根目录')
    parser.add_argument('--epochs', type=int, default=None, help='训练轮数')
    parser.add_argument('--batch-size', dest='batch_size', type=int, default=None)
    parser.add_argument('--lr', type=float, default=None)
    parser.add_argument('--model-name', dest='model_name',
                        default='部位检测器', help='登记进模型库的基名')
    parser.add_argument('--max_samples', type=int, default=None,
                        help='冒烟测试：仅转换前 N 张')
    parser.add_argument('--skip-convert', dest='skip_convert',
                        action='store_true', help='跳过转换（沿用已有标签）')
    parser.add_argument('--no-register', dest='register_shared',
                        action='store_false', default=True,
                        help='跳过共享模型库登记')
    args = parser.parse_args()

    labels_root = _MODULE_DIR / 'training_output' / 'pseudo_labels'
    if not args.skip_convert:
        to_yolo(args.data_root, str(labels_root), 'train', args.max_samples)
    else:
        print(f'【转换】按参数跳过，沿用 {labels_root} 下的已有标签')

    # 图片目录：取首条记录 image_path 的父目录（数据集内 split 目录自适应）
    records = iter_records(args.data_root, 'train')
    if not records:
        print('【错误】annotations.csv 无训练记录，请检查数据目录。')
        return 2
    images_dir = Path(args.data_root) / Path(records[0]['image_path']).parent

    # 延迟导入（先完成标签转换，再加载 torch 相关模块，启动更快）
    from trainer_detector import run_detector_training
    train_args = {
        'images_dir': str(images_dir),
        'labels_dir': str(labels_root / 'labels'),
        'verified_list': str(labels_root / 'verified_list.txt'),
        'out_dir': str(_MODULE_DIR / 'training_output' / 'detector'),
        'model_name': args.model_name,
        'num_workers': 0,  # Windows 兼容
    }
    if args.epochs:
        train_args['epochs'] = args.epochs
    if args.batch_size:
        train_args['batch_size'] = args.batch_size
    if args.lr:
        train_args['lr'] = args.lr
    train_args['register_shared'] = args.register_shared
    return run_detector_training(train_args)


if __name__ == '__main__':
    sys.exit(main())
