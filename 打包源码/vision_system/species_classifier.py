# -*- coding: utf-8 -*-
"""
species_classifier.py —— 物种识别：模型获取（含注册表回退）与物种分类

作者：天咏（明月照今日，风摇过两川。）

设计说明：
1. 模块级缓存按注册名保存已加载的模型实例，同一注册名重复调用直接复用；
2. 激活模型不可用时按 config.SPECIES_FALLBACK_ORDER 顺序回退，
   每步打印中文提示，全部不可用则抛出汇总各模型原因的 ModelUnavailableError；
3. 加载成功的实例设置 registry_name 实例属性，供引擎显示实际使用的模型；
4. 核心链路只依赖 model_backends 抽象基类，不感知任何具体模型实现。
"""

from typing import Dict, List, Optional

import threading

import config
from model_backends import (
    ModelLoadError,
    ModelUnavailableError,
    SpeciesBackend,
    SpeciesPrediction,
)

# 模型实例缓存：键为模型注册名，值为加载成功的模型实例（进程内单例复用）
_BACKEND_CACHE: Dict[str, SpeciesBackend] = {}
# 加载串行锁：界面后台预热线程与识别任务线程可能同时请求加载同一模型，
# 串行化后后到者等待片刻即命中缓存，避免同一模型被重复加载两份
_BACKEND_LOAD_LOCK = threading.Lock()


def _species_registry() -> dict:
    """获取物种模型注册表：优先「出厂 + 共享模型库」合并视图。

    防御性接入说明：config.get_all_species_models() 由共享模型库任务提供，
    用 hasattr 探测其是否存在（config 模块 __getattr__ 对未知属性抛
    AttributeError，hasattr 可安全返回 False）；接口缺失或调用异常时
    退回出厂注册表 config.SPECIES_MODELS，保证回退链始终可用。
    """
    if hasattr(config, "get_all_species_models"):
        try:
            merged = config.get_all_species_models()
            if merged:
                return merged
        except Exception:
            pass  # 合并接口异常时退回出厂注册表，不抛出
    return config.SPECIES_MODELS


def get_species_backend(name: Optional[str] = None) -> SpeciesBackend:
    """获取已加载的物种识别模型，含注册表回退与单例缓存。

    参数：
        name  模型注册名，None 时使用 config.ACTIVE_SPECIES_MODEL

    返回：
        加载成功的物种识别模型实例（已设置 registry_name 实例属性）

    异常：
        ModelUnavailableError  激活模型与全部回退模型均不可用
    """
    requested = name or config.ACTIVE_SPECIES_MODEL

    # 请求名称已有可用缓存：直接复用，不再重复加载
    if requested in _BACKEND_CACHE:
        return _BACKEND_CACHE[requested]

    with _BACKEND_LOAD_LOCK:
        # 拿到锁后重查缓存：等待期间其他线程可能已完成加载
        if requested in _BACKEND_CACHE:
            return _BACKEND_CACHE[requested]

        # 注册表：出厂 + 共享模型库合并视图（共享库模型 id 同样可解析加载）
        registry = _species_registry()

        failure_reasons: List[str] = []
        # 候选顺序：先尝试请求模型本身，再按配置的回退顺序尝试其余模型
        candidates = [requested] + [
            item for item in config.SPECIES_FALLBACK_ORDER if item != requested
        ]

        for position, candidate in enumerate(candidates):
            # 回退链上的模型若已有缓存实例，直接复用并提示回退结果
            if candidate in _BACKEND_CACHE:
                if position > 0:
                    print(f"激活模型 {requested} 不可用，回退到 {candidate}（缓存实例）")
                return _BACKEND_CACHE[candidate]

            if position > 0:
                print(f"激活模型 {requested} 不可用，回退到 {candidate}")

            # 注册表登记检查：未登记的名称无法实例化
            if candidate not in registry:
                failure_reasons.append(f"{candidate}：未在物种模型注册表中登记")
                continue
            entry = registry[candidate]

            # 按注册表参数实例化适配器，并记录实际使用的注册名
            backend = entry["adapter"](**entry.get("params", {}))
            backend.registry_name = candidate

            # 权重文件缺失：记录中文原因并进入下一步回退
            if not backend.is_available():
                failure_reasons.append(f"{candidate}：模型文件缺失或不可用")
                continue

            # 加载权重；失败按中文原因记录后继续回退
            try:
                backend.load()
            except ModelLoadError as exc:
                failure_reasons.append(f"{candidate}：{exc}")
                continue

            # 加载成功：写入缓存，后续同名请求直接复用
            _BACKEND_CACHE[candidate] = backend
            return backend

    raise ModelUnavailableError(
        "所有候选物种识别模型均不可用：" + "；".join(failure_reasons)
    )


def classify_species(
    pil_image,
    backend: Optional[SpeciesBackend] = None,
    top_k: int = 3,
) -> List[SpeciesPrediction]:
    """识别图片中的鸟类物种，返回按置信度降序排列的 Top-K 结果。"""
    if backend is None:
        backend = get_species_backend()
    return backend.predict(pil_image, top_k)
