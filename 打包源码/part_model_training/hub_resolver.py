# -*- coding: utf-8 -*-
"""
hub_resolver.py —— HuggingFace / 魔搭（ModelScope）开源模型底座解析器

作者：天咏（明月照今日，风摇过两川。）

【解析顺序】（与视觉系统 resolve_model_path 策略一致）
1. 本地目录（存在 config.json 的目录）→ 直接返回；
2. 魔搭 ModelScope snapshot_download（国内直连，优先）；
3. HF 镜像（默认 https://hf-mirror.com，可经环境变量 HF_ENDPOINT 覆盖）；
4. 全部失败时抛出中文异常（调用方转成用户可读提示）。

已下载过的模型 transformers 会自动复用本地缓存，不会重复下载。
"""

import os
from pathlib import Path

# HF 镜像端点：环境变量优先，默认 hf-mirror.com（与视觉系统配置一致）
DEFAULT_MIRROR = "https://hf-mirror.com"


def is_local_model_dir(path_str: str) -> bool:
    """判断给定路径是否为可直接加载的本地模型目录（含 config.json）。"""
    p = Path(path_str)
    return p.is_dir() and (p / "config.json").is_file()


def resolve_base_model(model_id: str, quiet: bool = False) -> str:
    """把模型 ID / 本地路径解析为 from_pretrained 可用的路径或 ID。

    Args:
        model_id: 模型 ID（如 nvidia/segformer-b0-finetuned-ade-512-512）
                  或本地模型目录绝对/相对路径
        quiet: 静默模式（不打印解析过程）

    Returns:
        传入本地目录 → 原样返回；
        传入模型 ID → 魔搭下载成功时返回本地快照路径，否则原样返回
        （由 transformers 走 HF 缓存 / HF_ENDPOINT 镜像加载）。

    Raises:
        ValueError: 输入为空。
        RuntimeError: 指明是本地目录但缺少 config.json。
    """
    model_id = (model_id or "").strip()
    if not model_id:
        raise ValueError("模型 ID 不能为空。")

    # ① 本地目录直连（形似 "org/name" 的模型 ID 不会命中：既非已存在
    # 目录也非绝对路径，自然落到后续在线解析分支）
    if Path(model_id).is_dir() or os.path.isabs(model_id):
        p = Path(model_id)
        if not (p / "config.json").is_file():
            raise RuntimeError(
                f"目录 {model_id} 缺少 config.json，不是有效的本地模型目录。")
        if not quiet:
            print(f"【底座】使用本地模型目录：{p}")
        return str(p)

    # ② 魔搭优先（国内直连）
    try:
        from modelscope import snapshot_download
        path = snapshot_download(model_id)
        if not quiet:
            print(f"【底座】已从魔搭 ModelScope 获取：{model_id}")
        return str(path)
    except Exception as exc:  # 魔搭无该模型 / 未安装 modelscope
        if not quiet:
            print(f"【底座】魔搭获取失败（{type(exc).__name__}），回退 HF 镜像…")

    # ③ HF 镜像回退：设置端点后原样返回 ID，交给 transformers 解析
    os.environ.setdefault("HF_ENDPOINT", DEFAULT_MIRROR)
    if not quiet:
        print(f"【底座】经 HF 镜像加载：{model_id}"
              f"（首次下载需联网，已缓存模型自动复用）")
    return model_id
