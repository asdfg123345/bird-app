# -*- coding: utf-8 -*-
"""
model_backends.py —— 模型抽象层：抽象基类与统一数据结构

作者：天咏（明月照今日，风摇过两川。）

设计原则：核心链路（解析/识别/定位/描述/标记/引擎/GUI/CLI）只认识
本文件的抽象接口，不认识任何具体模型。新增模型 = 继承基类 + 注册表登记。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple


class ModelLoadError(RuntimeError):
    """模型加载失败（消息必须为中文，供界面直接展示）。"""


class ModelUnavailableError(RuntimeError):
    """模型不可用（权重缺失等），触发注册表回退。"""


@dataclass
class SpeciesPrediction:
    """单条物种识别结果。"""
    label_en: str        # 模型原始英文标签
    label_cn: str        # 中文名（未收录映射时与英文相同）
    confidence: float    # 置信度 0~1


@dataclass
class Box:
    """像素坐标边界框（左上角 x1,y1；右下角 x2,y2）。"""
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    def union(self, other: "Box") -> "Box":
        """两框并集（覆盖两翼/双眼/双腿整体区域）。"""
        return Box(
            min(self.x1, other.x1), min(self.y1, other.y1),
            max(self.x2, other.x2), max(self.y2, other.y2),
        )

    def clamp_to(self, width: int, height: int) -> "Box":
        """裁剪到图像范围内，防止越界绘制。"""
        return Box(
            min(max(0.0, self.x1), float(width)),
            min(max(0.0, self.y1), float(height)),
            min(max(0.0, self.x2), float(width)),
            min(max(0.0, self.y2), float(height)),
        )

    def to_int_tuple(self) -> Tuple[int, int, int, int]:
        """转 OpenCV 需要的整数元组 (x1, y1, x2, y2)。"""
        return (int(round(self.x1)), int(round(self.y1)),
                int(round(self.x2)), int(round(self.y2)))


def union_boxes(boxes: List[Box]) -> Optional[Box]:
    """多个框的并集；空列表返回 None。"""
    if not boxes:
        return None
    result = boxes[0]
    for box in boxes[1:]:
        result = result.union(box)
    return result


class SpeciesBackend(ABC):
    """物种识别模型抽象基类。"""

    def __init__(self, **params):
        self._loaded = False

    @abstractmethod
    def is_available(self) -> bool:
        """权重文件存在性检查（供回退决策）。"""

    @abstractmethod
    def load(self) -> None:
        """加载权重；失败抛出带中文说明的 ModelLoadError。"""

    @abstractmethod
    def predict(self, pil_image, top_k: int = 3) -> List[SpeciesPrediction]:
        """返回 Top-K 识别结果（按置信度降序）。"""

    @property
    def loaded(self) -> bool:
        return self._loaded


class PartDetectorBackend(ABC):
    """部位检测模型抽象基类。"""

    def __init__(self, **params):
        self._loaded = False

    @abstractmethod
    def is_available(self) -> bool:
        """权重文件存在性检查。"""

    @abstractmethod
    def load(self) -> None:
        """加载权重；失败抛出带中文说明的 ModelLoadError。"""

    @abstractmethod
    def detect(self, pil_image, part_en: str) -> Optional[Box]:
        """检测部位（部位英文名 → 像素框 or None）。

        对称部位（wing/eye/leg）由适配器内部合并左右实例后返回并集框。
        """

    @abstractmethod
    def detect_bird(self, pil_image) -> Optional[Box]:
        """检测鸟体整框（部位未检出时回退链使用）。"""

    @property
    def loaded(self) -> bool:
        return self._loaded
