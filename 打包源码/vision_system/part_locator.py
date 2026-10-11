# -*- coding: utf-8 -*-
"""
part_locator.py —— 部位定位：检测模型获取（含注册表回退）与部位框解算

作者：天咏（明月照今日，风摇过两川。）

定位策略（三级回退）：
1. 检测器直接检出部位框，来源记为实际使用的模型注册名；
2. 部位未检出但检出鸟体整框：按 config.SPACE_PRIORS 空间先验比例
   相对鸟体框估计部位位置，is_estimated 标记为 True，来源记为"空间先验估计"；
3. 鸟体也未检出：抛出 PartLocationError，提示用户更换更清晰的图片。

模型获取逻辑与 species_classifier.get_species_backend 完全对称。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

import config
from model_backends import (
    Box,
    ModelLoadError,
    ModelUnavailableError,
    PartDetectorBackend,
)


class PartLocationError(RuntimeError):
    """部位定位失败（消息为中文，供界面直接展示）。"""


@dataclass
class LocationResult:
    """部位定位结果。"""
    box: Box                        # 部位像素框（已裁剪到图像范围内）
    part_en: str                    # 语义部位英文名
    part_cn: str                    # 语义部位中文名
    is_estimated: bool              # 是否为空间先验估计框
    confidence: Optional[float]     # 检测置信度（估计框为 None）
    source: str                     # 定位来源（模型注册名 / 空间先验估计）


# 检测器实例缓存：键为模型注册名，值为加载成功的检测器实例（进程内单例复用）
_DETECTOR_CACHE: Dict[str, PartDetectorBackend] = {}


def get_part_detector(name: Optional[str] = None) -> PartDetectorBackend:
    """获取已加载的部位检测模型，逻辑与物种模型获取完全对称。

    参数：
        name  模型注册名，None 时使用 config.ACTIVE_PART_MODEL

    返回：
        加载成功的部位检测模型实例（已设置 registry_name 实例属性）

    异常：
        ModelUnavailableError  激活模型与全部回退模型均不可用
    """
    requested = name or config.ACTIVE_PART_MODEL

    # 请求名称已有可用缓存：直接复用，不再重复加载
    if requested in _DETECTOR_CACHE:
        return _DETECTOR_CACHE[requested]

    failure_reasons: List[str] = []
    # 候选顺序：先尝试请求模型本身，再按配置的回退顺序尝试其余模型
    candidates = [requested] + [
        item for item in config.PART_FALLBACK_ORDER if item != requested
    ]

    for position, candidate in enumerate(candidates):
        # 回退链上的模型若已有缓存实例，直接复用并提示回退结果
        if candidate in _DETECTOR_CACHE:
            if position > 0:
                print(f"激活模型 {requested} 不可用，回退到 {candidate}（缓存实例）")
            return _DETECTOR_CACHE[candidate]

        if position > 0:
            print(f"激活模型 {requested} 不可用，回退到 {candidate}")

        # 注册表登记检查：未登记的名称无法实例化
        if candidate not in config.PART_MODELS:
            failure_reasons.append(f"{candidate}：未在部位模型注册表中登记")
            continue
        entry = config.PART_MODELS[candidate]

        # 按注册表参数实例化适配器，并记录实际使用的注册名
        detector = entry["adapter"](**entry.get("params", {}))
        detector.registry_name = candidate

        # 权重文件缺失：记录中文原因并进入下一步回退
        if not detector.is_available():
            failure_reasons.append(f"{candidate}：模型文件缺失或不可用")
            continue

        # 加载权重；失败按中文原因记录后继续回退
        try:
            detector.load()
        except ModelLoadError as exc:
            failure_reasons.append(f"{candidate}：{exc}")
            continue

        # 加载成功：写入缓存，后续同名请求直接复用
        _DETECTOR_CACHE[candidate] = detector
        return detector

    raise ModelUnavailableError(
        "所有候选部位检测模型均不可用：" + "；".join(failure_reasons)
    )


def locate_part(
    pil_image,
    part_en: str,
    detector: Optional[PartDetectorBackend] = None,
) -> LocationResult:
    """定位图片中指定语义部位，返回像素框；失败按三级策略逐级回退。"""
    if detector is None:
        detector = get_part_detector()
    part_cn = config.PART_CN_NAMES[part_en]
    # 来源标识：优先使用注册名实例属性，缺失时回退为类名
    source = getattr(detector, "registry_name", None) or detector.__class__.__name__

    # 第一级：检测器直接检出部位框
    box = detector.detect(pil_image, part_en)
    if box is not None:
        return LocationResult(
            box=box,
            part_en=part_en,
            part_cn=part_cn,
            is_estimated=False,
            confidence=None,
            source=source,
        )

    # 第二级：部位未检出，检测鸟体整框后按空间先验比例估计部位框
    bird_box = detector.detect_bird(pil_image)
    if bird_box is not None:
        cx, cy, w, h = config.SPACE_PRIORS[part_en]
        bird_w = bird_box.width
        bird_h = bird_box.height
        est_box = Box(
            x1=bird_box.x1 + (cx - w / 2.0) * bird_w,
            y1=bird_box.y1 + (cy - h / 2.0) * bird_h,
            x2=bird_box.x1 + (cx + w / 2.0) * bird_w,
            y2=bird_box.y1 + (cy + h / 2.0) * bird_h,
        )
        # 裁剪到图像范围内，防止越界绘制
        est_box = est_box.clamp_to(pil_image.size[0], pil_image.size[1])
        return LocationResult(
            box=est_box,
            part_en=part_en,
            part_cn=part_cn,
            is_estimated=True,
            confidence=None,
            source="空间先验估计",
        )

    # 第三级：鸟体也未检出，无法估计，抛出中文提示
    raise PartLocationError(
        "未能在图片中定位鸟类目标，请换一张鸟体更清晰的图片"
    )
