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
    {"cmd": "answer", "image_path": "...", "question": "...",
     "marker_style": "frame|circle|省略",
     "species_model": "注册名|省略", "part_model": "注册名|省略"}

响应格式（每行一个 JSON 对象）：
    {"ok": true, "cmd": "ping"}
    {"ok": true, "species": [[注册名, 显示文本, 可用], ...],
               "part":    [[注册名, 显示文本, 可用], ...]}
    {"ok": true, "message": "回答文本", "marked_image_path": "路径或null", ...}
    {"ok": false, "error": "中文错误说明"}

用法：
    <训练环境python> -u qa_server.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback

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


def _send(payload: dict) -> None:
    """向协议专用通道写一行 JSON 响应并立即flush。"""
    if _protocol_out is not None:
        _protocol_out.write(json.dumps(payload, ensure_ascii=False) + "\n")
        _protocol_out.flush()
    else:                              # 兜底（重定向失败时退回普通 stdout）
        sys.__stdout__.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.__stdout__.flush()


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


def _handle_answer(req: dict) -> dict:
    """执行一次视觉问答并序列化结果。"""
    import vqa_engine
    answer = vqa_engine.answer_question(
        image_path=req.get("image_path"),
        question=req.get("question") or "",
        marker_style=req.get("marker_style") or None,
        species_model=req.get("species_model") or None,
        part_model=req.get("part_model") or None,
    )
    return {
        "ok": bool(answer.ok),
        "message": answer.message,
        "marked_image_path": str(answer.marked_image_path) if answer.marked_image_path else None,
        "part_cn": answer.part_cn,
        "is_estimated": bool(answer.is_estimated),
        "species_model_used": answer.species_model_used,
        "part_model_used": answer.part_model_used,
    }


def _dispatch(req: dict) -> dict:
    """按 cmd 分发请求；未知命令返回中文错误。"""
    cmd = req.get("cmd")
    if cmd == "ping":
        return {"ok": True, "cmd": "ping"}
    if cmd == "models":
        return _handle_models()
    if cmd == "answer":
        return _handle_answer(req)
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
