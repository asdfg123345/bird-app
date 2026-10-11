# -*- coding: utf-8 -*-
"""
annotator.py —— 红色标记绘制（方框 / 圆圈），原图永不写回

作者：天咏（明月照今日，风摇过两川。）

说明：
1. 读取使用 np.fromfile + cv2.imdecode，写出使用 cv2.imencode + tofile，
   全程支持中文路径；
2. 所有绘制参数（颜色/线宽/字号/内边距/输出后缀）均来自 config.ANNOTATOR；
3. 原图只读，绘制发生在内存解码副本上，输出为新文件且不覆盖已有同名文件。
"""

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import config
from model_backends import Box

# 标签文字颜色（RGB 白字）
LABEL_TEXT_RGB = (255, 255, 255)


def _load_font():
    """加载配置的中文字体；失败时回退 PIL 默认字体（仍绘制中文，可能显示为方块）。"""
    try:
        return ImageFont.truetype(
            str(config.ANNOTATOR["font_path"]),
            int(config.ANNOTATOR["font_size"]),
        )
    except Exception:
        # 回退行为：字体文件缺失或损坏时使用 PIL 默认字体；
        # 默认字体不含中文字形，中文标签可能显示为方块，但仍会绘制文字内容。
        return ImageFont.load_default()


def _measure_text(font, text):
    """用 font.getbbox 测量文字墨迹尺寸，返回（宽, 高, x 偏移, y 偏移）。"""
    ink = font.getbbox(text) or (0, 0, 0, 0)
    return ink[2] - ink[0], ink[3] - ink[1], ink[0], ink[1]


def _draw_label(pil_img, text, x, y, bg_rgb):
    """在 PIL 图像上以白字红底绘制标签；(x, y) 为底色矩形左上角，自动收敛到图像内。"""
    font = _load_font()
    pad = int(config.ANNOTATOR["label_bg_pad"])
    text_w, text_h, off_x, off_y = _measure_text(font, text)
    img_w, img_h = pil_img.size
    # 标签整体收敛到图像范围内，防止越界被裁掉
    x = int(min(max(0, x), max(0, img_w - text_w - 2 * pad)))
    y = int(min(max(0, y), max(0, img_h - text_h - 2 * pad)))
    draw = ImageDraw.Draw(pil_img)
    draw.rectangle([x, y, x + text_w + 2 * pad, y + text_h + 2 * pad], fill=bg_rgb)
    draw.text((x + pad - off_x, y + pad - off_y), text, font=font, fill=LABEL_TEXT_RGB)


def _clamp_box_int(box: Box, width: int, height: int):
    """把像素框收敛到图像范围内并取整，保证至少 1×1 像素，返回 (x1, y1, x2, y2)。"""
    clamped = box.clamp_to(width, height)
    x1, y1, x2, y2 = clamped.to_int_tuple()
    # 防止框完全越界导致宽高为 0
    x1 = min(max(0, x1), width - 1)
    y1 = min(max(0, y1), height - 1)
    x2 = max(min(width, x2), x1 + 1)
    y2 = max(min(height, y2), y1 + 1)
    return x1, y1, x2, y2


def _next_output_path(out_dir: Path, stem: str) -> Path:
    """生成输出路径：原文件名 + 配置后缀；同名已存在则追加 _2/_3 序号，避免覆盖。"""
    suffix = str(config.ANNOTATOR["output_name_suffix"])
    candidate = out_dir / f"{stem}{suffix}.png"
    seq = 2
    while candidate.exists():
        candidate = out_dir / f"{stem}{suffix}_{seq}.png"
        seq += 1
    return candidate


def annotate(image_path, box: Box, label_cn: str, style: str = "frame", out_dir=None) -> Path:
    """在图片上绘制红色标记并输出为新文件，返回输出文件路径。

    参数：
        image_path  原图路径（str/Path，支持中文路径；原图只读，绝不写回）
        box         部位像素框
        label_cn    中文标签（白字红底绘制）
        style       标记样式："frame" 方框 / "circle" 圆圈
        out_dir     输出目录，None 时用 config.OUTPUT_DIR（自动创建）
    """
    src_path = Path(image_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"图片文件不存在：{src_path}")
    target_dir = Path(out_dir) if out_dir else config.OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    # 读取原图：字节读入 + 内存解码，兼容中文路径；解码结果即内存副本，原文件不动
    raw = np.fromfile(str(src_path), dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"图片解码失败：{src_path}")
    img_h, img_w = image.shape[:2]

    color = tuple(int(c) for c in config.ANNOTATOR["color_bgr"])
    thickness = int(config.ANNOTATOR["thickness"])
    x1, y1, x2, y2 = _clamp_box_int(box, img_w, img_h)

    font = _load_font()
    pad = int(config.ANNOTATOR["label_bg_pad"])
    text_w, text_h, _, _ = _measure_text(font, label_cn)

    if style == "frame":
        # 方框样式：按配置颜色与线宽绘制矩形
        cv2.rectangle(image, (x1, y1), (x2, y2), color, thickness)
        label_x = x1
        label_y = y1 - text_h - 2 * pad
        if label_y < 0:
            # 框上方放不下时改放框内顶部
            label_y = y1 + pad
    elif style == "circle":
        # 圆圈样式：圆心取框中心，半径 = 短边一半 × 配置系数
        center_x = int(round((x1 + x2) / 2))
        center_y = int(round((y1 + y2) / 2))
        radius = int(min(x2 - x1, y2 - y1) / 2 * float(config.ANNOTATOR["circle_pad"]))
        radius = max(radius, 1)
        cv2.circle(image, (center_x, center_y), radius, color, thickness)
        label_x = center_x - text_w // 2
        label_y = center_y - radius - text_h - 2 * pad - 2
        if label_y < 0:
            # 圆圈上方越界时放到圆心下方（圆圈底部之外）
            label_y = center_y + radius + 2
    else:
        raise ValueError(f"未知标记样式：{style}（可选 frame/circle）")

    # 转 PIL 绘制中文标签（白字红底），底色用配置红色（BGR 转 RGB）
    pil_img = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    _draw_label(pil_img, label_cn, label_x, label_y, color[::-1])
    marked = cv2.cvtColor(np.asarray(pil_img), cv2.COLOR_RGB2BGR)

    # 写盘：编码为 PNG 后按字节写出，兼容中文路径；同名自动追加序号
    out_path = _next_output_path(target_dir, src_path.stem)
    ok, buf = cv2.imencode(".png", marked)
    if not ok:
        raise RuntimeError("标记图编码失败，无法写出。")
    buf.tofile(str(out_path))
    return out_path
