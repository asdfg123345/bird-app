# -*- coding: utf-8 -*-
# ============================================================
# 项目签名 | Project Signature
# 作者：陈建宇，号天咏
# 题记：明月照今日，风遥照两川
# 项目：SegFormer 部位分割模型（v7 架构迁移版）
# ============================================================
"""
部位分割增强版 SegFormer 模型（复用 v7 高精度架构）

【架构】
1. SegFormer-B2 主干（encoder + decode_head）
2. MSFPA 模块：多尺度特征金字塔适配器（插入在 encoder 与 decode_head 之间）
3. BA-MSR 模块：边界感知细化（附加在 decode_head 输出后）

【前向流程】
input → encoder → 4 个尺度特征
       → MSFPA → 增强特征（作为 decode_head 的辅助输入）
       → decode_head → logits
       → BA-MSR → refined_logits

【辅助损失】
MSFPA 输出通过辅助分类头产生辅助 logits，用于辅助损失监督。
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import List, Optional
from transformers import SegformerForSemanticSegmentation


# ==================== MSFPA 模块 ====================
class SEBlock(nn.Module):
    """Squeeze-and-Excitation 通道注意力"""

    def __init__(self, channels: int, reduction: int = 16):
        super().__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(channels, channels // reduction),
            nn.ReLU(inplace=True),
            nn.Linear(channels // reduction, channels),
            nn.Sigmoid()
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        b, c, _, _ = x.shape
        w = self.avg_pool(x).view(b, c)
        w = self.fc(w).view(b, c, 1, 1)
        return x * w


class DepthwiseSeparableConv(nn.Module):
    """深度可分离卷积（减少参数量）"""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: int = 3,
        padding: int = 0,
        dilation: int = 1
    ):
        super().__init__()
        self.depthwise = nn.Conv2d(
            in_channels, in_channels, kernel_size,
            padding=padding, dilation=dilation,
            groups=in_channels, bias=False
        )
        self.pointwise = nn.Conv2d(in_channels, out_channels, 1, bias=False)
        self.bn = nn.BatchNorm2d(out_channels)
        self.act = nn.ReLU(inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.depthwise(x)
        x = self.pointwise(x)
        x = self.bn(x)
        return self.act(x)


class ASPPModule(nn.Module):
    """Atrous Spatial Pyramid Pooling（多尺度上下文）"""

    def __init__(
        self,
        in_channels: int,
        hidden_dim: int,
        dilations: List[int] = [6, 12, 18]
    ):
        super().__init__()
        self.global_branch = nn.Sequential(
            nn.Conv2d(in_channels, hidden_dim, 1, bias=False),
            nn.BatchNorm2d(hidden_dim),
            nn.ReLU(inplace=True)
        )
        self.dilated_branches = nn.ModuleList([
            DepthwiseSeparableConv(
                in_channels, hidden_dim,
                kernel_size=3, padding=d, dilation=d
            )
            for d in dilations
        ])
        self.image_pool = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(in_channels, hidden_dim, 1, bias=False),
            nn.BatchNorm2d(hidden_dim),
            nn.ReLU(inplace=True)
        )
        num_branches = 1 + len(dilations) + 1
        self.fusion = nn.Sequential(
            nn.Conv2d(hidden_dim * num_branches, hidden_dim, 1, bias=False),
            nn.BatchNorm2d(hidden_dim),
            nn.ReLU(inplace=True),
            nn.Dropout(0.1)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h, w = x.shape[2:]
        outputs = [self.global_branch(x)]
        for branch in self.dilated_branches:
            outputs.append(branch(x))
        pool = self.image_pool(x)
        pool = F.interpolate(pool, size=(h, w), mode='bilinear', align_corners=False)
        outputs.append(pool)
        out = torch.cat(outputs, dim=1)
        return self.fusion(out)


class MSFPAModule(nn.Module):
    """MSFPA 多尺度特征金字塔适配器"""

    def __init__(
        self,
        in_channels_list: List[int],
        hidden_dim: int = 256,
        dilations: List[int] = [6, 12, 18]
    ):
        super().__init__()
        assert len(in_channels_list) == 4, "需要 4 个尺度的特征"
        self.hidden_dim = hidden_dim

        self.lateral_convs = nn.ModuleList([
            nn.Conv2d(c, hidden_dim, 1, bias=False)
            for c in in_channels_list
        ])
        self.se = SEBlock(hidden_dim * 4, reduction=16)
        self.reduce_conv = nn.Sequential(
            nn.Conv2d(hidden_dim * 4, hidden_dim, 1, bias=False),
            nn.BatchNorm2d(hidden_dim),
            nn.ReLU(inplace=True)
        )
        self.aspp = ASPPModule(hidden_dim, hidden_dim, dilations)

    def forward(self, features: List[torch.Tensor]) -> torch.Tensor:
        """
        Args:
            features: 4 个尺度的特征 [H/4, H/8, H/16, H/32]

        Returns:
            fused: (B, hidden_dim, H/8, W/8)
        """
        target_h, target_w = features[1].shape[2:]
        unified = []
        for feat, conv in zip(features, self.lateral_convs):
            x = conv(feat)
            if x.shape[2:] != (target_h, target_w):
                x = F.interpolate(
                    x, size=(target_h, target_w),
                    mode='bilinear', align_corners=False
                )
            unified.append(x)

        concat = torch.cat(unified, dim=1)
        se_out = self.se(concat)
        reduced = self.reduce_conv(se_out)
        out = self.aspp(reduced)
        return out


# ==================== 多方向边缘提取模块 ====================
class MultiDirEdgeExtractor(nn.Module):
    """多方向 Sobel 边缘提取器

    同时计算 0°、90°、45°、135° 四个方向的 Sobel 梯度，
    将梯度平方按方向权重加权后开方，再归一化到 [0, 1]。
    输入既可以是 logits（通道数 > 1），也可以是标签索引图（通道数 = 1）。
    """

    def __init__(
        self,
        num_classes: Optional[int] = None,
        dir_weights: Optional[List[float]] = None
    ):
        """
        Args:
            num_classes: 类别数（仅用于接口兼容，当前实现通过通道数判断输入类型）
            dir_weights: 四个方向的权重，顺序为 [0°, 90°, 45°, 135°]
        """
        super().__init__()
        if dir_weights is None:
            dir_weights = [0.25, 0.25, 0.25, 0.25]
        assert len(dir_weights) == 4, "方向权重必须为 4 个"
        self.num_classes = num_classes

        # 注册方向权重缓冲，形状 (4, 1, 1, 1, 1) 便于与堆叠后的 5 维梯度图相乘
        self.register_buffer(
            'dir_weights',
            torch.tensor(dir_weights, dtype=torch.float32).view(4, 1, 1, 1, 1)
        )

        # 四个方向的 Sobel 核
        kernels = {
            'sobel_h': [[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]],
            'sobel_v': [[-1, -2, -1], [0, 0, 0], [1, 2, 1]],
            'sobel_diag_45': [[0, 1, 2], [-1, 0, 1], [-2, -1, 0]],
            'sobel_diag_135': [[2, 1, 0], [1, 0, -1], [0, -1, -2]],
        }
        for name, kernel in kernels.items():
            tensor = torch.tensor(kernel, dtype=torch.float32).view(1, 1, 3, 3)
            self.register_buffer(name, tensor)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: (B, C, H, W)，logits 或 one-hot/标签索引图

        Returns:
            boundary: (B, 1, H, W) 归一化边界响应图
        """
        if x.dim() != 4:
            raise ValueError(f"输入必须为 4 维张量，当前维度为 {x.dim()}")

        # 通道数 > 1 时视为 logits 或 one-hot，取 argmax；否则视为标签索引图
        if x.shape[1] > 1:
            pred = x.argmax(dim=1, keepdim=True).float()
        else:
            pred = x.float()

        # 反射填充，保持尺寸不变
        pred_padded = F.pad(pred, (1, 1, 1, 1), mode='reflect')

        # 分别用四个 Sobel 核卷积
        edges = []
        for name in ['sobel_h', 'sobel_v', 'sobel_diag_45', 'sobel_diag_135']:
            kernel = getattr(self, name)
            edge = F.conv2d(pred_padded, kernel)
            edges.append(edge)
        edges = torch.stack(edges, dim=0)  # (4, B, 1, H, W)

        # 加权梯度平方和开方
        weighted = (edges ** 2) * self.dir_weights
        boundary = torch.sqrt(weighted.sum(dim=0) + 1e-6)  # (B, 1, H, W)

        # 按样本最大值归一化
        bmax = boundary.amax(dim=[1, 2, 3], keepdim=True).clamp(min=1e-6)
        boundary = boundary / bmax
        return boundary


# ==================== 边缘密度加权分类头 ====================
class EDWCClassifier(nn.Module):
    """边缘密度加权分类头（Edge-Density Weighted Classifier）

    利用边界图生成边缘分数，并根据局部边缘密度动态融合原 logits 与边缘分数，
    在边界复杂区域增强边缘感知能力。
    """

    def __init__(
        self,
        num_classes: int,
        alpha: float = 0.5,
        tau: float = 0.3,
        radius: int = 3
    ):
        """
        Args:
            num_classes: 类别数
            alpha: 融合系数上限
            tau: 边缘密度阈值
            radius: 局部窗口半径（窗口大小 = 2*radius + 1）
        """
        super().__init__()
        self.num_classes = num_classes
        self.edwc_alpha = alpha
        self.edwc_tau = tau
        self.edwc_radius = radius

        # 将边界图映射为每个类别的边缘分数
        self.edge_conv = nn.Conv2d(1, num_classes, kernel_size=1, bias=True)
        nn.init.xavier_uniform_(self.edge_conv.weight)
        nn.init.zeros_(self.edge_conv.bias)

    def forward(
        self,
        logits: torch.Tensor,
        boundary_map: torch.Tensor
    ) -> torch.Tensor:
        """
        Args:
            logits: (B, num_classes, H, W)
            boundary_map: (B, 1, H, W)

        Returns:
            fused_logits: (B, num_classes, H, W)
        """
        # 边缘分数
        edge_score = self.edge_conv(boundary_map)  # (B, C, H, W)

        # 局部边缘密度：以每个像素为中心的窗口内边界均值
        kernel_size = 2 * self.edwc_radius + 1
        rho_e = F.avg_pool2d(
            boundary_map,
            kernel_size=kernel_size,
            stride=1,
            padding=self.edwc_radius
        )  # (B, 1, H, W)

        # 动态融合系数
        lambda_e = self.edwc_alpha * torch.sigmoid(
            (rho_e - self.edwc_tau) * 10.0
        )  # (B, 1, H, W)

        # 为 AMP 混合精度安全，统一用 float32 计算概率融合
        logits_f = logits.float()
        edge_score_f = edge_score.float()
        lambda_f = lambda_e.float()

        prob_logits = F.softmax(logits_f, dim=1)
        prob_edge = F.softmax(edge_score_f, dim=1)

        fused_prob = prob_logits * (1.0 - lambda_f) + prob_edge * lambda_f
        fused_prob = torch.clamp(fused_prob, min=1e-7, max=1.0)
        fused_logits = torch.log(fused_prob)

        return fused_logits.to(logits.dtype)


# ==================== BA-MSR 模块 ====================
class BAMSRModule(nn.Module):
    """BA-MSR 边界感知多尺度细化模块"""

    def __init__(self, num_classes: int = 7, alpha_init: float = 0.1):
        super().__init__()
        self.num_classes = num_classes
        self.boundary_conv = nn.Conv2d(1, 1, kernel_size=1, bias=True)
        self.alpha = nn.Parameter(torch.tensor(alpha_init))

        # 使用多方向边缘提取器替代原来的 sobel_x/sobel_y
        self.edge_extractor = MultiDirEdgeExtractor(num_classes=num_classes)

        nn.init.xavier_uniform_(self.boundary_conv.weight)
        nn.init.zeros_(self.boundary_conv.bias)

    def compute_boundary_map(self, logits: torch.Tensor) -> torch.Tensor:
        """调用多方向边缘提取器计算边界图"""
        return self.edge_extractor(logits)

    def forward(self, logits: torch.Tensor) -> torch.Tensor:
        """前向细化"""
        with torch.no_grad():
            boundary = self.compute_boundary_map(logits)
        attention = torch.sigmoid(self.boundary_conv(boundary))
        refined = logits * (1.0 + self.alpha * attention)
        return refined

    def get_alpha(self) -> float:
        """获取当前 α 值"""
        return self.alpha.item()


# ==================== 增强版 SegFormer ====================
class SegFormerEnhancedV7(nn.Module):
    """v7 增强版 SegFormer 模型"""

    def __init__(
        self,
        segformer: SegformerForSemanticSegmentation,
        msfpa: MSFPAModule,
        ba_msr: BAMSRModule,
        edwc: Optional[EDWCClassifier] = None,
        use_msfpa: bool = True,
        use_ba_msr: bool = True,
        use_edwc: bool = False,
        aux_loss_weight: float = 0.4
    ):
        super().__init__()
        self.segformer = segformer
        self.msfpa = msfpa
        self.ba_msr = ba_msr
        self.edwc = edwc
        self.use_msfpa = use_msfpa
        self.use_ba_msr = use_ba_msr
        self.use_edwc = use_edwc
        self.aux_loss_weight = aux_loss_weight

        # EDWC 启用时，如果没有传入分类头则自动创建，同时创建边缘提取器
        if self.use_edwc:
            if self.edwc is None:
                self.edwc = EDWCClassifier(
                    num_classes=segformer.config.num_labels
                )
            self.edge_extractor = MultiDirEdgeExtractor(
                num_classes=segformer.config.num_labels
            )
        else:
            self.edge_extractor = None

        self.aux_head = nn.Sequential(
            nn.Conv2d(msfpa.hidden_dim, msfpa.hidden_dim, 3, padding=1, bias=False),
            nn.BatchNorm2d(msfpa.hidden_dim),
            nn.ReLU(inplace=True),
            nn.Dropout(0.1),
            nn.Conv2d(msfpa.hidden_dim, segformer.config.num_labels, 1)
        )

        for m in self.aux_head.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def get_encoder_features(self, pixel_values: torch.Tensor) -> List[torch.Tensor]:
        """获取 encoder 的 4 个尺度特征"""
        outputs = self.segformer.segformer.encoder(
            pixel_values,
            output_hidden_states=True,
            return_dict=True
        )
        hidden_states = outputs.hidden_states
        return list(hidden_states[-4:])

    def forward(
        self,
        pixel_values: torch.Tensor,
        labels: Optional[torch.Tensor] = None
    ) -> dict:
        """前向计算"""
        encoder_features = self.get_encoder_features(pixel_values)

        msfpa_out = None
        aux_logits = None
        if self.use_msfpa:
            msfpa_out = self.msfpa(encoder_features)
            aux_logits = self.aux_head(msfpa_out)
            target_h, target_w = encoder_features[0].shape[2:]
            aux_logits = F.interpolate(
                aux_logits, size=(target_h, target_w),
                mode='bilinear', align_corners=False
            )

        segformer_outputs = self.segformer(
            pixel_values=pixel_values,
            output_hidden_states=True,
            return_dict=True
        )
        logits = segformer_outputs.logits

        if self.use_ba_msr:
            logits = self.ba_msr(logits)

        # EDWC 边缘密度加权分类头：在边界复杂区域融合边缘分数
        if self.use_edwc:
            with torch.no_grad():
                boundary_map = self.edge_extractor(logits)
            logits = self.edwc(logits, boundary_map)

        return {
            'logits': logits,
            'aux_logits': aux_logits,
            'msfpa_out': msfpa_out,
            'encoder_features': encoder_features
        }

    def get_auxiliary_features(self, pixel_values: torch.Tensor):
        """获取辅助特征（用于辅助损失）"""
        encoder_features = self.get_encoder_features(pixel_values)
        msfpa_out = self.msfpa(encoder_features) if self.use_msfpa else None
        return encoder_features, msfpa_out


# ==================== 模型构建函数 ====================
def build_enhanced_segformer(config) -> SegFormerEnhancedV7:
    """构建 v7 增强版 SegFormer 模型

    Args:
        config: ConfigV7 配置对象

    Returns:
        SegFormerEnhancedV7 实例
    """
    print(f"[v7] 加载预训练模型: {config.pretrained_model_name}")
    segformer = SegformerForSemanticSegmentation.from_pretrained(
        config.pretrained_model_name,
        num_labels=config.num_classes,
        ignore_mismatched_sizes=True
    )

    in_channels_list = [
        segformer.config.hidden_sizes[0],
        segformer.config.hidden_sizes[1],
        segformer.config.hidden_sizes[2],
        segformer.config.hidden_sizes[3],
    ]
    print(f"[v7] Encoder 通道数: {in_channels_list}")

    msfpa = MSFPAModule(
        in_channels_list=in_channels_list,
        hidden_dim=config.msfpa_hidden_dim,
        dilations=config.msfpa_dilations
    )

    ba_msr = BAMSRModule(
        num_classes=config.num_classes,
        alpha_init=config.ba_msr_alpha_init
    )

    # 根据配置决定是否启用 EDWC
    use_edwc = getattr(config, 'use_edwc', False)
    edwc = None
    if use_edwc:
        edwc = EDWCClassifier(
            num_classes=config.num_classes,
            alpha=getattr(config, 'edwc_alpha', 0.5),
            tau=getattr(config, 'edwc_tau', 0.3),
            radius=getattr(config, 'edwc_radius', 3)
        )
        print(f"[v7] 启用 EDWC 分类头: alpha={edwc.edwc_alpha}, "
              f"tau={edwc.edwc_tau}, radius={edwc.edwc_radius}")

    model = SegFormerEnhancedV7(
        segformer=segformer,
        msfpa=msfpa,
        ba_msr=ba_msr,
        edwc=edwc,
        use_msfpa=True,
        use_ba_msr=True,
        use_edwc=use_edwc
    )

    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"[v7] 增强模型参数: {total/1e6:.2f}M (可训练 {trainable/1e6:.2f}M)")

    return model
