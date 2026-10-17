# -*- coding: utf-8 -*-
"""训练性能推导函数单元测试：批大小 / 加载进程数按模式与架构的推导规则。

设计为无 GPU 也可运行：推导函数本身的设备差异通过 monkeypatch 隔离。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_trainer  # noqa: E402


def _patch_gpu(monkeypatch, gpu_bytes, avail_bytes=16 * 1024 ** 3):
    """伪造 GPU 显存与系统可用内存，隔离设备差异。"""
    monkeypatch.setattr(model_trainer, "get_gpu_memory_bytes",
                        lambda: gpu_bytes)
    monkeypatch.setattr(model_trainer, "get_system_memory_bytes",
                        lambda: (32 * 1024 ** 3, avail_bytes))


def _patch_mem(monkeypatch, avail_bytes):
    """伪造系统可用内存，隔离 workers 推导对真实机器内存的依赖。"""
    monkeypatch.setattr(model_trainer, "get_system_memory_bytes",
                        lambda: (32 * 1024 ** 3, avail_bytes))


def test_auto_batch_resnet_amp_large_gpu(monkeypatch):
    """12GB 显存 + ResNet + AMP：推导值应触及适应模式上限 128。"""
    _patch_gpu(monkeypatch, 12 * 1024 ** 3)
    batch, lines = model_trainer.derive_auto_batch("adaptive", "resnet50", True)
    assert 8 <= batch <= 128
    assert batch == 128  # 12GB×55%÷45MB≈151，超过上限 128，应被截断
    assert any("最终批大小" in line for line in lines)


def test_auto_batch_vit_costs_more_than_resnet(monkeypatch):
    """同显存下 ViT 每样本估算更高，批大小应不大于 ResNet。"""
    _patch_gpu(monkeypatch, 6 * 1024 ** 3)
    batch_vit, _ = model_trainer.derive_auto_batch(
        "adaptive", "vit_bird_525", True)
    batch_rn, _ = model_trainer.derive_auto_batch("adaptive", "resnet50", True)
    assert batch_vit <= batch_rn


def test_auto_batch_fp32_smaller_than_amp(monkeypatch):
    """关闭 AMP 时每样本按 2 倍估算，批大小应下降。"""
    _patch_gpu(monkeypatch, 3 * 1024 ** 3)
    batch_amp, _ = model_trainer.derive_auto_batch("high", "resnet50", True)
    batch_fp32, _ = model_trainer.derive_auto_batch("high", "resnet50", False)
    assert batch_fp32 < batch_amp


def test_auto_batch_no_gpu_cpu_fallback(monkeypatch):
    """无 GPU 走 CPU 安全回退 batch=8。"""
    monkeypatch.setattr(model_trainer, "get_gpu_memory_bytes", lambda: None)
    batch, _ = model_trainer.derive_auto_batch("adaptive", "resnet50", False)
    assert batch == 8


def test_auto_workers_low_fixed_two(monkeypatch):
    """低速模式固定 2 个加载进程（内存充足时不触发约束）。"""
    _patch_mem(monkeypatch, 32 * 1024 ** 3)
    workers, lines = model_trainer.derive_auto_workers("low")
    assert workers == 2
    assert any("workers=2" in line for line in lines)


def test_auto_workers_high_not_less_than_adaptive(monkeypatch):
    """高速模式的 workers 下限（4）不低于适应模式（2），且区间正确。"""
    _patch_mem(monkeypatch, 32 * 1024 ** 3)
    ad_w, _ = model_trainer.derive_auto_workers("adaptive")
    hi_w, _ = model_trainer.derive_auto_workers("high")
    assert 2 <= ad_w <= 8
    assert 4 <= hi_w <= 12
    assert hi_w >= ad_w


def test_auto_workers_none_falls_back_to_adaptive(monkeypatch):
    """无模式（旧路径调用）按适应模式推导，两种传法结果一致。"""
    _patch_mem(monkeypatch, 32 * 1024 ** 3)
    assert model_trainer.derive_auto_workers(None)[0] == \
        model_trainer.derive_auto_workers("adaptive")[0]


def test_auto_workers_memory_cap(monkeypatch):
    """可用内存不足时，按三池进程总量（每进程约 800MB）下调 workers。"""
    _patch_mem(monkeypatch, 4 * 1024 ** 3)
    # 4GB×60%÷800MB≈3 为三池总量上限；训练 w + 评估 2×min(w,2)，
    # 仅 w=1 时总量 1+2=3 不超限，high 原推导（≥4）应被限制到 1
    workers, lines = model_trainer.derive_auto_workers("high")
    assert workers == 1
    assert any("限制到" in line for line in lines)


def test_auto_workers_memory_plenty_no_cap(monkeypatch):
    """可用内存充足时，不触发内存限制，保持 CPU 推导结果。"""
    _patch_mem(monkeypatch, 32 * 1024 ** 3)
    cpu = os.cpu_count() or 4
    expected = max(4, min(12, cpu // 2))
    workers, _ = model_trainer.derive_auto_workers("high")
    assert workers == expected


def test_infer_architecture_branches():
    """基础模型标识 → 架构推断：内置两支 + 未知回退 resnet50。"""
    assert model_trainer._infer_architecture("resnet50") == "resnet50"
    assert model_trainer._infer_architecture("vit_bird_525") == "vit_bird_525"
    assert model_trainer._infer_architecture("__不存在__") == "resnet50"
    assert model_trainer._infer_architecture("") == "resnet50"


def test_amp_compat_shim_available():
    """AMP 兼容层：有 torch 时新旧版 API 至少一支可用，amp_supported 一致。"""
    assert model_trainer._amp_autocast is not None
    assert model_trainer._amp_grad_scaler is not None
    assert callable(model_trainer._amp_autocast)
    assert callable(model_trainer._amp_grad_scaler)
    assert model_trainer.amp_supported() is True


def test_probe_batch_size_cpu_short_circuit():
    """批大小实测探测：非 CUDA 设备直接返回原值，不做任何探测。"""
    import torch
    result = model_trainer.probe_batch_size(
        model=None, device=torch.device("cpu"), batch_size=64,
        img_size=224, num_classes=10, amp_enabled=False, logger=lambda *_: None)
    assert result == 64
