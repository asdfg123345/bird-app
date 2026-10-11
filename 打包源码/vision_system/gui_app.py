# -*- coding: utf-8 -*-
"""
gui_app.py —— tkinter 桌面界面：模型选择 / 选图预览 / 提问识别 / 结果展示

作者：天咏（明月照今日，风摇过两川。）

说明：
1. 全项目仅本文件导入 tkinter；main.py 的 gui 分支延迟导入本模块，
   云端训练环境不 import gui_app 即无 GUI 依赖；
2. 全部界面与交互逻辑封装在可嵌入面板 BirdVqaPanel(tk.Frame) 中，
   可放入任意父容器复用；BirdVqaApp(tk.Tk) 仅作为独立窗口包壳，
   保持 main.py gui 入口的原有调用方式完全兼容；
3. 识别过程在子线程执行，结果经 panel.after 回到主线程刷新界面；
4. 窗口标题、默认模型、默认标记样式等一律取自 config，无硬编码；
5. 物种模型下拉防御性接入 config.get_all_species_models()（出厂+共享库
   合并注册表）：该接口尚不存在或调用失败时自动退回 config.SPECIES_MODELS。
"""

import json
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import config
import vqa_engine

# 缩采样滤镜（兼容不同 Pillow 版本）
try:
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:
    _RESAMPLE = Image.LANCZOS

# 预览区与结果区缩略图最大宽度（像素）
_PREVIEW_MAX_WIDTH = 360
_RESULT_MAX_WIDTH = 480


def _adapter_available(entry):
    """实例化适配器并检查可用性；任何异常一律按不可用处理。"""
    try:
        adapter = entry["adapter"](**entry.get("params", {}))
        return bool(adapter.is_available())
    except Exception:
        return False


def _default_display(options, active_name):
    """按注册名找到默认显示文本；找不到时回退首项。"""
    for name, display in options:
        if name == active_name:
            return display
    return options[0][1] if options else ""


def _species_registry():
    """获取物种模型注册表：优先出厂+共享库合并接口，缺失或失败时退回出厂表。

    防御性接入说明：config.get_all_species_models() 由共享模型库任务提供，
    本函数用 hasattr 探测其是否存在（config 模块的 __getattr__ 对未知属性
    会抛 AttributeError，hasattr 可安全返回 False），两个改动天然兼容。
    """
    if hasattr(config, "get_all_species_models"):
        try:
            merged = config.get_all_species_models()
            if merged:
                return merged
        except Exception:
            pass  # 合并接口异常时静默退回出厂注册表，保证界面可用
    return config.SPECIES_MODELS


class QaServiceClient:
    """视觉问答子进程客户端：管理引擎服务进程生命周期与 JSON 行协议通信。

    控制台 exe 不含 torch，无法同进程加载视觉模型；本客户端以用户指定的
    训练环境 Python 解释器拉起 qa_server.py 子进程（qa_service_cmd），
    问答请求与模型下拉数据全部经 stdin/stdout 的 JSON 行协议转发。
    协议细节见 qa_server.py 模块文档。所有方法线程安全（内部持锁）。
    """

    def __init__(self, cmd, cwd=None):
        self._cmd = list(cmd)          # 启动命令（如 [python, "-u", qa_server.py]）
        self._cwd = cwd                # 服务进程工作目录（视觉系统目录）
        self._proc = None              # 服务子进程
        self._lock = threading.Lock()  # 请求串行化（面板同时只发一个请求）

    @property
    def running(self):
        """服务进程是否仍在运行。"""
        return self._proc is not None and self._proc.poll() is None

    def start(self, timeout=90):
        """启动服务子进程并等待就绪信号；失败抛中文异常。"""
        import subprocess
        if self.running:
            return
        self._proc = subprocess.Popen(
            self._cmd, cwd=self._cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,          # 服务日志丢弃，避免管道阻塞
            text=True, encoding="utf-8", errors="replace",
            bufsize=1)                          # 行缓冲
        first = self._proc.stdout.readline()    # 等待服务就绪行
        if not first:
            code = self._proc.poll()
            raise RuntimeError(f"视觉服务进程未就绪即退出（退出码 {code}），"
                               "请检查训练环境解释器与依赖是否可用")
        try:
            payload = json.loads(first)
        except ValueError:
            raise RuntimeError("视觉服务就绪响应不是合法 JSON，服务版本不匹配")
        if not payload.get("ok"):
            raise RuntimeError(f"视觉服务启动失败：{payload.get('error', '未知原因')}")

    def request(self, payload, timeout=1800):
        """发送一个请求并等待响应；超时/进程退出/协议异常均抛中文异常。"""
        import subprocess
        with self._lock:
            if not self.running:
                raise RuntimeError("视觉服务进程未运行")
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            if not line:
                code = self._proc.poll()
                raise RuntimeError(f"视觉服务进程已退出（退出码 {code}）")
            try:
                return json.loads(line)
            except ValueError:
                raise RuntimeError("视觉服务响应不是合法 JSON")

    def models(self):
        """拉取物种/部位模型下拉数据（含可用性标记）。"""
        return self.request({"cmd": "models"})

    def answer(self, image_path, question, marker_style, species_model, part_model):
        """执行一次问答；返回 qa_server 的响应字典。"""
        return self.request({
            "cmd": "answer",
            "image_path": image_path,
            "question": question,
            "marker_style": marker_style or "",
            "species_model": species_model or "",
            "part_model": part_model or "",
        })

    def close(self):
        """终止服务子进程（先礼貌终止，超时后强杀）。"""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass


class _ServiceAnswer:
    """服务响应适配：包装 qa_server 的 answer 响应字典，提供与
    vqa_engine.VqaAnswer 相同的 message / marked_image_path 只读属性，
    使面板结果展示代码两种模式完全复用。"""

    def __init__(self, payload: dict):
        self.message = payload.get("message") or ""
        self.marked_image_path = payload.get("marked_image_path") or None


class BirdVqaPanel(tk.Frame):
    """可嵌入识别面板：左列控制区，右列结果区，底部状态栏。

    对外可编程接口（便于父容器控制）：
      set_image(path)   —— 程序化指定待识别图片并刷新预览，返回是否成功
      refresh_models()  —— 重建模型下拉框（模型注册表变化后调用）
      worker_running    —— 只读属性，识别线程是否运行中
    """

    def __init__(self, master, qa_service_cmd=None, **kwargs):
        super().__init__(master, **kwargs)
        self._image_path = None        # 当前选中的图片路径
        self._preview_photo = None     # 左侧预览图引用（防止被回收）
        self._result_photo = None      # 右侧结果图引用（防止被回收）
        self._worker_running = False   # 识别线程是否运行中
        # 服务模式：提供 qa_service_cmd 时，模型推理整体在训练环境子进程中
        # 运行（控制台 exe 无 torch），面板只负责界面与 JSON 协议转发
        self._service = None           # QaServiceClient（服务模式时创建）
        self._service_cmd = list(qa_service_cmd) if qa_service_cmd else None

        if self._service_cmd:
            # 服务模式：选项列表先留空，待服务就绪后由子进程数据填充；
            # 本地构建选项会触发 config 注册表 → model_adapters → import torch，
            # 在无 torch 的控制台 exe 内必须避开
            self._species_options = []
            self._part_options = []
        else:
            # 生成下拉框选项：注册名 | 显示名，不可用项追加后缀
            self._species_options = self._build_species_options()
            self._part_options = self._build_options(config.PART_MODELS)

        self._build_widgets()

        if self._service_cmd:
            self._set_status("视觉服务启动中（首次加载模型较慢，请稍候）...")
            worker = threading.Thread(target=self._start_service_worker,
                                      daemon=True)
            worker.start()

    # ---------- 服务模式 ----------

    def _start_service_worker(self):
        """服务线程：启动引擎子进程并拉取模型下拉数据，完成后回主线程刷新。"""
        import os
        try:
            client = QaServiceClient(self._service_cmd,
                                     cwd=os.path.dirname(self._service_cmd[-1]))
            client.start()
            payload = client.models()
            if not payload.get("ok"):
                raise RuntimeError(payload.get("error", "模型列表获取失败"))
        except Exception as exc:
            self.after(0, self._on_service_failed, str(exc))
            return
        self._service = client
        self.after(0, self._on_service_models, payload)

    def _on_service_models(self, payload):
        """服务模型数据到达：填充下拉选项并恢复默认选中。"""
        self._species_options = [(n, d) for n, d, _a in payload.get("species", [])]
        self._part_options = [(n, d) for n, d, _a in payload.get("part", [])]
        self._species_combo["values"] = [d for _, d in self._species_options]
        self._species_var.set(
            _default_display(self._species_options, config.ACTIVE_SPECIES_MODEL))
        self._part_combo["values"] = [d for _, d in self._part_options]
        self._part_var.set(
            _default_display(self._part_options, config.ACTIVE_PART_MODEL))
        self._set_status("视觉服务就绪")

    def _on_service_failed(self, message):
        """服务启动失败：状态栏与回答区显示中文原因，界面其余部分可用。"""
        self._set_status("视觉服务启动失败")
        self._result_text.delete("1.0", "end")
        self._result_text.insert(
            "1.0",
            f"视觉问答服务启动失败：\n{message}\n\n"
            "处理办法：请到「模型训练」页确认 Python 解释器已指向本机训练环境"
            "（含 torch/transformers），然后切换到其他页签再切回本页重试。")

    def shutdown(self):
        """关闭服务子进程（父容器销毁前调用，幂等）。"""
        if self._service is not None:
            self._service.close()
            self._service = None

    # ---------- 可编程接口 ----------

    def set_image(self, path):
        """程序化指定待识别图片并刷新预览；返回加载是否成功。"""
        return self._load_image(path)

    def refresh_models(self):
        """重建模型下拉框选项（模型注册表变化后由父容器调用）。"""
        self._species_options = self._build_species_options()
        self._part_options = self._build_options(config.PART_MODELS)
        self._species_combo["values"] = [d for _, d in self._species_options]
        self._species_var.set(
            _default_display(self._species_options, config.ACTIVE_SPECIES_MODEL))
        self._part_combo["values"] = [d for _, d in self._part_options]
        self._part_var.set(
            _default_display(self._part_options, config.ACTIVE_PART_MODEL))
        self._set_status("模型下拉已刷新")

    @property
    def worker_running(self):
        """识别线程是否运行中（供窗口壳关闭确认等场景查询）。"""
        return self._worker_running

    # ---------- 选项构建 ----------

    @staticmethod
    def _build_options(registry):
        """由模型注册表生成（注册名, 显示文本）列表。"""
        options = []
        for name, entry in registry.items():
            display = f"{name} | {entry.get('display_name', '')}"
            if not _adapter_available(entry):
                display += "（不可用）"
            options.append((name, display))
        return options

    @classmethod
    def _build_species_options(cls):
        """构建物种模型下拉选项（防御性接入共享库合并注册表）。"""
        return cls._build_options(_species_registry())

    @staticmethod
    def _registry_of(display, options):
        """由显示文本反查注册名；找不到时返回 None。"""
        for name, text in options:
            if text == display:
                return name
        return None

    # ---------- 界面搭建 ----------

    def _build_widgets(self):
        """按 grid 布局：左列控制区、右列结果区、底部状态栏。"""
        control = ttk.Frame(self, padding=10)
        control.grid(row=0, column=0, sticky="nsw", padx=(10, 5), pady=10)
        result = ttk.Frame(self, padding=10)
        result.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self._build_model_area(control)
        self._build_image_area(control)
        self._build_question_area(control)
        self._build_marker_area(control)
        self._build_status_area()

        ttk.Label(result, text="标记结果图：").pack(anchor="w")
        self._result_image_label = ttk.Label(result, text="（提问后此处显示标记图）")
        self._result_image_label.pack(anchor="w", pady=(0, 8))

        ttk.Label(result, text="回答文本：").pack(anchor="w")
        text_frame = ttk.Frame(result)
        text_frame.pack(fill="both", expand=True)
        self._result_text = tk.Text(text_frame, wrap="word", height=14)
        scroll = ttk.Scrollbar(text_frame, orient="vertical",
                               command=self._result_text.yview)
        self._result_text.configure(yscrollcommand=scroll.set)
        self._result_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_model_area(self, parent):
        """顶部模型选择区：物种模型与部位模型两个只读下拉框。"""
        box = ttk.LabelFrame(parent, text="模型选择", padding=8)
        box.pack(fill="x", pady=(0, 10))

        ttk.Label(box, text="物种模型：").grid(row=0, column=0, sticky="w")
        self._species_var = tk.StringVar(
            value=_default_display(self._species_options, config.ACTIVE_SPECIES_MODEL))
        self._species_combo = ttk.Combobox(
            box, textvariable=self._species_var,
            values=[display for _, display in self._species_options],
            state="readonly", width=44)
        self._species_combo.grid(row=0, column=1, sticky="ew", pady=2)
        self._species_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._validate_selection(
                self._species_var, self._species_options, config.ACTIVE_SPECIES_MODEL))

        ttk.Label(box, text="部位模型：").grid(row=1, column=0, sticky="w")
        self._part_var = tk.StringVar(
            value=_default_display(self._part_options, config.ACTIVE_PART_MODEL))
        self._part_combo = ttk.Combobox(
            box, textvariable=self._part_var,
            values=[display for _, display in self._part_options],
            state="readonly", width=44)
        self._part_combo.grid(row=1, column=1, sticky="ew", pady=2)
        self._part_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._validate_selection(
                self._part_var, self._part_options, config.ACTIVE_PART_MODEL))

        box.columnconfigure(1, weight=1)

    def _build_image_area(self, parent):
        """选图区：选择按钮、路径标签与预览区。"""
        box = ttk.LabelFrame(parent, text="图片", padding=8)
        box.pack(fill="x", pady=(0, 10))
        ttk.Button(box, text="选择图片", command=self._choose_image).pack(anchor="w")
        self._path_label = ttk.Label(box, text="尚未选择图片", wraplength=340)
        self._path_label.pack(anchor="w", pady=(4, 0))
        self._preview_label = ttk.Label(box, text="（预览区）")
        self._preview_label.pack(anchor="w", pady=(6, 0))

    def _build_question_area(self, parent):
        """问题区：输入框与灰色占位提示。"""
        box = ttk.LabelFrame(parent, text="提问", padding=8)
        box.pack(fill="x", pady=(0, 10))
        self._placeholder = "请输入中文问题，例如：这种鸟的翅膀长啥样？"
        self._question_entry = ttk.Entry(box, width=44)
        self._question_entry.pack(fill="x")
        self._question_entry.insert(0, self._placeholder)
        self._question_entry.config(foreground="#808080")
        self._question_entry.bind("<FocusIn>", self._on_question_focus_in)
        self._question_entry.bind("<FocusOut>", self._on_question_focus_out)

    def _build_marker_area(self, parent):
        """标记样式单选区：红框 / 圆圈。"""
        box = ttk.LabelFrame(parent, text="标记样式", padding=8)
        box.pack(fill="x", pady=(0, 10))
        default_marker = config.GUI_DEFAULT_MARKER \
            if config.GUI_DEFAULT_MARKER in ("frame", "circle") else "frame"
        self._marker_var = tk.StringVar(value=default_marker)
        ttk.Radiobutton(box, text="红框（frame）", variable=self._marker_var,
                        value="frame").pack(anchor="w")
        ttk.Radiobutton(box, text="圆圈（circle）", variable=self._marker_var,
                        value="circle").pack(anchor="w")
        self._ask_button = ttk.Button(parent, text="提问", command=self._on_ask)
        self._ask_button.pack(fill="x", pady=(4, 0))

    def _build_status_area(self):
        """底部状态栏。"""
        self._status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self._status_var, anchor="w",
                  relief="sunken").grid(row=1, column=0, columnspan=2,
                                        sticky="ew", padx=10, pady=(0, 8))

    # ---------- 模型选择校验 ----------

    def _validate_selection(self, var, options, default_name):
        """选中不可用项时提示并回退默认模型。"""
        display = var.get()
        for name, text in options:
            if text == display:
                if "（不可用）" in text:
                    self._set_status(f"模型 {name} 当前不可用，已回退为默认模型")
                    var.set(_default_display(options, default_name))
                return

    # ---------- 选图 ----------

    def _choose_image(self):
        """弹出文件选择框并加载预览缩略图。"""
        path = filedialog.askopenfilename(
            title="选择鸟类图片",
            filetypes=[("图片文件", "*.jpg *.jpeg *.png *.bmp *.webp"),
                       ("所有文件", "*.*")])
        if not path:
            return
        self._load_image(path)

    def _load_image(self, path):
        """记录路径并加载预览缩略图；读取失败时提示并返回 False。"""
        self._image_path = path
        self._path_label.config(text=path)
        try:
            img = Image.open(path).convert("RGB")
        except Exception as exc:
            self._set_status(f"图片读取失败：{exc}")
            return False
        img.thumbnail((_PREVIEW_MAX_WIDTH, 1 << 20), _RESAMPLE)
        self._preview_photo = ImageTk.PhotoImage(img)
        self._preview_label.config(image=self._preview_photo, text="")
        self._set_status("图片已加载，请输入问题后点击“提问”")
        return True

    # ---------- 问题输入框占位提示 ----------

    def _on_question_focus_in(self, _event):
        """获得焦点时清除占位提示。"""
        if self._question_entry.get() == self._placeholder:
            self._question_entry.delete(0, "end")
            self._question_entry.config(foreground="#000000")

    def _on_question_focus_out(self, _event):
        """失去焦点且为空时恢复占位提示。"""
        if not self._question_entry.get().strip():
            self._question_entry.insert(0, self._placeholder)
            self._question_entry.config(foreground="#808080")

    def _current_question(self):
        """取用户输入的问题（占位提示视为空）。"""
        text = self._question_entry.get().strip()
        return "" if text == self._placeholder else text

    # ---------- 提问与识别 ----------

    def _on_ask(self):
        """校验输入后启动识别子线程。"""
        if self._worker_running:
            self._set_status("识别正在进行，请稍候...")
            return
        if not self._image_path:
            self._set_status("请先选择图片")
            return
        question = self._current_question()
        if not question:
            self._set_status("请输入问题")
            return

        species_name = self._registry_of(self._species_var.get(), self._species_options)
        part_name = self._registry_of(self._part_var.get(), self._part_options)
        marker = self._marker_var.get()

        self._ask_button.config(state="disabled")
        self._set_status("正在识别，请稍候...")
        self._worker_running = True
        worker = threading.Thread(
            target=self._ask_worker,
            args=(self._image_path, question, marker, species_name, part_name),
            daemon=True,
        )
        worker.start()

    def _ask_worker(self, image_path, question, marker, species_name, part_name):
        """识别子线程：调用问答引擎，异常兜底转中文提示。"""
        try:
            if self._service is not None:
                # 服务模式：问答整体在训练环境子进程执行，按 JSON 协议取结果
                resp = self._service.answer(
                    image_path, question, marker, species_name, part_name)
                if not resp.get("ok"):
                    raise RuntimeError(resp.get("error") or resp.get("message")
                                       or "识别失败")
                answer = _ServiceAnswer(resp)
            else:
                answer = vqa_engine.answer_question(
                    image_path=image_path,
                    question=question,
                    marker_style=marker,
                    species_model=species_name,
                    part_model=part_name,
                )
        except Exception as exc:
            self.after(0, self._on_ask_error, f"识别出错：{exc}")
            return
        self.after(0, self._on_ask_done, answer)

    def _on_ask_done(self, answer):
        """识别完成：回到主线程刷新文本区、结果图与状态栏。"""
        self._worker_running = False
        self._ask_button.config(state="normal")
        self._result_text.delete("1.0", "end")
        self._result_text.insert("1.0", answer.message)
        if answer.marked_image_path:
            self._show_result_image(answer.marked_image_path)
            self._set_status("识别完成")
        else:
            self._set_status("识别完成（未生成标记图）")

    def _on_ask_error(self, message):
        """识别异常：文本区显示中文错误并恢复按钮。"""
        self._worker_running = False
        self._ask_button.config(state="normal")
        self._result_text.delete("1.0", "end")
        self._result_text.insert("1.0", message)
        self._set_status("识别失败")

    def _show_result_image(self, path):
        """在结果区显示标记图（等比缩放至宽 480）。"""
        try:
            img = Image.open(path).convert("RGB")
        except Exception as exc:
            self._set_status(f"标记图读取失败：{exc}")
            return
        img.thumbnail((_RESULT_MAX_WIDTH, 1 << 20), _RESAMPLE)
        self._result_photo = ImageTk.PhotoImage(img)
        self._result_image_label.config(image=self._result_photo, text="")

    # ---------- 状态 ----------

    def _set_status(self, text):
        """更新状态栏文本。"""
        self._status_var.set(text)


class BirdVqaApp(tk.Tk):
    """独立主窗口包壳：承载 BirdVqaPanel，保持 main.py gui 入口兼容。"""

    def __init__(self):
        super().__init__()
        self.title(config.GUI_WINDOW_TITLE)
        self.minsize(900, 700)

        # 可嵌入面板承载全部界面构建与交互逻辑
        self.panel = BirdVqaPanel(self)
        self.panel.pack(fill="both", expand=True)

        # 窗口级逻辑：关闭时若识别进行中需二次确认
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """关闭窗口：识别进行中需二次确认。"""
        if self.panel.worker_running:
            confirmed = messagebox.askyesno("确认退出", "识别正在进行，确定要退出吗？")
            if not confirmed:
                return
        self.destroy()


def run():
    """启动图形界面主循环。"""
    app = BirdVqaApp()
    app.mainloop()


if __name__ == "__main__":
    run()
