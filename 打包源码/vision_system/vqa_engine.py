# -*- coding: utf-8 -*-
"""
vqa_engine.py —— VQA 引擎：解析 → 识别 → 定位 → 描述 → 标记 全链路编排

作者：天咏（明月照今日，风摇过两川。）

统一捕获各环节异常并转为中文提示；模型参数可覆盖 config 激活项。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from PIL import Image

import config
from model_backends import ModelLoadError, ModelUnavailableError, SpeciesPrediction


@dataclass
class VqaAnswer:
    """问答结果结构体。"""
    ok: bool                              # 整体是否成功
    message: str                          # 汇总中文回答文本（可直接展示）
    species_top1: Optional[SpeciesPrediction] = None
    species_top3: List[SpeciesPrediction] = field(default_factory=list)
    part_en: Optional[str] = None         # 部位英文类别（未提问部位时为 None）
    part_cn: Optional[str] = None         # 部位中文名
    is_estimated: bool = False            # 部位框是否为空间先验估计
    description: Optional[str] = None     # 部位特征描述文本
    marked_image_path: Optional[Path] = None  # 标记图输出路径
    species_model_used: Optional[str] = None  # 实际使用的物种模型注册名
    part_model_used: Optional[str] = None     # 实际使用的部位模型注册名


def _format_species(top3: List[SpeciesPrediction]) -> str:
    """物种结果格式化为中文文本。"""
    if not top3:
        return "物种识别：无结果"
    lines = []
    for idx, pred in enumerate(top3):
        rank = "Top-1" if idx == 0 else f"Top-{idx + 1}"
        lines.append(f"{rank}：{pred.label_cn}（{pred.confidence * 100:.1f}%）")
    return "\n".join(lines)


def answer_question(
    image_path,
    question: str,
    marker_style: Optional[str] = None,
    species_model: Optional[str] = None,
    part_model: Optional[str] = None,
    out_dir=None,
) -> VqaAnswer:
    """回答一个鸟类视觉问题。

    参数：
        image_path   图片路径
        question     中文问题（如"这种鸟的翅膀长啥样？"）
        marker_style 标记样式 "frame"/"circle"，None 用 config 默认
        species_model / part_model  注册表名称，None 用 config 激活项
        out_dir      标记图输出目录，None 用 config.OUTPUT_DIR
    """
    style = marker_style or config.GUI_DEFAULT_MARKER
    output_dir = Path(out_dir) if out_dir else config.OUTPUT_DIR

    image_path = Path(image_path)
    if not image_path.is_file():
        return VqaAnswer(ok=False, message=f"图片文件不存在：{image_path}")
    if not question or not question.strip():
        return VqaAnswer(ok=False, message="问题不能为空，请输入中文问题（如：这种鸟的翅膀长啥样？）")

    # 各模块在函数内导入，避免云端训练环境引入 GUI/引擎依赖
    from question_parser import available_parts_hint, parse_question
    from species_classifier import classify_species, get_species_backend
    from part_locator import PartLocationError, locate_part
    from feature_describer import describe_part
    from annotator import annotate

    try:
        pil_image = Image.open(image_path).convert("RGB")
    except Exception as exc:
        return VqaAnswer(ok=False, message=f"图片读取失败：{exc}")

    parsed = parse_question(question)

    # ---- 物种识别（必定执行） ----
    try:
        species_backend = get_species_backend(species_model)
        top3 = classify_species(pil_image, backend=species_backend)
        species_used = species_backend.__class__.registry_name \
            if hasattr(species_backend.__class__, "registry_name") else species_model or config.ACTIVE_SPECIES_MODEL
    except ModelUnavailableError as exc:
        return VqaAnswer(ok=False, message=f"物种识别模型不可用：{exc}")
    except ModelLoadError as exc:
        return VqaAnswer(ok=False, message=str(exc))
    except Exception as exc:
        return VqaAnswer(ok=False, message=f"物种识别失败：{exc}")

    parts_text = _format_species(top3)
    top1 = top3[0] if top3 else None

    # ---- 问题未提及部位：仅物种识别 ----
    if parsed.part_en is None:
        hint = available_parts_hint()
        message = (
            f"{parts_text}\n\n"
            f"未识别到部位关键词，仅完成物种识别。\n"
            f"可用部位：{hint}\n"
            f"示例问题：这种鸟的{hint.split('/')[0]}长啥样？"
        )
        return VqaAnswer(
            ok=True, message=message,
            species_top1=top1, species_top3=top3,
            species_model_used=species_used,
        )

    # ---- 部位定位 ----
    try:
        detector = None
        part_backend = None
        from part_locator import get_part_detector
        part_backend = get_part_detector(part_model)
        part_used = part_model or config.ACTIVE_PART_MODEL
        location = locate_part(pil_image, parsed.part_en, detector=part_backend)
    except PartLocationError as exc:
        # 部位定位失败：保留物种结果，不产生误导性标记
        message = f"{parts_text}\n\n部位定位失败：{exc}"
        return VqaAnswer(
            ok=False, message=message,
            species_top1=top1, species_top3=top3,
            part_en=parsed.part_en, part_cn=parsed.part_cn,
            species_model_used=species_used,
        )
    except (ModelUnavailableError, ModelLoadError) as exc:
        message = f"{parts_text}\n\n部位定位模型不可用：{exc}"
        return VqaAnswer(
            ok=False, message=message,
            species_top1=top1, species_top3=top3,
            species_model_used=species_used,
        )
    except Exception as exc:
        message = f"{parts_text}\n\n部位定位失败：{exc}"
        return VqaAnswer(
            ok=False, message=message,
            species_top1=top1, species_top3=top3,
            species_model_used=species_used,
        )

    # ---- 特征描述 ----
    try:
        bird_box = None  # 描述侧重部位区域本身；鸟体框未知时形状占比按整图估算
        description_obj = describe_part(
            pil_image, location.box,
            bird_box=bird_box, part_cn=parsed.part_cn,
        )
        description = description_obj.full_text
    except Exception as exc:
        description = f"特征描述生成失败：{exc}"

    # ---- 标记绘制 ----
    marked_path = None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        marked_path = annotate(
            image_path, location.box, parsed.part_cn,
            style=style, out_dir=output_dir,
        )
    except Exception as exc:
        description = f"{description}\n（标记图生成失败：{exc}）"

    # ---- 汇总回答 ----
    est_note = "\n【估计位置】该部位未自动检出，位置按鸟体空间先验估计，仅供参考。" \
        if location.is_estimated else ""
    message = (
        f"{parts_text}\n\n"
        f"询问部位：{parsed.part_cn}{est_note}\n\n"
        f"{description}\n\n"
        f"标记图：{marked_path if marked_path else '未生成'}"
    )
    return VqaAnswer(
        ok=True, message=message,
        species_top1=top1, species_top3=top3,
        part_en=parsed.part_en, part_cn=parsed.part_cn,
        is_estimated=location.is_estimated,
        description=description,
        marked_image_path=marked_path,
        species_model_used=species_used,
        part_model_used=part_used,
    )
