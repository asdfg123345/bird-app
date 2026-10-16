# -*- coding: utf-8 -*-
"""
qa_server.py —— 视觉问答子进程服务（JSON 行协议）

作者：天咏（明月照今日，风摇过两川。）

说明：
1. 本模块是「控制台复用训练环境跑视觉问答」架构的引擎端：
   打包后的控制台 exe 不含 torch，无法同进程加载视觉模型；
   由本机训练环境的 Python 解释器以子进程方式启动本服务，
   控制台面板通过 stdin/stdout 的 JSON 行协议与之通信；
2. 协议一请求一响应、逐行进行：请求方写一行 JSON（以 \\n 结尾），
   服务处理后在 stdout 写一行 JSON 响应（flush 立即送出）；
3. 引擎与各模型适配器在本进程内懒加载，仅本进程 import torch，
   控制台进程全程不触碰 torch；
4. 任何异常一律转为 {"ok": false, "error": "中文说明"} 响应，
   绝不向 stdout 打印协议之外的杂讯（日志一律走 stderr）。

请求格式（每行一个 JSON 对象）：
    {"cmd": "ping"}
    {"cmd": "models"}
    {"cmd": "preload"}
    {"cmd": "species", "image_path": "...", "species_model": "注册名|省略"}
    {"cmd": "answer", "image_path": "...", "question": "...",
     "marker_style": "frame|circle|省略",
     "species_model": "注册名|省略", "part_model": "注册名|省略"}
    {"cmd": "detect_parts", "image_path": "...",
     "marker_style": "frame|circle|省略", "part_model": "注册名|省略"}

响应格式（每行一个 JSON 对象）：
    {"ok": true, "cmd": "ping"}
    {"ok": true, "species": [[注册名, 显示文本, 可用], ...],
               "part":    [[注册名, 显示文本, 可用], ...]}
    {"ok": true}                                        （模型预热）
    {"ok": true, "species_rows": [[中文名, 置信度], ...],
               "species_model_used": "注册名"}          （仅物种识别）
    {"ok": true, "message": "回答文本", "marked_image_path": "路径或null",
               "part_box": [x1, y1, x2, y2] 或 null, ...}（视觉问答）
    {"ok": true, "message": "汇总文本", "marked_image_path": "路径或null",
               "parts": [[中文名, 英文名, x1, y1, x2, y2, 是否估计, 特征描述], ...],
               "detected_count": 检出数}              （一键全部位检测）
    {"ok": false, "error": "中文错误说明"}

进度事件（可选，仅出现在最终响应之前，不占请求次序）：
    长耗时任务（识别/问答/全部位检测）处理过程中，服务可先发送零到多行
    进度事件再发送最终响应，供请求方实时推进界面进度条：
    {"event": "progress", "stage": "阶段中文说明",
     "current": 当前步数或null, "total": 总步数或null}
    请求方实现时必须循环读行：读到 event=progress 行则更新进度并继续
    读下一行，读到普通响应行（无 event 字段）即为本请求最终响应。

用法：
    <训练环境python> -u qa_server.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# ============================================================
# 协议通道隔离（服务启动第一步，必须在任何模型库加载之前完成）
# ============================================================
# 三方库（modelscope/tqdm 等）会向 stdout 打印下载进度等杂讯，污染
# JSON 行协议。此处做 fd 级重定向：复制原 stdout fd 作为协议专用通道，
# 把 fd1 指向 stderr——此后所有 print / C 级写入全部落入 stderr，
# 只有 _send() 经协议专用 fd 输出，保证一请求一响应的通道纯净。
_protocol_out = None
try:
    _saved_fd = os.dup(1)              # 复制原 stdout fd 作为协议专用通道
    os.dup2(2, 1)                      # fd1 重定向到 stderr，承接全部杂讯
    sys.stdout = os.fdopen(            # Python 级 print 也指向 stderr
        1, "w", encoding="utf-8", errors="replace", buffering=1)
    _protocol_out = os.fdopen(
        _saved_fd, "w", encoding="utf-8", errors="replace", buffering=1)
except OSError:                        # fd 重定向失败时退回普通 stdout 输出
    _protocol_out = None

# stdin 同步重设为 UTF-8：Windows 子进程默认按本地编码（GBK）解码管道，
# 父进程以 UTF-8 写入的中文图片路径会被误解码成乱码（如"鸟类"→"椋熷被"），
# 导致服务端"图片文件不存在"、识别每次必败。stdout 已在上方处理，此处补齐。
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:                      # reconfigure 不可用时改为直接包装底层缓冲
    try:
        import io
        sys.stdin = io.TextIOWrapper(
            sys.stdin.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass                           # 极端环境（stdin 非 tty/管道）保持原状


def _send(payload: dict) -> None:
    """向协议专用通道写一行 JSON 响应并立即flush。"""
    if _protocol_out is not None:
        _protocol_out.write(json.dumps(payload, ensure_ascii=False) + "\n")
        _protocol_out.flush()
    else:                              # 兜底（重定向失败时退回普通 stdout）
        sys.__stdout__.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.__stdout__.flush()


def _progress(stage, current=None, total=None) -> None:
    """向请求方发送一行进度事件（最终响应之前的中间通知）。"""
    _send({"event": "progress", "stage": str(stage),
           "current": current, "total": total})


def _model_rows(registry) -> list:
    """把模型注册表转成 [注册名, 显示文本, 可用] 行列表（异常项按不可用）。"""
    import model_adapters  # 仅本进程需要 torch，延迟到此处导入
    rows = []
    for name, entry in registry.items():
        display = f"{name} | {entry.get('display_name', '')}"
        available = False
        try:
            adapter = entry["adapter"](**entry.get("params", {}))
            available = bool(adapter.is_available())
        except Exception:
            available = False
        if not available:
            display += "（不可用）"
        rows.append([name, display, available])
    return rows


def _handle_models() -> dict:
    """返回物种+部位模型下拉数据（注册名、显示文本、可用性）。"""
    import config
    merged = None
    if hasattr(config, "get_all_species_models"):
        try:
            merged = config.get_all_species_models()
        except Exception:
            merged = None
    species_registry = merged or config.SPECIES_MODELS
    return {
        "ok": True,
        "species": _model_rows(species_registry),
        "part": _model_rows(config.PART_MODELS),
    }


def _handle_preload() -> dict:
    """预热当前激活的物种与部位模型（已加载则命中缓存立即返回）。

    由控制台在引擎就绪后后台调用，把"首次检测需现场加载模型"的等待
    提前转移到预热阶段，用户首次点检测时模型已在缓存中。
    """
    try:
        _progress("正在后台预热模型（首次加载较慢，请稍候）...")
        from species_classifier import get_species_backend
        from part_locator import get_part_detector
        get_species_backend()
        get_part_detector()
    except Exception as exc:
        # 预热失败不致命：正式检测时按原逻辑加载/回退并给出完整提示
        return {"ok": False, "error": f"模型预热失败：{exc}"}
    return {"ok": True}


def _handle_species(req: dict) -> dict:
    """仅执行物种识别（无需提问；供界面「识别物种」按钮调用）。

    模型经 get_species_backend 按注册名获取：注册表为「出厂 + 共享模型库」
    合并视图，激活模型不可用时按配置顺序自动回退。
    """
    from PIL import Image
    from model_backends import ModelLoadError, ModelUnavailableError
    from species_classifier import classify_species, get_species_backend
    image_path = req.get("image_path")
    if not image_path or not Path(str(image_path)).is_file():
        return {"ok": False, "error": f"图片文件不存在：{image_path}"}
    try:
        _progress("正在加载物种识别模型（首次加载较慢，请稍候）...")
        pil_image = Image.open(image_path).convert("RGB")
        backend = get_species_backend(req.get("species_model") or None)
        _progress("正在进行物种识别...", 1, 1)
        top3 = classify_species(pil_image, backend=backend)
    except ModelUnavailableError as exc:
        return {"ok": False, "error": f"物种识别模型不可用：{exc}"}
    except ModelLoadError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"物种识别失败：{exc}"}
    used = getattr(backend, "registry_name", None)
    return {
        "ok": True,
        "species_rows": [[p.label_cn, float(p.confidence)] for p in top3],
        "species_model_used": used,
    }


def _handle_answer(req: dict) -> dict:
    """执行一次视觉问答并序列化结果。"""
    import vqa_engine
    answer = vqa_engine.answer_question(
        image_path=req.get("image_path"),
        question=req.get("question") or "",
        marker_style=req.get("marker_style") or None,
        species_model=req.get("species_model") or None,
        part_model=req.get("part_model") or None,
        progress_callback=_progress,
    )
    return {
        "ok": bool(answer.ok),
        "message": answer.message,
        "marked_image_path": str(answer.marked_image_path) if answer.marked_image_path else None,
        "part_cn": answer.part_cn,
        "is_estimated": bool(answer.is_estimated),
        "part_box": list(answer.part_box) if answer.part_box else None,
        "description": answer.description,
        "species_model_used": answer.species_model_used,
        "part_model_used": answer.part_model_used,
    }


def _handle_detect_parts(req: dict) -> dict:
    """一键全部位检测（无需提问）：检测全部语义部位并返回结构化框。

    parts 行结构：[中文名, 英文名, x1, y1, x2, y2, 是否估计, 特征描述]，
    供控制台界面做"点击部位标记显示名称"的交互。
    """
    import vqa_engine
    result = vqa_engine.detect_all_parts(
        image_path=req.get("image_path"),
        marker_style=req.get("marker_style") or None,
        part_model=req.get("part_model") or None,
        progress_callback=_progress,
    )
    return {
        "ok": bool(result.ok),
        "message": result.message,
        "marked_image_path": str(result.marked_image_path) if result.marked_image_path else None,
        "parts": [[hit.part_cn, hit.part_en,
                   hit.box[0], hit.box[1], hit.box[2], hit.box[3],
                   bool(hit.is_estimated), hit.description] for hit in result.parts],
        "detected_count": int(result.detected_count),
        "part_model_used": result.part_model_used,
    }


def _dispatch(req: dict) -> dict:
    """按 cmd 分发请求；未知命令返回中文错误。"""
    cmd = req.get("cmd")
    if cmd == "ping":
        return {"ok": True, "cmd": "ping"}
    if cmd == "models":
        return _handle_models()
    if cmd == "preload":
        return _handle_preload()
    if cmd == "species":
        return _handle_species(req)
    if cmd == "answer":
        return _handle_answer(req)
    if cmd == "detect_parts":
        return _handle_detect_parts(req)
    return {"ok": False, "error": f"未知命令：{cmd}"}


def main() -> int:
    """主循环：逐行读请求、逐行写响应；EOF 或致命错误时退出。"""
    # 启动即回一行就绪信号，请求方可据此确认服务可用
    _send({"ok": True, "cmd": "ready"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("请求必须是 JSON 对象")
            _send(_dispatch(req))
        except Exception as exc:          # 协议解析或处理异常：统一中文错误响应
            traceback.print_exc(file=sys.stderr)   # 详细堆栈走 stderr，不污染协议
            _send({"ok": False, "error": f"服务处理失败：{exc}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
