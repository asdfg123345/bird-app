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
    part_box: Optional[List[int]] = None  # 部位像素框 [x1, y1, x2, y2]（供界面点击交互）
    description: Optional[str] = None     # 部位特征描述文本
    marked_image_path: Optional[Path] = None  # 标记图输出路径
    species_model_used: Optional[str] = None  # 实际使用的物种模型注册名
    part_model_used: Optional[str] = None     # 实际使用的部位模型注册名


@dataclass
class PartHit:
    """单个部位的结构化检测结果（一键全部位检测使用）。"""
    part_en: str                          # 语义部位英文名
    part_cn: str                          # 语义部位中文名
    box: List[int]                        # 部位像素框 [x1, y1, x2, y2]
    is_estimated: bool                    # 是否为空间先验估计框（当前恒为 False）
    description: str                      # 该部位的科普式特征描述文本


@dataclass
class PartsDetectAnswer:
    """一键全部位检测结果结构体。"""
    ok: bool                              # 整体是否成功（至少检出 1 个部位即成功）
    message: str                          # 汇总中文文本（可直接展示）
    parts: List[PartHit] = field(default_factory=list)   # 检出部位的结构化结果
    detected_count: int = 0               # 检出部位数量
    marked_image_path: Optional[Path] = None  # 全部位标注图输出路径
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


def _safe_report(progress_callback, stage, current=None, total=None):
    """安全触发进度回调：回调为 None 或回调异常均不影响主流程。

    统一进度协议：progress_callback(阶段文本, 当前步, 总步数)；
    current 为 None 表示无法确定步数（如模型加载），界面按忙碌动画展示。
    """
    if progress_callback is None:
        return
    try:
        progress_callback(stage, current, total)
    except Exception:
        pass                       # 进度仅用于展示，绝不能中断推理链路


def answer_question(
    image_path,
    question: str,
    marker_style: Optional[str] = None,
    species_model: Optional[str] = None,
    part_model: Optional[str] = None,
    out_dir=None,
    progress_callback=None,
) -> VqaAnswer:
    """回答一个鸟类视觉问题。

    参数：
        image_path   图片路径
        question     中文问题（如"这种鸟的翅膀长啥样？"）
        marker_style 标记样式 "frame"/"circle"，None 用 config 默认
        species_model / part_model  注册表名称，None 用 config 激活项
        out_dir      标记图输出目录，None 用 config.OUTPUT_DIR
        progress_callback  进度回调（阶段文本, 当前步, 总步数），见 _safe_report
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
    _safe_report(progress_callback, "正在加载物种识别模型（首次加载较慢，请稍候）...")
    try:
        species_backend = get_species_backend(species_model)
        _safe_report(progress_callback, "正在进行物种识别...", 1, 1)
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
    _safe_report(progress_callback, "正在加载部位检测模型并定位部位（首次加载较慢，请稍候）...", 2, 4)
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
    _safe_report(progress_callback, "正在生成部位特征描述...", 3, 4)
    try:
        bird_box = None  # 描述侧重部位区域本身；鸟体框未知时形状占比按整图估算
        description_obj = describe_part(
            pil_image, location.box,
            bird_box=bird_box, part_cn=parsed.part_cn,
        )
        description = description_obj.full_text
    except Exception as exc:
        description = f"特征描述生成失败：{exc}"

    # 结构化部位框（收敛到图像范围内取整，供界面做点击交互）
    clamped_box = location.box.clamp_to(pil_image.size[0], pil_image.size[1])
    part_box = list(clamped_box.to_int_tuple())

    # ---- 标记绘制 ----
    _safe_report(progress_callback, "正在绘制部位标记图...", 4, 4)
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
        part_box=part_box,
        description=description,
        marked_image_path=marked_path,
        species_model_used=species_used,
        part_model_used=part_used,
    )


def _iou(box_a, box_b) -> float:
    """计算两个 Box 的交并比（过滤"部位框≈鸟体整框"的粗粒度定位）。"""
    ax1, ay1, ax2, ay2 = box_a.to_int_tuple()
    bx1, by1, bx2, by2 = box_b.to_int_tuple()
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / float(area_a + area_b - inter)


def detect_all_parts(
    image_path,
    marker_style: Optional[str] = None,
    part_model: Optional[str] = None,
    out_dir=None,
    progress_callback=None,
) -> PartsDetectAnswer:
    """一键全部位检测：无需提问，对整图直接检测全部 12 个语义部位。

    与提问链路解耦的独立入口：批量调用检测器直接检出（不走空间先验
    估计——12 个估计框互相重叠不可读），对检出部位生成科普式特征描述，
    并把全部检出框绘制到同一张标注图上；结构化结果（parts）携带每个
    部位的像素框坐标，供界面做"点击部位标记显示名称"的交互。

    参数：
        image_path   图片路径
        marker_style 标记样式 "frame"/"circle"，None 用 config 默认
        part_model   部位模型注册名，None 用 config 激活项
        out_dir      标注图输出目录，None 用 config.OUTPUT_DIR
        progress_callback  进度回调（阶段文本, 当前步, 总步数），见 _safe_report
    """
    style = marker_style or config.GUI_DEFAULT_MARKER
    output_dir = Path(out_dir) if out_dir else config.OUTPUT_DIR

    image_path = Path(image_path)
    if not image_path.is_file():
        return PartsDetectAnswer(ok=False, message=f"图片文件不存在：{image_path}")

    total_parts = len(config.PART_CATEGORIES)
    total_steps = total_parts + 2    # 部位检测 + 特征描述 + 标注图绘制

    # 各模块在函数内导入，避免云端训练环境引入 GUI/引擎依赖
    from part_locator import get_part_detector
    from feature_describer import describe_part
    from annotator import annotate_multi

    try:
        pil_image = Image.open(image_path).convert("RGB")
    except Exception as exc:
        return PartsDetectAnswer(ok=False, message=f"图片读取失败：{exc}")

    # ---- 获取部位检测器（含注册表回退与实例缓存） ----
    _safe_report(progress_callback, "正在加载部位检测模型（首次加载较慢，请稍候）...")
    try:
        part_backend = get_part_detector(part_model)
        part_used = part_model or config.ACTIVE_PART_MODEL
    except ModelUnavailableError as exc:
        return PartsDetectAnswer(ok=False, message=f"部位检测模型不可用：{exc}")
    except Exception as exc:
        return PartsDetectAnswer(ok=False, message=f"部位检测模型加载失败：{exc}")

    # ---- 批量检测：鸟体整框 + 全部部位框（一次前向或逐部位回退实现） ----
    # 鸟体整框用于过滤"部位框≈整鸟"的粗粒度结果：占满画面的特写照片上，
    # OWL-ViT 对各部位往往只给出整鸟大小的框，12 个部位会重叠为同一大框、
    # 失去定位意义；与鸟体框几乎重合（IoU≥0.9）的部位框按"未精确定位"处理。
    def _batch_progress(done: int) -> None:
        _safe_report(progress_callback,
                     f"正在检测部位（{done}/{total_parts}）...", done, total_steps)

    try:
        parts_boxes, bird_box = part_backend.detect_parts_batch(
            pil_image, list(config.PART_CATEGORIES), progress=_batch_progress)
    except Exception as exc:
        return PartsDetectAnswer(ok=False, message=f"部位检测失败：{exc}")

    # ---- 逐部位整理结果（IoU 过滤 + 科普式特征描述） ----
    _safe_report(progress_callback, "正在生成各部位特征描述...",
                 total_parts + 1, total_steps)
    hits: List[PartHit] = []
    annot_items = []               # annotate_multi 的 (Box, 中文标签) 项
    missed_cn: List[str] = []
    img_w, img_h = pil_image.size
    for part_en in config.PART_CATEGORIES:
        part_cn = config.PART_CN_NAMES[part_en]
        box = parts_boxes.get(part_en)
        if box is not None and bird_box is not None \
                and _iou(box, bird_box) >= 0.9:
            box = None             # 部位框与鸟体几乎重合：模型未能精确定位该部位
        if box is None:
            missed_cn.append(part_cn)
            continue
        # 科普式特征描述：生成失败不影响检测展示，仅记录提示
        try:
            description = describe_part(
                pil_image, box, bird_box=None, part_cn=part_cn).full_text
        except Exception as exc:
            description = f"特征描述生成失败：{exc}"
        clamped = box.clamp_to(img_w, img_h)
        hits.append(PartHit(
            part_en=part_en,
            part_cn=part_cn,
            box=list(clamped.to_int_tuple()),
            is_estimated=False,
            description=description,
        ))
        annot_items.append((clamped, part_cn))

    # ---- 汇总提示（无任何检出时区分"鸟都未检出"与"部位均未检出"） ----
    total = len(config.PART_CATEGORIES)
    if not hits:
        if bird_box is None:
            return PartsDetectAnswer(
                ok=False,
                message=("未能在图片中定位鸟类目标，无法进行部位检测，"
                         "请换一张鸟体更清晰的图片。"),
                part_model_used=part_used,
            )
        return PartsDetectAnswer(
            ok=False,
            message=(f"已定位到鸟体，但 {total} 个部位均未能精确定位"
                     f"（可能鸟体过小、占满画面或部位被遮挡）。\n"
                     "提示：可在「问题互动」中单独询问某个部位，"
                     "系统将按鸟体空间先验给出估计位置。"),
            part_model_used=part_used,
        )

    # ---- 全部位标注图（一次绘制全部检出框） ----
    _safe_report(progress_callback, "正在绘制全部位标注图...", total_steps, total_steps)
    marked_path = None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        marked_path = annotate_multi(
            image_path, annot_items, style=style, out_dir=output_dir)
    except Exception as exc:
        marked_note = f"（标注图生成失败：{exc}）"
    else:
        marked_note = ""

    # ---- 汇总科普文案：逐部位描述清单 + 未检出部位说明 ----
    lines = [f"部位检测完成：共 {len(hits)}/{total} 个部位直接检出"
             f"（检测模型：{part_used}）。", ""]
    for hit in hits:
        lines.append(f"【{hit.part_cn}】")
        lines.append(hit.description)
        lines.append("")
    if missed_cn:
        lines.append("未精确定位：" + "、".join(missed_cn))
        lines.append("提示：可在「问题互动」中单独询问上述部位，"
                     "系统将按鸟体空间先验给出估计位置。")
    lines.append(f"标注图：{marked_path if marked_path else '未生成'}{marked_note}")
    message = "\n".join(lines)

    return PartsDetectAnswer(
        ok=True, message=message, parts=hits,
        detected_count=len(hits), marked_image_path=marked_path,
        part_model_used=part_used,
    )
