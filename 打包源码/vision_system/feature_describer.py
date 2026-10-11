# -*- coding: utf-8 -*-
"""
feature_describer.py —— 部位视觉特征程序化统计（颜色 / 形状 / 斑纹）

作者：天咏（明月照今日，风摇过两川。）

说明：
1. 全部结论来自裁剪区域的像素统计（色相分桶、边缘密度、局部对比度等），
   程序只陈述统计事实，不做任何主观臆测；
2. 输入为 PIL 图像与像素框，输出结构化中文描述，供 vqa_engine 组装回答。
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from model_backends import Box

# ============ 颜色统计阈值 ============
SAT_BG_MAX = 40           # 饱和度上限：低于此值视为无彩色像素（偏背景，不参与色相分桶）
WHITE_V_MIN = 180         # 白色判定：无彩色且明度高于此值记为白色
BLACK_V_MAX = 70          # 黑色判定：无彩色且明度低于此值记为黑色
HUE_BUCKET_COUNT = 12     # 色相桶数量：OpenCV 色相范围 0~179 均分为 12 桶
HUE_BIN_WIDTH = 15        # 每桶色相宽度：12 × 15 = 180，恰好覆盖 0~179
BROWN_S_MAX = 90          # 褐色特判：橙/黄桶平均饱和度低于此值时命名为褐色（棕色系）
DARK_V_MAX = 100          # 深色判定：色相桶内平均明度低于此值加前缀“深”
LIGHT_V_MIN = 200         # 浅色判定：色相桶内平均明度高于此值加前缀“浅”
MIN_SECOND_RATIO = 0.08   # 次色调门槛：占比须达到 8% 才列出，避免噪声色干扰
MIN_VALID_PIXELS = 200    # 有效像素下限：彩色像素（饱和度达标）少于 200 时颜色统计可信度低

# ============ 形状统计阈值 ============
SLENDER_RATIO_MIN = 2.5   # 狭长形判定：宽高比 w/h 大于等于此值
TALL_RATIO_MAX = 0.6      # 高窄形判定：宽高比 w/h 小于等于此值
SQUARE_RATIO_HI = 1.6     # 方形上界：0.6 < 宽高比 < 此值判为接近方形，否则为横宽形

# ============ 斑纹统计阈值 ============
EDGE_DENSITY_PURE = 0.02     # 边缘密度下限：低于此值判为整体以纯色为主
EDGE_DENSITY_COMPLEX = 0.15  # 边缘密度上限：高于此值判为斑纹较复杂（密集纹理或斑块）
# 说明：真实羽色条纹多为硬边缘，密度常达 0.05~0.15，故中档区间走方向性判据；
#       仅当密度超过 0.15（细密杂斑级）才直接判复杂，避免条纹误判。
EDGE_CANNY_LOW_K = 0.66      # Canny 自适应下阈值系数：下阈值 = 灰度中位数 × 此系数
EDGE_CANNY_HIGH_K = 1.33     # Canny 自适应上阈值系数：上阈值 = 灰度中位数 × 此系数（不超过 255）
GRAD_MAG_MIN = 10.0          # 梯度幅值下限：低于此值视为平坦像素，不计入方向统计
GRAD_DOMINANT_MIN = 0.6      # 方向主导门槛：最大方向 bin 占比达到 60% 判为明显条纹
ORIENT_AXIS_MAX = 22.5       # 横向夹角界：梯度方向与水平线夹角小于此值（度）归横向
ORIENT_VERT_MIN = 67.5       # 纵向夹角下界：夹角（度）大于此值
ORIENT_VERT_MAX = 112.5      # 纵向夹角上界：夹角（度）小于此值归纵向，其余归斜向
CONTRAST_STRONG = 40         # 局部对比度上限：块标准差中位数达到此值判为明暗对比较强
CONTRAST_WEAK = 25           # 局部对比度下限：低于此值判为明暗对比偏弱，介于两者之间为适中
GRID_ROWS = 8                # 局部对比度网格行数
GRID_COLS = 8                # 局部对比度网格列数（共 64 块，取各块标准差的中位数）
SMALL_SIDE_LIMIT = 32        # 区域边长下限：裁剪区宽或高小于此值时提示统计仅供参考

# 色相桶中文名（按桶索引 0~11 顺序：红 → 桃红）
HUE_CN_NAMES = [
    "红色", "橙色", "黄色", "黄绿色", "绿色", "青色",
    "蓝色", "蓝紫色", "紫色", "品红色", "玫红色", "桃红色",
]


@dataclass
class PartDescription:
    """部位视觉特征描述结构体。"""
    color_text: str     # 颜色统计文案
    shape_text: str     # 形状统计文案
    pattern_text: str   # 斑纹统计文案
    full_text: str      # 三段拼装的完整文案


def _clamp_box_int(box: Box, width: int, height: int) -> Tuple[int, int, int, int]:
    """把像素框收敛到图像范围内并取整，保证至少 1×1 像素，返回整数左上/右下角。"""
    clamped = box.clamp_to(width, height)
    x1, y1, x2, y2 = clamped.to_int_tuple()
    # 防止框完全越界导致裁剪宽高为 0
    x1 = min(max(0, x1), width - 1)
    y1 = min(max(0, y1), height - 1)
    x2 = max(min(width, x2), x1 + 1)
    y2 = max(min(height, y2), y1 + 1)
    return x1, y1, x2, y2


def _color_text(hsv: np.ndarray) -> str:
    """统计裁剪区颜色组成并生成文案（剔除背景法 + 色相 12 桶 + 深浅修饰）。"""
    hue = hsv[:, :, 0].astype(np.int16)
    sat = hsv[:, :, 1].astype(np.int16)
    val = hsv[:, :, 2].astype(np.int16)
    total = int(hue.size)
    if total == 0:
        return "该区域颜色信息不足。"

    low_sat = sat < SAT_BG_MAX
    white_mask = low_sat & (val > WHITE_V_MIN)
    black_mask = low_sat & (val < BLACK_V_MAX)
    gray_mask = low_sat & ~(white_mask | black_mask)
    colored_mask = ~low_sat

    # 无彩色像素按白/黑/灰三组直接计数（本身已含明暗信息，不再加深浅前缀）
    groups: Dict[str, int] = {}
    for name, mask in (("白色", white_mask), ("黑色", black_mask), ("灰色", gray_mask)):
        count = int(np.count_nonzero(mask))
        if count > 0:
            groups[name] = count

    # 彩色像素按色相分 12 桶，统计每桶像素数及平均明度/饱和度
    colored_hue = hue[colored_mask]
    valid = int(colored_hue.size)
    if valid > 0:
        bucket_idx = np.minimum(colored_hue // HUE_BIN_WIDTH, HUE_BUCKET_COUNT - 1)
        counts = np.bincount(bucket_idx, minlength=HUE_BUCKET_COUNT)
        val_sum = np.bincount(bucket_idx, weights=val[colored_mask], minlength=HUE_BUCKET_COUNT)
        sat_sum = np.bincount(bucket_idx, weights=sat[colored_mask], minlength=HUE_BUCKET_COUNT)
        for bucket in range(HUE_BUCKET_COUNT):
            count = int(counts[bucket])
            if count == 0:
                continue
            mean_v = float(val_sum[bucket]) / count
            mean_s = float(sat_sum[bucket]) / count
            name = HUE_CN_NAMES[bucket]
            # 褐色特判：橙/黄色桶（索引 1、2）平均饱和度偏低时判为褐色（棕色系）
            if bucket in (1, 2) and mean_s < BROWN_S_MAX:
                name = "褐色"
            # 深浅修饰：按桶内平均明度加前缀
            if mean_v < DARK_V_MAX:
                name = "深" + name
            elif mean_v > LIGHT_V_MIN:
                name = "浅" + name
            groups[name] = count

    ranked = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)
    if ranked:
        main_name, main_count = ranked[0]
        pieces = [f"该区域主色调为{main_name}（约占{main_count / total * 100:.0f}%）"]
        # 次色占比须达标才列出
        if len(ranked) > 1 and ranked[1][1] / total >= MIN_SECOND_RATIO:
            pieces.append(f"次色调为{ranked[1][0]}（约占{ranked[1][1] / total * 100:.0f}%）")
        text = "，".join(pieces) + "。"
    else:
        text = "该区域颜色信息不足。"

    # 有效（彩色）像素过少时标注可信度低
    if valid < MIN_VALID_PIXELS:
        text += "有效像素过少，颜色统计可信度低。"
    return text


def _shape_text(crop_w: int, crop_h: int, area_ratio: float, scope: str) -> str:
    """按宽高比分级形状，并给出细长度与面积占比文案。"""
    ratio = crop_w / crop_h
    if ratio >= SLENDER_RATIO_MIN:
        phrase = "呈狭长形"
    elif ratio <= TALL_RATIO_MAX:
        phrase = "呈高窄形"
    elif ratio < SQUARE_RATIO_HI:
        phrase = "接近方形"
    else:
        phrase = "呈横宽形"
    # 细长度 = 长边 / 短边，保留 1 位小数
    slender = max(crop_w, crop_h) / min(crop_w, crop_h)
    return (
        f"区域{phrase}（宽高比约 {ratio:.1f}，细长度 {slender:.1f}），"
        f"面积约占{scope} {area_ratio * 100:.0f}%。"
    )


def _edge_density(gray: np.ndarray) -> float:
    """边缘密度：Canny（自适应阈值）检出的边缘像素数占总像素比例。"""
    height, width = gray.shape
    total = int(gray.size)
    if total == 0 or height < 3 or width < 3:
        return 0.0
    median = float(np.median(gray))
    lower = int(min(255, max(0, round(EDGE_CANNY_LOW_K * median))))
    upper = int(min(255, max(0, round(EDGE_CANNY_HIGH_K * median))))
    edges = cv2.Canny(gray, lower, upper)
    return float(np.count_nonzero(edges)) / total


def _gradient_pattern(gray: np.ndarray) -> str:
    """中等边缘密度时，用 Sobel 梯度方向直方图判断是否存在主导方向的条纹。"""
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return "可见柔和的渐变过渡"
    grad_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    magnitude = np.sqrt(grad_x * grad_x + grad_y * grad_y)
    strong = magnitude >= GRAD_MAG_MIN
    if int(np.count_nonzero(strong)) == 0:
        return "可见柔和的渐变过渡"
    # 梯度方向角归一化到 0~180 度后分横向/纵向/斜向三类
    angle = np.degrees(np.arctan2(grad_y[strong], grad_x[strong]))
    angle = np.abs(angle) % 180.0
    axis = (angle < ORIENT_AXIS_MAX) | (angle > 180.0 - ORIENT_AXIS_MAX)
    vert = (angle > ORIENT_VERT_MIN) & (angle < ORIENT_VERT_MAX)
    diag = ~(axis | vert)
    bins = np.array([np.count_nonzero(axis), np.count_nonzero(vert), np.count_nonzero(diag)],
                    dtype=np.float64)
    shares = bins / bins.sum()
    top = int(np.argmax(shares))
    if shares[top] >= GRAD_DOMINANT_MIN:
        orient = ("横向", "纵向", "斜向")[top]
        return f"可见明显{orient}条纹状斑纹"
    return "可见柔和的渐变过渡"


def _local_contrast(gray: np.ndarray) -> float:
    """局部对比度：灰度图分 8x8 块，取各块标准差的中位数（范围 0~255）。"""
    height, width = gray.shape
    if height == 0 or width == 0:
        return 0.0
    if height < GRID_ROWS * 2 or width < GRID_COLS * 2:
        # 图太小无法稳定分块时退化为整体标准差
        return float(np.std(gray))
    block_h = height // GRID_ROWS
    block_w = width // GRID_COLS
    stds = []
    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            block = gray[row * block_h:(row + 1) * block_h,
                         col * block_w:(col + 1) * block_w]
            if block.size:
                stds.append(float(np.std(block)))
    return float(np.median(stds)) if stds else 0.0


def _pattern_text(gray: np.ndarray) -> str:
    """按边缘密度分级斑纹复杂程度，并结合局部对比度生成文案。"""
    density = _edge_density(gray)
    if density < EDGE_DENSITY_PURE:
        statement = "整体以纯色为主"
    elif density <= EDGE_DENSITY_COMPLEX:
        statement = _gradient_pattern(gray)
    else:
        statement = "斑纹较复杂，可见密集纹理或斑块"

    contrast = _local_contrast(gray)
    if contrast >= CONTRAST_STRONG:
        contrast_text = "明暗对比较强"
    elif contrast >= CONTRAST_WEAK:
        contrast_text = "明暗对比适中"
    else:
        contrast_text = "明暗对比偏弱"
    return f"{statement}，{contrast_text}。"


def describe_part(pil_image, box: Box, bird_box: Optional[Box] = None,
                  part_cn: str = "部位") -> PartDescription:
    """对图像中指定部位区域做程序化视觉特征统计。

    参数：
        pil_image  PIL 图像（任意模式，内部转 RGB 处理）
        box        部位像素框（先收敛到图像范围内再裁剪）
        bird_box   鸟体整框（可选；给出时面积占比以鸟体为基准，否则以整图为基准）
        part_cn    部位中文名（由 vqa_engine 传入，保留以兼容上游语义调用）

    返回：
        PartDescription，含颜色/形状/斑纹三段文案及拼装后的 full_text。
    """
    rgb_image = pil_image.convert("RGB")
    img_w, img_h = rgb_image.size
    x1, y1, x2, y2 = _clamp_box_int(box, img_w, img_h)

    # 裁剪部位区域并转 numpy（RGB），HSV 供颜色统计，灰度供斑纹统计
    crop = rgb_image.crop((x1, y1, x2, y2))
    rgb = np.asarray(crop)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    color_text = _color_text(hsv)

    crop_w, crop_h = x2 - x1, y2 - y1
    if bird_box is not None and bird_box.area > 0:
        denom = bird_box.area
        scope = "鸟体"
    else:
        denom = float(img_w * img_h)
        scope = "整图"
    clamped_box = Box(float(x1), float(y1), float(x2), float(y2))
    area_ratio = clamped_box.area / denom
    shape_text = _shape_text(crop_w, crop_h, area_ratio, scope)

    pattern_text = _pattern_text(gray)

    full_text = (
        f"【颜色】{color_text}\n"
        f"【形状】{shape_text}\n"
        f"【斑纹】{pattern_text}\n"
        f"以上特征基于图像区域统计分析。"
    )
    if crop_w < SMALL_SIDE_LIMIT or crop_h < SMALL_SIDE_LIMIT:
        full_text += "（区域较小，特征统计仅供参考）"

    return PartDescription(
        color_text=color_text,
        shape_text=shape_text,
        pattern_text=pattern_text,
        full_text=full_text,
    )
