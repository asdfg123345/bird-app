# -*- coding: utf-8 -*-
# ============================================================
# 项目签名 | Project Signature
# 作者：陈建宇，号天咏
# 题记：明月照今日，风遥照两川
# 项目：SegFormer 部位分割模型（v7 架构迁移版）
# ============================================================
r"""
部位分割模型配置文件

【设计原则】
所有文件路径均通过 os.path.dirname(__file__) 自动推断，避免硬编码绝对路径。
数据根目录支持以下方式传入（优先级从高到低）：
1. 命令行参数 --data_root
2. 环境变量 SEGPART_DATA_ROOT
3. 代码中直接设置 config.data_root
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List


# 本模块所在目录，作为所有相对路径的基准
_SEGPART_DIR = os.path.dirname(os.path.abspath(__file__))


def _get_data_root() -> str:
    """从环境变量获取部位检测数据集根目录，避免硬编码。"""
    return os.environ.get("SEGPART_DATA_ROOT", "")


# 15 个语义部位（与视觉系统 config.DETECTOR_CLASSES 顺序完全一致）；
# 类别索引 = 部位序号 + 1（0 为背景）
PART_NAMES: List[str] = [
    'back', 'beak', 'belly', 'breast', 'crown',
    'forehead', 'left_eye', 'left_leg', 'left_wing', 'nape',
    'right_eye', 'right_leg', 'right_wing', 'tail', 'throat',
]

# 语义部位 → 左右对称部位索引表（分割输出为左右独立类别，检测接口需合并）
SYMMETRIC_PARTS: Dict[str, List[str]] = {
    'wing': ['left_wing', 'right_wing'],
    'eye': ['left_eye', 'right_eye'],
    'leg': ['left_leg', 'right_leg'],
}

# 各部位椭圆掩码半径占图像短边的比例（尺寸先验，依据鸟类解剖学先验设定）
PART_RADIUS_RATIO: Dict[str, float] = {
    'beak': 0.05, 'left_eye': 0.04, 'right_eye': 0.04,
    'crown': 0.06, 'forehead': 0.06, 'throat': 0.06, 'nape': 0.07,
    'left_leg': 0.07, 'right_leg': 0.07,
    'back': 0.13, 'belly': 0.12, 'breast': 0.10,
    'left_wing': 0.15, 'right_wing': 0.15, 'tail': 0.12,
}


@dataclass
class ConfigSegPart:
    """部位分割模型训练配置"""

    # ============ 数据集配置 ============
    data_root: str = field(default_factory=_get_data_root)
    num_classes: int = 16  # 背景 + 15 部位

    # ============ 模型配置（沿用 v7 架构）============
    pretrained_model_name: str = 'nvidia/segformer-b2-finetuned-ade-512-512'
    input_size: int = 512
    msfpa_hidden_dim: int = 256
    msfpa_dilations: List[int] = field(default_factory=lambda: [6, 12, 18])
    ba_msr_alpha_init: float = 0.1

    # ============ 训练配置 ============
    batch_size: int = 4
    gradient_accumulation: int = 1  # 梯度累积步数（显存不足时调大，等效批大小=batch×累积）
    epochs: int = 30
    base_lr: float = 6e-5
    weight_decay: float = 0.05
    warmup_iters: int = 200
    poly_power: float = 1.0
    # 差异化学习率倍率（编码器慢学，新增模块快学）
    encoder_lr_scale: float = 0.1
    decoder_lr_scale: float = 1.0
    classifier_lr_scale: float = 5.0
    msfpa_lr_scale: float = 1.0
    ba_msr_lr_scale: float = 1.0
    aux_loss_weight: float = 0.4

    # ============ 损失函数配置 ============
    ce_weight: float = 1.0
    # Dice 权重 1.0：与加权 CE 协同对抗背景主导的类别失衡
    # （首训 0.3 时模型收敛到全背景塌缩解，mIoU 停在 0.05）
    dice_weight: float = 1.0
    boundary_weight: float = 0.5
    boundary_sigma: float = 10.0

    # ============ AMP / 其他 ============
    use_amp: bool = True
    num_workers: int = 0  # Windows 兼容
    seed: int = 42

    # ============ 输出配置（基于本模块所在目录自动推断）============
    output_dir: str = field(
        default_factory=lambda: os.path.join(_SEGPART_DIR, 'training_output')
    )
    save_best_only: bool = True

    def validate(self) -> None:
        """验证配置参数合法性"""
        if not self.data_root:
            raise ValueError(
                "数据根目录未配置。请通过以下方式之一设置:\n"
                "  1. 设置环境变量: $env:SEGPART_DATA_ROOT = '数据路径'\n"
                "  2. 命令行参数: python train_segpart.py --data_root '数据路径'"
            )
        if not os.path.isdir(self.data_root):
            raise FileNotFoundError(f"数据根目录不存在: {self.data_root}")
        if self.num_classes != len(PART_NAMES) + 1:
            raise ValueError(
                f"num_classes({self.num_classes}) 应为部位数({len(PART_NAMES)}) + 1"
            )


# 全局单例配置
CONFIG_SEGPART = ConfigSegPart()
