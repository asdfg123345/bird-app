#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

"""鸟类爬虫桌面控制台（Windows 弹窗界面，Tkinter 标准库实现）。

功能：
    1. 下载控制：选择类群 → 一键启动/停止爬虫（子进程方式，不影响原有脚本）；
    2. 拖入文件爬取：选择或监视 txt/csv 文件，自动解析类群 ID/物种名并爬取入库；
    3. 数据浏览：按关键词/类群查询已入库的观察记录，双击跳转 iNaturalist 原页；
    4. 照片浏览：按种类浏览已下载的照片缩略图，双击用系统看图工具打开大图；
    5. 模型训练：选择训练模式与模型基名启动训练子进程，实时显示终端日志；
    6. 模型库：浏览已注册模型、预览缩略图、设为激活模型、导入外部模型；
    7. 视觉分析：嵌入鸟类视觉分析系统的问答面板（首次切到该页时惰性加载）；
    8. 统计与说明：查看数据库总量、各类群分布、批次记录，并展示数据库设计说明。

数据库设计完全遵循《数据库结构提取_爬虫项目复用_20260907.md》：
    - 两层库：raw_data.db（原始留存）+ bird_observations.db（规范化主库）；
    - SHA-256 指纹去重 + UNIQUE 约束 + upsert（已在 inaturalist_db_manager.py 实现）；
    - 照片按种类分文件夹存储：data/taxon_<id>_<名称>/photos/。
"""

from __future__ import annotations


# ============================================================
# 冻结环境适配：打包为 exe 后，数据目录固定到 exe 所在目录
# ============================================================
def _frozen_base(src_file):
    """打包后返回 exe 所在目录；源码运行返回脚本所在目录。"""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(src_file).resolve().parent


import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

# 同目录共享模型库模块（注册表/训练模式预设/预览图；无 torch 依赖，可安全导入）
import model_library as ml

# 路径均相对脚本目录解析，避免硬编码
BASE_DIR = _frozen_base(__file__)
DATA_DIR = BASE_DIR / "data"
DB_DIR = DATA_DIR / "db"
DEFAULT_RAW_DB = DB_DIR / "raw_data.db"
DEFAULT_MAIN_DB = DB_DIR / "bird_observations.db"

# 模型训练默认使用的 Python 解释器（训练需 torch/torchvision，GUI 默认解释器不一定具备）。
# 默认优先用 anaconda 的「框架一_GPU」conda 环境；可用 --train-python 覆盖。
DEFAULT_TRAIN_PYTHON = Path(
    r"C:\Users\MECHREVO\anaconda3\envs\框架一_GPU\python.exe")
# 模型训练默认数据根目录（训练数据集压缩版，脚本会自动下钻一层外壳）
DEFAULT_TRAIN_DATA = Path(r"d:\鸟类\鸟类训练数据集_压缩版")
# 训练产物不再使用散装输出目录：统一由训练脚本注册进模型库
# （模型库根目录见 model_library.MODEL_LIBRARY_DIR，即 data/model_library）

# Pillow 为可选依赖：存在则显示缩略图，否则显示文件名按钮（不强制安装）
try:
    from PIL import Image, ImageTk  # type: ignore
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# 默认鸟类类群（与 batch_download_birds.py 的 DEFAULT_TAXA 保持一致）
DEFAULT_TAXA = [
    (574, "雉科"), (6912, "鸭科"), (5067, "鹰科"), (19728, "鸱鸮科"),
    (17599, "啄木鸟科"), (2715, "鸠鸽科"), (1627, "杜鹃科"), (13685, "雀科"),
    (12704, "鹟科"), (15977, "鸫科"), (4929, "鹭科"), (4342, "鸥科"),
    (3835, "鹬科"), (154, "秧鸡科"), (20967, "戴胜科"),
]


# ============================================================
# 鸟类视觉分析系统目录定位（供「视觉分析」页签惰性嵌入使用）
# ============================================================
def _find_vision_system_dir() -> Path | None:
    """定位「鸟类视觉分析系统」项目目录（不硬编码盘符路径）。

    探测顺序：
      1. 模型库设置文件（data/model_library/settings.json）中的
         vision_system_dir 键——要求该目录下存在 gui_app.py；
      2. 安装包内置目录：BASE_DIR\\vision_system（随 v1.3 安装包一起
         释放，含 gui_app.py 即视为命中）；
      3. 项目根目录（BASE_DIR.parent.parent，即鸟类总目录）下 glob
         「鸟类视觉分析系统_*」文件夹，取修改时间最新且含 gui_app.py 的一个。

    第 2、3 步命中后把路径写回设置文件，下次启动直接命中第 1 步；
    均未命中返回 None。
    """
    # 第 1 步：读取设置文件中已记录的视觉系统目录
    try:
        recorded = ml.read_settings().get("vision_system_dir")
    except Exception:            # 设置读取异常按未配置处理，继续走目录探测
        recorded = None
    if recorded:
        p = Path(str(recorded))
        if (p / "gui_app.py").is_file():
            return p
    # 第 2 步：安装包内置目录（随发布单元一起装入，开箱零配置）
    builtin = BASE_DIR / "vision_system"
    if (builtin / "gui_app.py").is_file():
        return builtin
    # 第 3 步：在项目根目录下探测「鸟类视觉分析系统_*」目录
    project_root = BASE_DIR.parent.parent
    try:
        found = [d for d in project_root.glob("鸟类视觉分析系统_*")
                 if d.is_dir() and (d / "gui_app.py").is_file()]
    except OSError:
        found = []               # 目录不可枚举时按未找到处理
    if not found:
        return None
    latest = max(found, key=lambda d: d.stat().st_mtime)  # 取修改时间最新
    # 命中后写回设置文件，下次启动直接命中第 1 步（写回失败不影响本次使用）
    try:
        settings = ml.read_settings()
        settings["vision_system_dir"] = str(latest)
        ml.write_settings(settings)
    except Exception:
        pass
    return latest


class BirdCrawlerGUI:
    """桌面控制台主窗口。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None       # 爬虫子进程
        self.log_queue: queue.Queue = queue.Queue()      # 子进程日志队列
        self.drop_proc: subprocess.Popen | None = None   # 拖入处理子进程
        self.drop_queue: queue.Queue = queue.Queue()     # 拖入处理日志队列
        self.train_proc: subprocess.Popen | None = None  # 模型训练子进程
        self.train_queue: queue.Queue = queue.Queue()    # 模型训练日志队列
        self.train_lock = threading.Lock()               # 训练子进程并发启动锁
        self.train_python_var = tk.StringVar(
            value=str(DEFAULT_TRAIN_PYTHON))             # 训练用 Python 解释器
        self.photo_index: dict | None = None             # 照片文件名索引缓存
        self._thumb_refs: list = []                      # 防止缩略图被垃圾回收
        self._model_preview_ref = None                   # 模型库预览图引用（防垃圾回收）
        self._obs_offset = 0                             # 观察记录分页偏移
        self._ph_offset = 0                              # 照片分页偏移

        root.title("鸟类爬虫控制台 - iNaturalist 数据采集与浏览")
        root.geometry("1080x760")
        root.minsize(960, 660)
        root.option_add("*Font", ("Microsoft YaHei UI", 9))

        self._build_banner()
        self._build_status_bar()
        self._build_notebook()
        self._ensure_databases()
        self._poll_log_queue()
        self._poll_drop_queue()
        self._poll_train_queue()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 启动早期检测 Pillow：缺失时弹窗提供一键安装（可跳过，不影响其余功能）
        if not HAS_PIL:
            root.after(300, self._prompt_pillow_install)

    # ================= 界面构建 =================
    def _build_banner(self) -> None:
        """顶部签名横幅（深色底，展示项目名与作者签名）。"""
        banner = tk.Frame(self.root, bg="#22384e", height=58)
        banner.pack(fill=tk.X)
        banner.pack_propagate(False)
        tk.Label(banner, text="鸟类爬虫控制台", bg="#22384e", fg="#ffffff",
                 font=("Microsoft YaHei UI", 14, "bold")).pack(side=tk.LEFT, padx=(16, 8))
        tk.Label(banner, text="iNaturalist 数据采集与图像下载系统", bg="#22384e",
                 fg="#c4d5e5").pack(side=tk.LEFT, pady=(8, 0))
        tk.Label(banner, text="签名：天咏（明月照今日，风摇过两川。）", bg="#22384e",
                 fg="#8fb0cc", font=("Microsoft YaHei UI", 9, "italic")).pack(
            side=tk.RIGHT, padx=16)

    def _build_status_bar(self) -> None:
        """底部状态栏（数据库路径 + 就绪标识）。"""
        bar = ttk.Frame(self.root, padding=(10, 3))
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(bar, text=f"数据库：{DEFAULT_MAIN_DB}",
                  foreground="#6b7683").pack(side=tk.LEFT)
        ttk.Label(bar, text="就绪", foreground="#6b7683").pack(side=tk.RIGHT)

    def _build_notebook(self) -> None:
        """构建各功能标签页（模型库页插在模型训练页之后）并应用统一样式。"""
        style = ttk.Style(self.root)
        # 优先使用 Windows 原生主题，观感与系统一致
        for theme in ("vista", "xpnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("TNotebook.Tab", padding=(18, 7),
                        font=("Microsoft YaHei UI", 9))
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self._build_download_tab()
        self._build_drop_tab()
        self._build_train_tab()
        self._build_models_tab()
        self._build_vision_tab()
        self._build_browse_tab()
        self._build_photos_tab()
        self._build_stats_tab()

    # ---------- 标签页 1：下载控制 ----------
    def _build_download_tab(self) -> None:
        page = ttk.Frame(self.notebook)
        self.notebook.add(page, text=" 下载控制 ")

        # 左侧：参数区
        left = ttk.Frame(page, padding=8)
        left.pack(side=tk.LEFT, fill=tk.Y)

        # 类群选择列表
        group = ttk.LabelFrame(left, text=" 选择要下载的类群（按住 Ctrl 多选；不选=全部 15 科） ", padding=6)
        group.pack(fill=tk.BOTH, expand=True)
        self.taxon_listbox = tk.Listbox(group, selectmode=tk.EXTENDED,
                                        height=14, exportselection=False)
        for tid, name in DEFAULT_TAXA:
            self.taxon_listbox.insert(tk.END, f"{tid}   {name}")
        self.taxon_listbox.pack(fill=tk.BOTH, expand=True)

        custom = ttk.LabelFrame(left, text=" 或输入自定义类群 ID（逗号分隔，优先于上方选择） ", padding=6)
        custom.pack(fill=tk.X, pady=(6, 0))
        self.custom_taxon_var = tk.StringVar()
        ttk.Entry(custom, textvariable=self.custom_taxon_var).pack(fill=tk.X)

        # 下载参数
        params = ttk.LabelFrame(left, text=" 下载参数 ", padding=6)
        params.pack(fill=tk.X, pady=6)

        row1 = ttk.Frame(params); row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="每类群观察上限:").pack(side=tk.LEFT)
        self.max_var = tk.IntVar(value=200)
        ttk.Spinbox(row1, from_=0, to=100000, textvariable=self.max_var,
                    width=8).pack(side=tk.LEFT, padx=4)
        ttk.Label(row1, text="(0=不限)").pack(side=tk.LEFT)

        row2 = ttk.Frame(params); row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="图片尺寸:").pack(side=tk.LEFT)
        self.size_var = tk.StringVar(value="large")
        ttk.Combobox(row2, textvariable=self.size_var, width=8, state="readonly",
                     values=["large", "medium", "original"]).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="质量等级:").pack(side=tk.LEFT, padx=(10, 0))
        self.grade_var = tk.StringVar(value="research")
        ttk.Combobox(row2, textvariable=self.grade_var, width=10, state="readonly",
                     values=["research", "needs_id", "casual", "any"]).pack(side=tk.LEFT, padx=4)

        row3 = ttk.Frame(params); row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="下载线程:").pack(side=tk.LEFT)
        self.workers_var = tk.IntVar(value=6)
        ttk.Spinbox(row3, from_=1, to=16, textvariable=self.workers_var,
                    width=8).pack(side=tk.LEFT, padx=4)

        # 操作按钮
        btns = ttk.Frame(left); btns.pack(fill=tk.X, pady=4)
        self.start_btn = ttk.Button(btns, text="开始下载", command=self._start_download)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.stop_btn = ttk.Button(btns, text="停止下载", command=self._stop_download,
                                   state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="打开数据目录",
                   command=lambda: os.startfile(str(DATA_DIR))).pack(side=tk.LEFT, padx=6)  # noqa: S606

        # 右侧：运行日志
        right = ttk.Frame(page, padding=(0, 8, 8, 8))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(right, text="运行日志（含去重统计：new=新增，existed=指纹重复跳过）").pack(anchor=tk.W)
        self.log_text = scrolledtext.ScrolledText(right, height=20, state=tk.DISABLED,
                                                  font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=4)

    # ---------- 标签页 2：拖入文件爬取 ----------
    def _build_drop_tab(self) -> None:
        """构建拖入文件爬取标签页（对接 drop_crawl_interface.py 接口）。"""
        page = ttk.Frame(self.notebook)
        self.notebook.add(page, text=" 拖入文件爬取 ")

        # 文件选择行（文本清单与图片均支持，可多选）
        top = ttk.Frame(page, padding=8)
        top.pack(fill=tk.X)
        ttk.Button(top, text="选择 txt/csv/图片文件…", style="Accent.TButton",
                   command=self._choose_drop_file).pack(side=tk.LEFT)
        ttk.Button(top, text="选择图片文件夹…",
                   command=self._choose_drop_folder).pack(side=tk.LEFT, padx=(6, 0))
        self.drop_file_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.drop_file_var, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.drop_start_btn = ttk.Button(top, text="开始处理",
                                         command=self._start_drop_file,
                                         state=tk.DISABLED)
        self.drop_start_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.drop_stop_btn = ttk.Button(top, text="停止", command=self._stop_drop,
                                        state=tk.DISABLED)
        self.drop_stop_btn.pack(side=tk.LEFT, padx=4)

        # 爬取参数
        params = ttk.LabelFrame(page, text=" 爬取参数 ", padding=6)
        params.pack(fill=tk.X, padx=8)
        row = ttk.Frame(params)
        row.pack(fill=tk.X)
        ttk.Label(row, text="每类群上限:").pack(side=tk.LEFT)
        self.drop_max_var = tk.IntVar(value=50)
        ttk.Spinbox(row, from_=1, to=100000, textvariable=self.drop_max_var,
                    width=8).pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="尺寸:").pack(side=tk.LEFT, padx=(10, 0))
        self.drop_size_var = tk.StringVar(value="large")
        ttk.Combobox(row, textvariable=self.drop_size_var, width=8, state="readonly",
                     values=["large", "medium", "original"]).pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="质量:").pack(side=tk.LEFT, padx=(10, 0))
        self.drop_grade_var = tk.StringVar(value="research")
        ttk.Combobox(row, textvariable=self.drop_grade_var, width=10, state="readonly",
                     values=["research", "needs_id", "casual", "any"]).pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="线程:").pack(side=tk.LEFT, padx=(10, 0))
        self.drop_workers_var = tk.IntVar(value=6)
        ttk.Spinbox(row, from_=1, to=16, textvariable=self.drop_workers_var,
                    width=6).pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="统一类群(可选):").pack(side=tk.LEFT, padx=(10, 0))
        self.drop_taxon_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.drop_taxon_var, width=14).pack(
            side=tk.LEFT, padx=4)

        # 常驻监视模式
        watch = ttk.LabelFrame(page, text=" 常驻监视模式 ", padding=6)
        watch.pack(fill=tk.X, padx=8, pady=6)
        self.drop_watch_btn = ttk.Button(watch, text="启动监视 data/drop_inbox",
                                         command=self._toggle_drop_watch)
        self.drop_watch_btn.pack(side=tk.LEFT)
        self.drop_watch_state = ttk.Label(watch, text="未启动", foreground="#8a95a1")
        self.drop_watch_state.pack(side=tk.LEFT, padx=10)
        ttk.Button(watch, text="打开投放目录",
                   command=self._open_drop_inbox).pack(side=tk.LEFT, padx=6)
        ttk.Label(watch, text="监视模式下将 txt/csv 放入投放目录即自动爬取；"
                              "也可直接把文件拖到「拖入文件自动爬取.bat」图标上。",
                  foreground="#8a95a1").pack(side=tk.LEFT, padx=8)

        # 处理报告
        ttk.Label(page, text="处理报告（每行一个类群 ID 或物种名，自动解析）",
                  padding=(10, 4, 0, 0)).pack(anchor=tk.W)
        self.drop_report = scrolledtext.ScrolledText(page, state=tk.DISABLED,
                                                     font=("Consolas", 9))
        self.drop_report.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 8))

    def _choose_drop_file(self) -> None:
        """选择要处理的 txt/csv 清单或图片文件（可多选）。"""
        paths = filedialog.askopenfilenames(
            title="选择清单文件或图片（可多选）",
            filetypes=[("支持的文件", "*.txt;*.csv;*.list;*.jpg;*.jpeg;*.png;*.webp;*.gif"),
                       ("文本清单", "*.txt;*.csv;*.list"),
                       ("图片", "*.jpg;*.jpeg;*.png;*.webp;*.gif"),
                       ("所有文件", "*.*")])
        if paths:
            self._drop_paths = [Path(p) for p in paths]
            self.drop_file_var.set(" ; ".join(p.name for p in self._drop_paths))
            self.drop_start_btn.config(state=tk.NORMAL)

    def _choose_drop_folder(self) -> None:
        """选择要导入的图片文件夹（文件夹名/子文件夹名即物种名或类群 ID）。"""
        path = filedialog.askdirectory(title="选择图片文件夹（按文件夹名解析种类）")
        if path:
            self._drop_paths = [Path(path)]
            self.drop_file_var.set(path)
            self.drop_start_btn.config(state=tk.NORMAL)

    def _drop_common_args(self) -> list[str]:
        """组装拖入接口的通用命令行参数。"""
        extra: list[str] = ["--max-per-taxon", str(self.drop_max_var.get()),
                            "--workers", str(self.drop_workers_var.get()),
                            "--photo-size", self.drop_size_var.get(),
                            "--quality-grade", self.drop_grade_var.get()]
        taxon = self.drop_taxon_var.get().strip()
        if taxon:
            extra += ["--taxon", taxon]
        return extra

    def _spawn_drop_proc(self, extra: list[str]) -> None:
        """启动 drop_crawl_interface 子进程（--file 单文件或 --watch 监视模式）。"""
        if self.drop_proc and self.drop_proc.poll() is None:
            messagebox.showinfo("提示", "拖入处理进程正在运行中，请先停止。")
            return
        cmd = [sys.executable, str(BASE_DIR / "drop_crawl_interface.py"), *extra,
               *self._drop_common_args(), "--inbox", str(DATA_DIR / "drop_inbox")]
        # 子进程用 UTF-8 输出，避免中文报告乱码
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.drop_proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env, cwd=str(BASE_DIR))
        self._append_drop_report(f"\n===== 启动拖入处理：{' '.join(cmd[2:])} =====\n")
        self.drop_stop_btn.config(state=tk.NORMAL)
        threading.Thread(target=self._read_drop_output, daemon=True).start()

    def _start_drop_file(self) -> None:
        """处理选中的清单/图片/文件夹。"""
        paths = getattr(self, "_drop_paths", [])
        if not paths:
            messagebox.showinfo("提示", "请先选择文件或文件夹。")
            return
        self._spawn_drop_proc(["--file", *[str(p) for p in paths]])

    def _toggle_drop_watch(self) -> None:
        """启动/停止常驻监视（按钮文字随状态切换）。"""
        if self.drop_proc and self.drop_proc.poll() is None:
            self._stop_drop()
            return
        self._spawn_drop_proc(["--watch"])
        self.drop_watch_state.config(text="监视中：等待放入文件…", foreground="#1d7a34")
        self.drop_watch_btn.config(text="停止监视")

    def _stop_drop(self) -> None:
        """终止拖入处理子进程。"""
        if self.drop_proc and self.drop_proc.poll() is None:
            self.drop_proc.terminate()
            self._append_drop_report("[用户] 已请求终止拖入处理进程。\n")

    def _open_drop_inbox(self) -> None:
        """打开投放目录（不存在则先创建）。"""
        inbox = DATA_DIR / "drop_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        os.startfile(str(inbox))  # noqa: S606

    def _read_drop_output(self) -> None:
        """后台线程：读取拖入处理子进程输出放入队列。"""
        assert self.drop_proc is not None and self.drop_proc.stdout is not None
        for line in self.drop_proc.stdout:
            self.drop_queue.put(line)
        self.drop_queue.put("\n[拖入处理进程已结束]\n")

    def _poll_drop_queue(self) -> None:
        """主循环定时器：刷新处理报告并在进程结束后复位按钮状态。"""
        try:
            while True:
                line = self.drop_queue.get_nowait()
                self._append_drop_report(line)
                if "[拖入处理进程已结束]" in line:
                    self.drop_stop_btn.config(state=tk.DISABLED)
                    self.drop_watch_btn.config(text="启动监视 data/drop_inbox")
                    self.drop_watch_state.config(text="未启动", foreground="#8a95a1")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_drop_queue)

    def _append_drop_report(self, text: str) -> None:
        """向处理报告框追加文本并滚动到底部。"""
        self.drop_report.config(state=tk.NORMAL)
        self.drop_report.insert(tk.END, text)
        self.drop_report.see(tk.END)
        self.drop_report.config(state=tk.DISABLED)

    # ---------- 标签页 3：模型训练 ----------
    def _build_train_tab(self) -> None:
        """构建模型训练标签页（对接 model_trainer.py，实时显示终端进程）。

        训练页内容较多（环境/模式/参数/划分/指标表/日志），外层套一层纵向滚动
        容器：窗口不够高时可拖动滚动条查看全部区域，避免底部区域被裁掉。
        """
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text=" 模型训练 ")
        canvas = tk.Canvas(outer, highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # 所有原有子控件继续挂在 page（= canvas 内的滚动内容框）上
        page = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=page, anchor="nw")

        def _sync_scrollregion(_event=None) -> None:
            """内容尺寸变化时刷新滚动范围。"""
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _stretch_width(event) -> None:
            """画布宽度变化时让内容框跟随等宽，避免横向留白。"""
            canvas.itemconfigure(window_id, width=event.width)

        page.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _stretch_width)

        def _on_wheel(event) -> None:
            """鼠标滚轮滚动训练页（仅在本页范围内生效）。"""
            canvas.yview_scroll(int(-event.delta / 120), "units")

        outer.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        outer.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        # 环境行：训练用 Python 解释器 + 数据目录
        env = ttk.LabelFrame(page, text=" 训练环境与数据 ", padding=6)
        env.pack(fill=tk.X, padx=8, pady=(8, 0))

        row_env = ttk.Frame(env); row_env.pack(fill=tk.X, pady=2)
        ttk.Label(row_env, text="训练Python:").pack(side=tk.LEFT)
        self.train_python_entry = ttk.Entry(
            row_env, textvariable=self.train_python_var, width=46)
        self.train_python_entry.pack(side=tk.LEFT, padx=4)
        ttk.Button(row_env, text="浏览…",
                   command=self._browse_train_python).pack(side=tk.LEFT)
        ttk.Button(row_env, text="检测依赖",
                   command=self._check_train_deps).pack(side=tk.LEFT, padx=6)
        self.train_dep_label = ttk.Label(row_env, text="", foreground="#8a95a1")
        self.train_dep_label.pack(side=tk.LEFT, padx=8)

        row_data = ttk.Frame(env); row_data.pack(fill=tk.X, pady=2)
        ttk.Label(row_data, text="数据目录:").pack(side=tk.LEFT)
        self.train_data_var = tk.StringVar(value=str(DEFAULT_TRAIN_DATA))
        ttk.Entry(row_data, textvariable=self.train_data_var, width=52).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(row_data, text="浏览…",
                   command=self._browse_train_data).pack(side=tk.LEFT)
        ttk.Button(row_data, text="扫描数据",
                   command=self._scan_train_data).pack(side=tk.LEFT, padx=6)

        # 中断续训：勾选后从最近一次中断的断点继续（含断点信息提示）
        row_resume = ttk.Frame(env); row_resume.pack(fill=tk.X, pady=2)
        self.train_resume_var = tk.BooleanVar(value=False)
        self.train_resume_check = ttk.Checkbutton(
            row_resume, text="从中断处续训", variable=self.train_resume_var,
            command=self._on_resume_toggled)
        self.train_resume_check.pack(side=tk.LEFT)
        self.train_resume_label = ttk.Label(row_resume, text="",
                                            foreground="#8a95a1")
        self.train_resume_label.pack(side=tk.LEFT, padx=8)
        self._refresh_resume_state()

        # 训练模式：四个单选（预设数据来自 model_library，与训练脚本共用）
        mode = ttk.LabelFrame(page, text=" 训练模式 ", padding=6)
        mode.pack(fill=tk.X, padx=8, pady=6)
        self.train_mode_var = tk.StringVar(value="adaptive")
        row_mode = ttk.Frame(mode); row_mode.pack(fill=tk.X)
        for mode_key in ("adaptive", "low", "high", "custom"):
            ttk.Radiobutton(
                row_mode, text=ml.TRAINING_MODE_LABELS[mode_key],
                value=mode_key, variable=self.train_mode_var,
                command=self._on_train_mode_changed).pack(side=tk.LEFT, padx=(0, 14))

        # 基础模型：训练起点下拉（内置预置 + 模型库已训练模型），选中后自动带出模型基名
        row_foundation = ttk.Frame(mode); row_foundation.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row_foundation, text="基础模型:").pack(side=tk.LEFT)
        self.train_base_model_var = tk.StringVar()
        self.train_base_model_combo = ttk.Combobox(
            row_foundation, textvariable=self.train_base_model_var,
            state="readonly", width=52)
        self.train_base_model_combo.pack(side=tk.LEFT, padx=4)
        self.train_base_model_combo.bind(
            "<<ComboboxSelected>>", self._on_base_model_selected)
        ttk.Label(row_foundation,
                  text="（训练起点；选模型库模型即以其权重继续微调）",
                  foreground="#8a95a1").pack(side=tk.LEFT)
        self._base_model_id = "resnet50"     # 当前选中的基础模型 id
        self._refresh_base_model_options()

        # 训练策略说明：随所选基础模型的架构联动刷新（只读提示）
        self.train_strategy_label = ttk.Label(
            mode, text="", foreground="#2f6f4f", wraplength=780, justify=tk.LEFT)
        self.train_strategy_label.pack(anchor=tk.W, pady=(2, 0))
        self._update_train_strategy_label()

        # 模型基名：用于训练产物命名（基名_月日_版本）
        row_base = ttk.Frame(mode); row_base.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row_base, text="模型基名:").pack(side=tk.LEFT)
        self.train_model_base_var = tk.StringVar(value="resnet50")
        ttk.Entry(row_base, textvariable=self.train_model_base_var,
                  width=18).pack(side=tk.LEFT, padx=4)
        ttk.Label(row_base, text="（用于训练产物命名：基名_月日_版本）",
                  foreground="#8a95a1").pack(side=tk.LEFT)

        # 模式说明区：只读文本，随单选切换即时刷新
        self.train_mode_desc = tk.Text(
            mode, height=6, wrap="word", relief="flat", bd=0,
            bg="#f4f6f9", font=("Microsoft YaHei UI", 9), state=tk.DISABLED)
        self.train_mode_desc.pack(fill=tk.X, pady=(4, 0))
        self.train_mode_desc.tag_configure(
            "mode_hl", foreground="#b04818",
            font=("Microsoft YaHei UI", 9, "bold"))

        # 训练参数：自定义模式专属参数 + 通用线程数
        params = ttk.LabelFrame(
            page, text=" 训练参数（轮数/优化器等仅自定义模式生效） ", padding=6)
        params.pack(fill=tk.X, padx=8, pady=6)

        # 自定义参数行：选中「自定义模式」时启用，其余模式置灰禁用
        self.train_epochs_var = tk.IntVar(value=15)
        self.train_optimizer_var = tk.StringVar(value="adamw")
        self.train_patience_var = tk.IntVar(value=6)
        self.train_batch_var = tk.IntVar(value=32)
        self.train_lr_var = tk.StringVar(value="1e-3")
        row_custom = ttk.Frame(params); row_custom.pack(fill=tk.X, pady=2)
        ttk.Label(row_custom, text="轮数:").pack(side=tk.LEFT)
        sb_epochs = ttk.Spinbox(row_custom, from_=1, to=200,
                                textvariable=self.train_epochs_var, width=6)
        sb_epochs.pack(side=tk.LEFT, padx=2)
        ttk.Label(row_custom, text="优化器:").pack(side=tk.LEFT, padx=(10, 0))
        cb_optimizer = ttk.Combobox(row_custom, textvariable=self.train_optimizer_var,
                                    width=7, state="readonly",
                                    values=["adamw", "adam", "sgd"])
        cb_optimizer.pack(side=tk.LEFT, padx=2)
        ttk.Label(row_custom, text="耐心值:").pack(side=tk.LEFT, padx=(10, 0))
        sb_patience = ttk.Spinbox(row_custom, from_=0, to=100,
                                  textvariable=self.train_patience_var, width=5)
        sb_patience.pack(side=tk.LEFT, padx=2)
        ttk.Label(row_custom, text="批大小:").pack(side=tk.LEFT, padx=(10, 0))
        sb_batch = ttk.Spinbox(row_custom, from_=1, to=256,
                               textvariable=self.train_batch_var, width=6)
        sb_batch.pack(side=tk.LEFT, padx=2)
        ttk.Label(row_custom, text="学习率:").pack(side=tk.LEFT, padx=(10, 0))
        en_lr = ttk.Entry(row_custom, textvariable=self.train_lr_var, width=8)
        en_lr.pack(side=tk.LEFT, padx=2)
        # 混合精度（AMP）开关：仅自定义模式可手动开关，其余模式按预设固定
        self.train_amp_var = tk.BooleanVar(value=False)
        self.cb_amp = ttk.Checkbutton(row_custom, text="混合精度(AMP)",
                                      variable=self.train_amp_var)
        self.cb_amp.pack(side=tk.LEFT, padx=(10, 0))
        # 记录自定义参数控件，便于随模式切换统一启用/禁用
        self._custom_param_widgets = [sb_epochs, cb_optimizer,
                                      sb_patience, sb_batch, en_lr, self.cb_amp]

        # 线程数（与模式无关，始终可用）
        # Windows 下多线程 DataLoader 易出问题，默认 0（主进程加载，最稳）
        self.train_workers_var = tk.IntVar(value=0)
        row_workers = ttk.Frame(params); row_workers.pack(fill=tk.X, pady=2)
        ttk.Label(row_workers, text="线程:").pack(side=tk.LEFT)
        ttk.Spinbox(row_workers, from_=0, to=16, textvariable=self.train_workers_var,
                    width=5).pack(side=tk.LEFT, padx=2)
        ttk.Label(row_workers, text="0=自动(按模式推导)；1-16=固定加载进程数",
                  foreground="#8a95a1").pack(side=tk.LEFT, padx=10)

        # 保存位置提示：训练产物统一走模型库入库流程，不再自选输出目录
        ttk.Label(params, text=f"模型将保存至模型库：{ml.MODEL_LIBRARY_DIR}",
                  foreground="#8a95a1").pack(anchor=tk.W, pady=(2, 0))

        # 数据划分与评估：4 级划分预设（含自定义）+ 最佳模型判据
        divide = ttk.LabelFrame(page, text=" 数据划分与评估 ", padding=6)
        divide.pack(fill=tk.X, padx=8, pady=(0, 6))

        row_split = ttk.Frame(divide); row_split.pack(fill=tk.X, pady=2)
        ttk.Label(row_split, text="划分方式:").pack(side=tk.LEFT)
        self._split_label_to_key = {v: k for k, v in ml.SPLIT_PRESET_LABELS.items()}
        self.train_split_var = tk.StringVar(
            value=ml.SPLIT_PRESET_LABELS["existing"])
        ttk.Combobox(row_split, textvariable=self.train_split_var,
                     state="readonly", width=26,
                     values=list(ml.SPLIT_PRESET_LABELS.values())).pack(
            side=tk.LEFT, padx=4)
        ttk.Label(row_split, text="训练:验证:检测 =",
                  foreground="#8a95a1").pack(side=tk.LEFT, padx=(6, 2))
        self.train_split_ratio_var = tk.StringVar(value="8,1,1")
        self.train_split_ratio_entry = ttk.Entry(
            row_split, textvariable=self.train_split_ratio_var, width=12)
        self.train_split_ratio_entry.pack(side=tk.LEFT)
        ttk.Label(row_split, text="（选「使用目录既有划分」时忽略比例）",
                  foreground="#8a95a1").pack(side=tk.LEFT, padx=6)
        # 类别极少的场合自动保底（小类全进训练集/不进检测集），无需手工调整

        row_metric = ttk.Frame(divide); row_metric.pack(fill=tk.X, pady=2)
        ttk.Label(row_metric, text="最佳模型判据:").pack(side=tk.LEFT)
        self._metric_label_to_key = {
            v: k for k, v in ml.SELECT_METRIC_OPTIONS.items()}
        self.train_metric_var = tk.StringVar(
            value=ml.SELECT_METRIC_OPTIONS["top1_acc"])
        ttk.Combobox(row_metric, textvariable=self.train_metric_var,
                     state="readonly", width=24,
                     values=list(ml.SELECT_METRIC_OPTIONS.values())).pack(
            side=tk.LEFT, padx=4)
        ttk.Label(row_metric, text="（按该指标保存最佳模型并驱动早停）",
                  foreground="#8a95a1").pack(side=tk.LEFT, padx=6)

        # 初始化说明文字与自定义参数可用状态
        self._on_train_mode_changed()

        # 操作按钮
        btns = ttk.Frame(page, padding=(8, 0))
        btns.pack(fill=tk.X)
        self.train_start_btn = ttk.Button(btns, text="开始训练",
                                          command=self._start_train,
                                          style="Accent.TButton")
        self.train_start_btn.pack(side=tk.LEFT)
        self.train_stop_btn = ttk.Button(btns, text="停止训练",
                                         command=self._stop_train,
                                         state=tk.DISABLED)
        self.train_stop_btn.pack(side=tk.LEFT, padx=6)
        self.train_open_out_btn = ttk.Button(btns, text="打开模型库目录",
                                             command=self._open_train_out,
                                             state=tk.DISABLED)
        self.train_open_out_btn.pack(side=tk.LEFT, padx=6)
        self.train_log_btn = ttk.Button(btns, text="打开日志文件",
                                        command=self._open_train_log,
                                        state=tk.DISABLED)
        self.train_log_btn.pack(side=tk.LEFT, padx=6)
        self._train_log_path = None   # 当前训练日志文件路径（结束后定位）

        # 每轮验证指标表：结构化展示（解析训练子进程的 [METRIC] 行，实时刷新）
        ttk.Label(page, text="每轮验证指标（实时解析训练进程输出）",
                  padding=(10, 6, 0, 0)).pack(anchor=tk.W)
        metric_cols = ("epoch", "train_loss", "val_loss",
                       "top1", "top5", "macro_f1", "best")
        metric_heads = ("轮次", "训练损失", "验证损失", "Top-1", "Top-5", "宏F1", "最佳")
        metric_widths = (60, 90, 90, 80, 80, 80, 60)
        metric_wrap = ttk.Frame(page)
        metric_wrap.pack(fill=tk.X, padx=8)
        self.train_metric_tree = ttk.Treeview(
            metric_wrap, columns=metric_cols, show="headings", height=6)
        for col, head, w in zip(metric_cols, metric_heads, metric_widths):
            self.train_metric_tree.heading(col, text=head)
            self.train_metric_tree.column(col, width=w, anchor=tk.CENTER)
        metric_scroll = ttk.Scrollbar(metric_wrap, orient=tk.VERTICAL,
                                      command=self.train_metric_tree.yview)
        self.train_metric_tree.configure(yscrollcommand=metric_scroll.set)
        self.train_metric_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        metric_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        # 最佳轮次行高亮
        self.train_metric_tree.tag_configure("best_row", background="#eaf7ee",
                                             foreground="#1d6b3a")

        # 训练进程输出（终端日志实时显示）；在滚动容器内用固定行高保证可见
        ttk.Label(page, text="训练进程输出（实时显示终端日志）",
                  padding=(10, 6, 0, 0)).pack(anchor=tk.W)
        self.train_log = scrolledtext.ScrolledText(page, state=tk.DISABLED,
                                                   font=("Consolas", 9),
                                                   bg="#101418", fg="#d4e2d4",
                                                   height=14)
        self.train_log.pack(fill=tk.X, padx=8, pady=(2, 10))

    def _on_train_mode_changed(self) -> None:
        """训练模式切换回调：刷新说明区文字并切换自定义参数可用状态。"""
        self._update_mode_description()
        self._apply_custom_param_state()

    def _refresh_base_model_options(self) -> None:
        """重建基础模型下拉选项：内置预置在前，模型库已训练模型按时间倒序在后。

        模型库读取失败时仅保留内置两项，不影响训练界面可用性。
        调用时机：训练页初始化、训练正常结束（新模型入库后）。
        """
        options = [("resnet50", "ResNet50（ImageNet 预训练）[内置预置]"),
                   ("vit_bird_525", "525类鸟类ViT（本地预置）[内置预置]")]
        try:
            records = ml.list_models()
        except Exception:
            records = []
        for record in records:
            name = str(record.get("name") or "")
            if not name:
                continue
            # 指标文本按记录的主指标名格式化（旧记录默认 Top-1）
            metric_text = ml.format_metric_value(record.get("metric_name"),
                                                 record.get("accuracy"))
            acc_text = f"（{metric_text}）" if metric_text != "—" else ""
            # 仅入库管理的架构不能作为训练起点，直接标注避免误选
            architecture = str(record.get("architecture") or "resnet50")
            trainable = architecture in ("resnet50", "vit_bird_525",
                                         "vit_hf", "resnest101")
            suffix = "「模型库」" if trainable else "「模型库·不可作训练起点」"
            options.append((name, f"{name} {acc_text}{suffix}"))
        self._base_model_options = options
        combo = getattr(self, "train_base_model_combo", None)
        if combo is None:
            return
        combo["values"] = [display for _, display in options]
        current = getattr(self, "_base_model_id", "resnet50")
        matched = [display for model_id, display in options if model_id == current]
        if not matched:      # 原选中项已被删除（如库内模型被删）：回退内置 ResNet50
            current = "resnet50"
            matched = [options[0][1]]
        self._base_model_id = current
        self.train_base_model_var.set(matched[0])
        self._update_train_strategy_label()

    def _on_base_model_selected(self, _event=None) -> None:
        """基础模型选中回调：记录 id，并把模型基名自动填为其基名（用户可再手改）。"""
        display = self.train_base_model_var.get()
        for model_id, disp in getattr(self, "_base_model_options", []):
            if disp == display:
                self._base_model_id = model_id
                break
        else:
            return
        try:
            record = ml.get_model(model_id)   # 内置条目也能查到
        except Exception:
            record = None
        base_name = (record or {}).get("base_name") or ""
        if base_name:
            self.train_model_base_var.set(base_name)
        self._update_train_strategy_label()

    def _update_train_strategy_label(self) -> None:
        """按所选基础模型的架构刷新「训练策略」说明文字。"""
        label = getattr(self, "train_strategy_label", None)
        if label is None:
            return
        model_id = getattr(self, "_base_model_id", "resnet50")
        architecture = "resnet50"
        if model_id == "vit_bird_525":
            architecture = "vit_bird_525"
        else:
            try:
                record = ml.get_model(model_id)
            except Exception:
                record = None
            if record:
                architecture = str(record.get("architecture") or "resnet50")
        label.config(text=f"训练策略：{ml.get_strategy_summary(architecture)}")

    # ---------- 中断续训 ----------
    def _load_last_interrupted(self) -> dict | None:
        """读取模型库目录下的中断提示文件（断点目录缺失或无效时返回 None）。"""
        path = ml.MODEL_LIBRARY_DIR / "last_interrupted.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        # 断点目录必须仍存在且含 resume_state.pth，否则视为无效记录
        out_dir = Path(str(data.get("out_dir") or ""))
        if not (out_dir / "resume_state.pth").is_file():
            return None
        return data

    def _refresh_resume_state(self) -> None:
        """刷新「从中断处续训」的可用状态与断点信息提示。"""
        check = getattr(self, "train_resume_check", None)
        label = getattr(self, "train_resume_label", None)
        if check is None or label is None:
            return
        info = self._load_last_interrupted()
        if info:
            check.config(state=tk.NORMAL)
            label.config(
                text=f"检测到断点：{info.get('model_name') or '—'}"
                     f"（已完成 {info.get('completed_epoch')} 轮，"
                     f"{info.get('timestamp') or '—'}）",
                foreground="#b06a00")
        else:
            check.config(state=tk.DISABLED)
            if self.train_resume_var.get():
                self.train_resume_var.set(False)
            label.config(text="暂无中断记录", foreground="#8a95a1")

    def _on_resume_toggled(self) -> None:
        """续训勾选回调：展示断点信息并确认续训语义。"""
        if not self.train_resume_var.get():
            return
        info = self._load_last_interrupted()
        if not info:
            self.train_resume_var.set(False)
            messagebox.showinfo("提示", "无可用的中断记录（需先有一次被中断的训练）。")
            return
        messagebox.showinfo(
            "续训确认",
            f"将从断点继续训练：\n{info.get('out_dir')}\n"
            f"已完成 {info.get('completed_epoch')} 轮，"
            f"最佳 {info.get('metric_name')} = {info.get('best_metric')}\n\n"
            f"续训会沿用断点中的基础模型、数据划分与随机种子，"
            f"「轮数」表示总目标轮数。")

    def _open_train_log(self) -> None:
        """用系统默认程序打开当前训练的日志文件。"""
        path = getattr(self, "_train_log_path", None)
        if not path or not Path(path).is_file():
            messagebox.showinfo("提示", "暂无训练日志文件（先启动一次训练）。")
            return
        try:
            os.startfile(str(path))  # noqa: S606
        except OSError as exc:
            messagebox.showerror("错误", f"打开日志文件失败：{exc}")

    # ---------- 每轮验证指标表 ----------
    def _clear_metric_table(self) -> None:
        """清空每轮验证指标表。"""
        tree = getattr(self, "train_metric_tree", None)
        if tree is not None:
            tree.delete(*tree.get_children())

    def _append_metric_row(self, payload: dict) -> None:
        """把一条 [METRIC] 结构化指标追加到每轮验证指标表。"""
        tree = getattr(self, "train_metric_tree", None)
        if tree is None:
            return

        def fmt(key: str) -> str:
            value = payload.get(key)
            return f"{value:.4f}" if isinstance(value, (int, float)) else "—"

        is_best = bool(payload.get("is_best"))
        values = (payload.get("epoch", "—"), fmt("train_loss"), fmt("val_loss"),
                  fmt("top1"), fmt("top5"), fmt("macro_f1"),
                  "★" if is_best else "")
        tags = ("best_row",) if is_best else ()
        tree.insert("", tk.END, values=values, tags=tags)
        children = tree.get_children()
        if children:
            tree.see(children[-1])

    def _replay_metric_history(self, out_dir: Path) -> None:
        """续训时回放断点目录中的 training_history.json 到指标表。"""
        hist_path = out_dir / "training_history.json"
        if not hist_path.is_file():
            return
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in history if isinstance(history, list) else []:
            if not isinstance(item, dict):
                continue
            self._append_metric_row({
                "epoch": item.get("epoch"),
                "train_loss": item.get("train_loss"),
                "val_loss": item.get("val_loss"),
                "top1": item.get("top1_acc"),
                "top5": item.get("top5_acc"),
                "macro_f1": item.get("macro_f1"),
                "is_best": item.get("is_best"),
            })

    def _update_mode_description(self) -> None:
        """把当前模式的预设信息（轮数/优化器/耐心值/批大小/学习率/说明）写入说明区。"""
        key = self.train_mode_var.get()
        preset = ml.TRAINING_MODE_PRESETS.get(key, {})
        if key == "custom":
            # 自定义模式：预设不提供固定值，提示在下方参数区填写
            epochs_text = "自定义（在下方填写）"
            patience_text = "自定义（在下方填写）"
            batch_text = "自定义（在下方填写）"
        else:
            epochs_text = str(preset.get("epochs", "—"))
            patience_text = str(preset.get("patience", "—"))
            batch_value = preset.get("batch")
            # batch 为 None 表示按设备自适应（由训练脚本推导）
            batch_text = "按设备自适应" if batch_value is None else str(batch_value)
        lr_value = preset.get("lr")
        lr_text = f"{lr_value:g}" if isinstance(lr_value, (int, float)) else "—"
        # 混合精度显示：True=固定开启；auto=有 GPU 时自动开启；False=关闭
        amp_value = preset.get("amp")
        if amp_value is True:
            amp_text = "开启"
        elif amp_value == "auto":
            amp_text = "有 GPU 时自动开启"
        else:
            amp_text = "关闭（自定义模式可勾选）" if key == "custom" else "关闭"
        workers_text = preset.get("workers_hint", "—")
        lines = [
            f"轮数：{epochs_text}",
            f"优化器：{preset.get('optimizer', '—')}",
            f"耐心值：{patience_text}",
            f"批大小：{batch_text}",
            f"学习率：{lr_text}",
            f"混合精度：{amp_text}",
            f"数据加载线程：{workers_text}",
            f"说明：{preset.get('description', '')}",
        ]
        box = self.train_mode_desc
        box.config(state=tk.NORMAL)
        box.delete("1.0", tk.END)
        box.insert(tk.END, "\n".join(lines))
        # 适应模式：突出「安全优先，效果优先，允许以时间换效果」
        if key == "adaptive":
            phrase = "安全优先，效果优先，允许以时间换效果"
            start = box.search(phrase, "1.0", tk.END)
            if start:
                box.tag_add("mode_hl", start, f"{start}+{len(phrase)}c")
        box.config(state=tk.DISABLED)

    def _apply_custom_param_state(self) -> None:
        """自定义模式时启用参数输入，其余模式置灰禁用。"""
        state = tk.NORMAL if self.train_mode_var.get() == "custom" else tk.DISABLED
        for widget in self._custom_param_widgets:
            try:
                widget.config(state=state)
            except tk.TclError:
                pass  # 控件已销毁时忽略

    def _browse_train_python(self) -> None:
        """选择训练用 Python 解释器。"""
        path = filedialog.askopenfilename(
            title="选择训练用 python.exe",
            filetypes=[("Python 解释器", "python.exe"), ("可执行文件", "*.exe"),
                       ("所有文件", "*.*")])
        if path:
            self.train_python_var.set(path)

    def _browse_train_data(self) -> None:
        """选择训练数据根目录。"""
        path = filedialog.askdirectory(title="选择数据目录（含 train/val/test）")
        if path:
            self.train_data_var.set(path)

    def _train_python(self) -> str:
        """取训练解释器路径；默认不存在时回退到当前解释器。"""
        p = self.train_python_var.get().strip()
        if p and Path(p).exists():
            return p
        return sys.executable

    def _check_train_deps(self) -> None:
        """检测训练依赖（torch/torchvision/PIL）是否就绪，结果显示在标签上。"""
        py = self._train_python()
        code = (
            "import importlib.util as u\n"
            "for m in ['torch', 'torchvision', 'PIL']:\n"
            "    print('DEP', m, bool(u.find_spec(m)))\n")
        try:
            out = subprocess.run(
                [py, "-c", code], capture_output=True, text=True,
                timeout=30, encoding="utf-8", errors="replace")
            deps = {}
            for l in (out.stdout or "").splitlines():
                parts = l.split()
                if len(parts) >= 3 and parts[0] == "DEP":
                    deps[parts[1]] = parts[2]
            if deps and all(v == "True" for v in deps.values()):
                self.train_dep_label.config(
                    text="依赖就绪（torch/torchvision/PIL）", foreground="#1d7a34")
            else:
                miss = [k for k, v in deps.items() if v != "True"]
                if not miss and out.returncode != 0:
                    self.train_dep_label.config(
                        text="解释器不可用或无输出", foreground="#b03030")
                else:
                    self.train_dep_label.config(
                        text="缺少: " + ",".join(miss), foreground="#b03030")
        except Exception as exc:
            self.train_dep_label.config(text=f"检测失败: {exc}", foreground="#b03030")

    def _scan_train_data(self) -> None:
        """扫描数据目录并打印类别/张数统计到训练日志。"""
        data_dir = self.train_data_var.get().strip()
        if not data_dir or not Path(data_dir).exists():
            messagebox.showinfo("提示", "请先选择有效的数据目录。")
            return
        cmd = [self._train_python(), str(BASE_DIR / "model_trainer.py"),
               "--data-dir", data_dir, "--list-only"]
        self._run_train_subprocess(cmd, label="扫描数据")

    def _start_train(self) -> None:
        """启动模型训练子进程（model_trainer.py），实时捕获终端输出。

        启动参数按训练模式组装：--mode <模式> --model-name <基名>；
        自定义模式额外追加轮数/优化器/耐心值/批大小/学习率（先校验合法性）。
        训练产物由训练脚本统一注册进模型库，因此不再传递 --out-dir。
        """
        if self.train_proc and self.train_proc.poll() is None:
            messagebox.showinfo("提示", "训练进程正在运行中。")
            return
        data_dir = self.train_data_var.get().strip()
        if not data_dir or not Path(data_dir).exists():
            messagebox.showerror("错误", "训练数据目录无效，请先选择。")
            return
        base_name = self.train_model_base_var.get().strip()
        if not base_name:
            messagebox.showerror("错误", "请填写模型基名（用于训练产物命名）。")
            return
        # 基名仅允许字母/数字/下划线/连字符，避免破坏「基名_月日_版本」命名规则
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", base_name):
            messagebox.showerror("错误", "模型基名只能包含字母、数字、下划线或连字符。")
            return
        mode = self.train_mode_var.get()
        # 数据划分方式：既有划分（默认）或按比例自动划分（4 级预设 + 自定义）
        split_label = self.train_split_var.get()
        split_key = self._split_label_to_key.get(split_label, "existing")
        ratio_text = self.train_split_ratio_var.get().strip()
        use_auto_split = split_key != "existing"
        if use_auto_split:
            preset_ratio = ml.SPLIT_PRESETS.get(split_key, {}).get("ratio")
            if split_key == "custom":
                try:
                    parts = [float(x) for x in ratio_text.replace("：", ":").split(",")]
                except ValueError:
                    messagebox.showerror("错误", "自定义比例需为三个数字，如 8,1,1。")
                    return
                if len(parts) != 3 or any(v < 0 for v in parts) or parts[0] <= 0 \
                        or sum(parts) <= 0:
                    messagebox.showerror(
                        "错误", "自定义比例需为三个非负数且训练比例大于 0（如 8,1,1）。")
                    return
                ratio_arg = ",".join(f"{v:g}" for v in parts)
            else:
                # 预设比例：同步显示到输入框，便于用户查看/微调
                ratio_arg = ",".join(f"{v:g}" for v in (preset_ratio or (8, 1, 1)))
                self.train_split_ratio_var.set(ratio_arg)
        # 最佳模型判据（默认 Top-1）
        metric_key = self._metric_label_to_key.get(self.train_metric_var.get(), None)
        # 续训：勾选时改用 --resume，指向断点目录（不再新开版本号）
        resume_dir = None
        if self.train_resume_var.get():
            info = self._load_last_interrupted()
            if not info:
                messagebox.showerror("错误", "无可用的中断记录，无法续训。")
                return
            resume_dir = str(info.get("out_dir") or "")

        cmd = [self._train_python(), str(BASE_DIR / "model_trainer.py"),
               "--data-dir", data_dir,
               "--base-model", getattr(self, "_base_model_id", "resnet50"),
               "--workers", str(self.train_workers_var.get())]
        if resume_dir:
            # 续训：写入同一断点目录；--epochs 表示总目标轮数
            cmd += ["--resume", resume_dir]
            self._replay_metric_history(Path(resume_dir))
            self._train_log_path = Path(resume_dir) / "train_log.txt"
            if getattr(self, "train_log_btn", None) is not None:
                self.train_log_btn.config(state=tk.NORMAL)
        else:
            # 全新训练：模式化入库流程（自动取号），并清空指标表
            cmd += ["--mode", mode, "--model-name", base_name]
            self._clear_metric_table()
        if use_auto_split:
            cmd += ["--auto-split", "--split-ratio", ratio_arg]
        if metric_key:
            cmd += ["--select-metric", metric_key]
        if mode == "custom" or resume_dir:
            # 自定义模式：校验并传入轮数/优化器/耐心值/批大小/学习率
            # 续训：只传轮数（总目标轮数），其余参数从断点快照恢复
            try:
                epochs = int(self.train_epochs_var.get())
                patience = int(self.train_patience_var.get())
                batch = int(self.train_batch_var.get())
                lr = float(self.train_lr_var.get())
            except (ValueError, tk.TclError):
                messagebox.showerror("错误", "自定义参数必须是有效数字（学习率如 1e-3）。")
                return
            if epochs < 1:
                messagebox.showerror("错误", "轮数必须 ≥ 1。")
                return
            if patience < 0:
                messagebox.showerror("错误", "耐心值必须 ≥ 0。")
                return
            if batch < 1:
                messagebox.showerror("错误", "批大小必须 ≥ 1。")
                return
            if lr <= 0:
                messagebox.showerror("错误", "学习率必须大于 0。")
                return
            cmd += ["--epochs", str(epochs)]
            if not resume_dir:
                cmd += ["--optimizer", self.train_optimizer_var.get(),
                        "--patience", str(patience),
                        "--batch-size", str(batch),
                        "--lr", str(lr)]
        # 自定义模式：勾选混合精度时显式传 --amp（其余模式按预设固定，不由 GUI 传参）
        if mode == "custom" and self.train_amp_var.get():
            cmd += ["--amp"]
        self._run_train_subprocess(cmd, label="续训" if resume_dir else "开始训练")

    def _run_train_subprocess(self, cmd: list[str], label: str) -> None:
        """启动训练相关子进程并接管道，后台线程把输出放入队列。

        加锁防止扫描/训练按钮被连点导致多个训练子进程并发启动。
        """
        with self.train_lock:
            if self.train_proc and self.train_proc.poll() is None:
                messagebox.showinfo("提示", "训练/扫描进程正在运行中，请先停止或等待结束。")
                return
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            try:
                self.train_proc = subprocess.Popen(  # noqa: S603
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", env=env, cwd=str(BASE_DIR))
            except OSError as exc:
                self._append_train_log(f"[错误] 无法启动训练进程: {exc}\n")
                return
            self._append_train_log(
                f"\n===== {label}：{' '.join(Path(c).name if i == 0 else c for i, c in enumerate(cmd))} =====\n")
            self.train_start_btn.config(state=tk.DISABLED)
            self.train_stop_btn.config(state=tk.NORMAL)
            self.train_open_out_btn.config(state=tk.DISABLED)
            # 训练运行期间禁用模型库删除按钮，防止删除与训练并发冲突
            if getattr(self, "model_del_btn", None) is not None:
                self.model_del_btn.config(state=tk.DISABLED)
            threading.Thread(target=self._read_train_output, daemon=True).start()

    def _read_train_output(self) -> None:
        """后台线程：读取训练子进程输出放入队列，并带上返回码标记结束原因。"""
        proc = self.train_proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            self.train_queue.put(line)
        rc = proc.wait()
        # 130 = 用户中断；非 0/130 视为异常，把返回码一并放入队列便于界面区分
        self.train_queue.put(f"\n[训练进程已结束 rc={rc}]\n")

    def _poll_train_queue(self) -> None:
        """主循环定时器：刷新训练日志并在进程结束后复位按钮状态。

        [METRIC] 前缀行是结构化指标输出（不写入文本日志区，转为核心指标表刷新）。
        """
        try:
            while True:
                line = self.train_queue.get_nowait()
                if line.startswith("[METRIC] "):
                    try:
                        self._append_metric_row(json.loads(line[len("[METRIC] "):]))
                    except (ValueError, TypeError):
                        pass
                    continue
                self._append_train_log(line)
                if "[训练进程已结束" in line:
                    self.train_start_btn.config(state=tk.NORMAL)
                    self.train_stop_btn.config(state=tk.DISABLED)
                    self.train_open_out_btn.config(state=tk.NORMAL)
                    # 训练结束后恢复模型库删除按钮
                    if getattr(self, "model_del_btn", None) is not None:
                        self.model_del_btn.config(state=tk.NORMAL)
                    # 定位最新产物日志文件，供「打开日志文件」按钮使用
                    self._locate_latest_train_log()
                    # 刷新续训断点可用状态（中断后新出现断点即可勾选）
                    self._refresh_resume_state()
                    # 返回码约定：0 正常（联动刷新模型库）/ 130 用户中断 / 其他异常高亮
                    m = re.search(r"rc=(\d+)", line)
                    if m:
                        rc = int(m.group(1))
                        if rc == 0:
                            # 正常结束：训练完成的模型已由脚本入库，联动刷新
                            # 模型库列表与训练页基础模型下拉（新模型可被选作微调起点）
                            try:
                                self.refresh_models()
                            except Exception as exc:
                                self._append_train_log(
                                    f"[提示] 刷新模型库失败：{exc}\n")
                            try:
                                self._refresh_base_model_options()
                            except Exception as exc:
                                self._append_train_log(
                                    f"[提示] 刷新基础模型列表失败：{exc}\n")
                        elif rc != 130:
                            self._append_train_log(
                                f"[提示] 训练进程异常退出（返回码 {rc}），"
                                f"请检查上方日志。\n")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_train_queue)

    def _stop_train(self) -> None:
        """请求终止训练子进程。"""
        if self.train_proc and self.train_proc.poll() is None:
            self.train_proc.terminate()
            self._append_train_log("[用户] 已请求终止训练进程。\n")

    def _open_train_out(self) -> None:
        """打开模型库根目录（训练产物统一入库，目录不存在则创建）。"""
        ml.MODEL_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(ml.MODEL_LIBRARY_DIR))  # noqa: S606

    def _locate_latest_train_log(self) -> None:
        """定位模型库中最新产物目录的 train_log.txt，供「打开日志文件」使用。"""
        try:
            candidates = list(ml.MODEL_LIBRARY_DIR.glob("*/train_log.txt"))
        except OSError:
            candidates = []
        if not candidates:
            return
        try:
            latest = max(candidates, key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        self._train_log_path = latest
        if getattr(self, "train_log_btn", None) is not None:
            self.train_log_btn.config(state=tk.NORMAL)

    def _append_train_log(self, text: str) -> None:
        """向训练日志框追加文本并滚动到底部。"""
        self.train_log.config(state=tk.NORMAL)
        self.train_log.insert(tk.END, text)
        self.train_log.see(tk.END)
        self.train_log.config(state=tk.DISABLED)

    # ---------- 标签页 4：模型库 ----------
    def _build_models_tab(self) -> None:
        """构建模型库标签页（列表/预览/激活/删除/导入，数据来自 model_library）。"""
        page = ttk.Frame(self.notebook)
        self.notebook.add(page, text=" 模型库 ")

        # 工具栏：刷新 / 激活 / 删除 / 打开目录 / 导入
        bar = ttk.Frame(page, padding=6)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="刷新", command=self.refresh_models).pack(side=tk.LEFT)
        ttk.Button(bar, text="设为激活模型",
                   command=self._activate_selected_model).pack(side=tk.LEFT, padx=6)
        self.model_del_btn = ttk.Button(bar, text="删除",
                                        command=self._delete_selected_model)
        self.model_del_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="打开模型目录",
                   command=self._open_selected_model_dir).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="导入外部模型",
                   command=self._import_external_model).pack(side=tk.LEFT, padx=6)
        ttk.Label(bar, text=f"模型库位置：{ml.MODEL_LIBRARY_DIR}",
                  foreground="#8a95a1").pack(side=tk.RIGHT)

        # 主体：左侧列表 + 右侧预览
        main = ttk.Frame(page)
        main.pack(fill=tk.BOTH, expand=True)

        # 列表区：模型名称/类别数/架构/主指标/日期版本/来源/激活标记
        left = ttk.Frame(main)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=(0, 6))
        columns = ("name", "classes", "architecture", "accuracy", "date_ver", "source", "active")
        headers = ("模型名称", "类别数", "架构", "主指标", "日期版本", "来源", "激活")
        widths = (210, 60, 150, 120, 120, 90, 50)
        self.model_tree = ttk.Treeview(left, columns=columns, show="headings",
                                       height=20)
        for col, head, w in zip(columns, headers, widths):
            self.model_tree.heading(col, text=head)
            self.model_tree.column(col, width=w, anchor=tk.W)
        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL,
                               command=self.model_tree.yview)
        self.model_tree.configure(yscrollcommand=scroll.set)
        self.model_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        # 激活模型行高亮（标签色 + ★ 标记）
        self.model_tree.tag_configure("active_row", background="#eaf2fc",
                                      foreground="#185a9d")
        # 选中行事件：刷新预览与详情
        self.model_tree.bind("<<TreeviewSelected>>", self._on_model_selected)

        # 预览区：固定 248×248 容器 + 详情标签
        right = ttk.LabelFrame(main, text=" 预览 ", padding=8)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=6, pady=(0, 6))
        preview_box = tk.Frame(right, width=248, height=248,
                               bg="#eef1f5", relief="groove", bd=1)
        preview_box.pack()
        preview_box.pack_propagate(False)
        if HAS_PIL:
            preview_text = "（选择模型查看预览）"
        else:
            # 未安装 Pillow：预览区整体降级为文字提示，列表功能不受影响
            preview_text = "（未安装 Pillow，\n无法显示缩略图）"
        self.model_preview_label = tk.Label(
            preview_box, text=preview_text, bg="#eef1f5", fg="#8a95a1",
            font=("Microsoft YaHei UI", 9), justify=tk.CENTER)
        self.model_preview_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.model_info_label = ttk.Label(right, text="", justify=tk.LEFT,
                                          wraplength=250)
        self.model_info_label.pack(fill=tk.X, pady=(8, 0))

        # 初始加载模型列表（模型库为空时显示占位提示）
        self.refresh_models()

    def refresh_models(self) -> None:
        """重新加载模型库列表（供工具栏刷新与训练完成联动调用）。"""
        tree = getattr(self, "model_tree", None)
        if tree is None:
            return
        selected = self._selected_model_name()
        tree.delete(*tree.get_children())
        try:
            models = ml.list_models(include_builtin=True)
            active_record = ml.get_active_species_model()
        except Exception as exc:
            messagebox.showerror("错误", f"读取模型注册表失败：{exc}")
            return
        active_name = active_record.get("name") if active_record else None
        if not models:
            tree.insert("", tk.END,
                        values=("（模型库为空）", "—", "—", "—", "—", "—", ""),
                        tags=())
        for record in models:
            name = str(record.get("name") or "")
            classes = record.get("class_count")
            accuracy = record.get("accuracy")
            # 主指标显示：按记录里的 metric_name 格式化（旧记录默认 Top-1）
            accuracy_text = ml.format_metric_value(record.get("metric_name"), accuracy)
            # 架构显示名（旧记录缺字段按 resnet50 兜底，经 get_architecture_info 归一）
            arch_text = ml.get_architecture_info(
                record.get("architecture")).get("display_name", "—")
            if record.get("builtin"):
                # 内置预置条目：无日期版本，激活列恒空
                date_ver = "—"
            else:
                try:
                    version_text = f"v{int(record.get('version')):03d}"
                except (TypeError, ValueError):
                    version_text = "—"
                date_ver = f"{record.get('date_str') or '—'} {version_text}"
            is_active = bool(name) and name == active_name
            values = (name,
                      classes if classes is not None else "—",
                      arch_text, accuracy_text, date_ver,
                      record.get("source") or "—",
                      "★" if is_active else "")
            tags = ("active_row",) if is_active else ()
            tree.insert("", tk.END, values=values, tags=tags)
        # 刷新后尽量恢复原选中；选中项已被删除则清空预览
        if selected:
            for item in tree.get_children():
                if str(tree.item(item, "values")[0]) == selected:
                    tree.selection_set(item)
                    tree.see(item)
                    break
            else:
                self._clear_model_preview()
        # 模型库变化需同步到训练页「基础模型」下拉（导入/删除后立即生效，无需重启）
        self._refresh_base_model_options()

    def _selected_model_name(self) -> str | None:
        """取列表当前选中行的模型名称；未选中返回 None。"""
        tree = getattr(self, "model_tree", None)
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        values = tree.item(selection[0], "values")
        return str(values[0]) if values else None

    def _clear_model_preview(self) -> None:
        """清空预览图与详情（无 Pillow 或未选中模型时显示占位文字）。"""
        self._model_preview_ref = None  # 释放旧图引用，允许垃圾回收
        if not HAS_PIL:
            self.model_preview_label.config(
                image="", text="（未安装 Pillow，\n无法显示缩略图）")
        else:
            self.model_preview_label.config(image="", text="（选择模型查看预览）")
        self.model_info_label.config(text="")

    def _on_model_selected(self, _event=None) -> None:
        """列表选中事件：显示该模型的预览缩略图与详情。"""
        name = self._selected_model_name()
        if not name or name == "（模型库为空）":
            self._clear_model_preview()
            return
        record = ml.get_model(name)
        self._show_model_preview(record)
        if record:
            mode_key = record.get("mode")
            mode_text = ml.TRAINING_MODE_LABELS.get(mode_key, mode_key or "—")
            arch_info = ml.get_architecture_info(record.get("architecture"))
            detect_text = "可推理" if arch_info.get("inference_supported") else "仅管理"
            lines = [
                f"名称：{record.get('name') or '—'}",
                f"架构：{arch_info.get('display_name', '—')}（{detect_text}）",
                f"任务：{record.get('task_type') or arch_info.get('task_type', '—')}",
                f"类别数：{record.get('class_count', '—')}",
                f"主指标：{ml.format_metric_value(record.get('metric_name'), record.get('accuracy'))}",
                f"模式：{mode_text}",
                f"注册时间：{record.get('trained_at') or '—'}",
                f"来源：{record.get('source') or '—'}",
                f"路径：{record.get('path') or '—'}",
            ]
            # 全部评估指标（若有）逐项列出，便于对比不同判据下的表现
            metrics = record.get("metrics")
            if isinstance(metrics, dict) and metrics:
                metric_text = "，".join(
                    ml.format_metric_value(k, v) for k, v in metrics.items())
                lines.append(f"全部指标：{metric_text}")
            if record.get("builtin") and record.get("note"):
                lines.append(f"说明：{record['note']}")
            self.model_info_label.config(text="\n".join(lines))
        else:
            self.model_info_label.config(text="")

    def _show_model_preview(self, record) -> None:
        """按模型记录显示预览缩略图（缺失时生成默认占位图）。"""
        # 无 Pillow 时降级为文字提示（列表功能不受影响）
        if not HAS_PIL:
            self.model_preview_label.config(
                image="", text="（未安装 Pillow，\n无法显示缩略图）")
            return
        self._model_preview_ref = None  # 先释放旧图引用
        image = None
        preview_path = record.get("preview_path") if record else None
        if preview_path:
            path = Path(preview_path)
            if path.is_file():
                try:
                    image = Image.open(path).convert("RGB")
                except Exception:
                    image = None  # 预览文件损坏时改用默认占位图
        if image is None:
            # 预览缺失：生成默认占位图（写入系统临时目录后加载）
            try:
                temp_path = (Path(tempfile.gettempdir()) /
                             "bird_model_default_preview.png")
                made = ml.make_default_preview(temp_path)
                if made and Path(made).is_file():
                    image = Image.open(made).convert("RGB")
            except Exception:
                image = None
        if image is None:
            self.model_preview_label.config(image="", text="（暂无预览图）")
            return
        image.thumbnail((240, 240))
        photo = ImageTk.PhotoImage(image)
        self._model_preview_ref = photo  # 持有引用防止被垃圾回收
        self.model_preview_label.config(image=photo, text="")

    def _delete_selected_model(self) -> None:
        """删除选中的模型（连同目录与注册表记录），删除后刷新列表。"""
        name = self._selected_model_name()
        if not name:
            messagebox.showinfo("提示", "请先在列表中选择要删除的模型。")
            return
        record = ml.get_model(name)
        if record and record.get("builtin"):
            messagebox.showinfo("提示", "「内置预置」模型随软件提供，不可删除。")
            return
        model_path = record.get("path") if record else None
        if model_path:
            message = f"确定删除模型「{name}」吗？\n将同时删除模型目录：\n{model_path}"
        else:
            message = f"确定删除模型「{name}」吗？"
        if not messagebox.askokcancel("删除确认", message):
            return
        # 记录是否为激活模型，删除成功后按需提示回退
        active_record = ml.get_active_species_model()
        was_active = bool(active_record) and active_record.get("name") == name
        if not ml.remove_model(name):
            messagebox.showerror("错误", "删除失败：模型不存在或注册表读取异常。")
            return
        self._clear_model_preview()
        self.refresh_models()
        if was_active:
            messagebox.showinfo("提示", "模型已删除；该模型为激活项，激活项已回退。")

    def _activate_selected_model(self) -> None:
        """把选中的模型设为激活物种模型（写注册表后刷新列表）。"""
        name = self._selected_model_name()
        if not name:
            messagebox.showinfo("提示", "请先在列表中选择要激活的模型。")
            return
        try:
            ml.set_active_species_model(name)
        except ValueError as exc:
            messagebox.showerror("错误", str(exc))
            return
        self.refresh_models()
        messagebox.showinfo("提示", f"已将「{name}」设为激活物种模型。")

    def _open_selected_model_dir(self) -> None:
        """用资源管理器打开选中模型的目录。"""
        name = self._selected_model_name()
        if not name:
            messagebox.showinfo("提示", "请先在列表中选择模型。")
            return
        record = ml.get_model(name)
        if record and record.get("builtin") and not (record.get("path") or "").strip():
            messagebox.showinfo("提示", "「内置预置」模型无独立目录（权重随程序提供）。")
            return
        model_path = record.get("path") if record else None
        if model_path and Path(model_path).is_dir():
            os.startfile(str(model_path))  # noqa: S606
        else:
            messagebox.showerror("错误", "模型目录不存在或已被移动。")

    def _import_external_model(self) -> None:
        """导入外部模型：选目录或单文件 → 探测架构/标签 → 确认后注册入库。

        支持三类来源（任一即可）：
          ① 训练产物目录（含 best_model.pt / last_model.pt）；
          ② HuggingFace 模型目录（config.json + 权重文件）；
          ③ 任意 .pt/.pth/.safetensors/.bin 权重文件（自动在模型库建目录归档）。
        识别出的架构可在确认对话框中手动改选；不支持的架构仅入库管理。
        """
        src = filedialog.askdirectory(
            title="选择外部模型目录（支持训练产物 / HuggingFace目录 / 含权重文件的目录）")
        if not src:
            return
        src_dir = Path(src)
        ok, note, candidates = ml.validate_import_source(src_dir)
        if not ok:
            # 目录内无可识别权重：引导用户改为选择单个权重文件
            if not messagebox.askyesno(
                    "未找到权重文件",
                    f"{note}\n\n是否改为选择单个权重文件（.pt / .pth / .safetensors）？"):
                return
            picked = filedialog.askopenfilename(
                title="选择模型权重文件",
                filetypes=[("模型权重", "*.pt *.pth *.safetensors *.bin"),
                           ("所有文件", "*.*")])
            if not picked:
                return
            staged = self._stage_weight_file(Path(picked))
            if staged is None:
                return
            src_dir = staged
            ok, note, candidates = ml.validate_import_source(src_dir)
            if not ok:
                messagebox.showerror("错误", f"导入失败：{note}")
                return
        # 目录内含多个权重且无单一模型标识（best/last/HF 配置）时，让用户指定具体文件
        has_single_marker = any((src_dir / n).is_file()
                                for n in ("best_model.pt", "last_model.pt")) \
            or ml.has_hf_config(src_dir)
        if not has_single_marker and len(candidates) > 1:
            picked = filedialog.askopenfilename(
                title="该目录含多个权重文件，请选择要导入的一个",
                initialdir=str(src_dir),
                filetypes=[("模型权重", "*.pt *.pth *.safetensors *.bin"),
                           ("所有文件", "*.*")])
            if not picked:
                return
            staged = self._stage_weight_file(Path(picked))
            if staged is None:
                return
            src_dir = staged
            ok, note, candidates = ml.validate_import_source(src_dir)
            if not ok:
                messagebox.showerror("错误", f"导入失败：{note}")
                return
        # 探测架构（尽力而为；无 torch 时按目录特征启发，用户可在对话框改选）
        weight_path = candidates[0] if candidates else None
        arch_key, arch_note = self._probe_architecture(src_dir, weight_path)
        # 自动生成类别标签（labels.json 已存在时不覆盖）
        labels = ml.build_labels_auto(src_dir)
        class_count = len(labels) if labels else None
        # 弹确认对话框：架构下拉 + 类别数
        confirmed = self._ask_import_confirm(src_dir, arch_key, arch_note, class_count)
        if confirmed is None:
            return
        arch_key, class_count = confirmed
        # 落盘 labels.json（目录内已有则保留原文件）
        if not (src_dir / "labels.json").is_file():
            generated = ml.build_labels_auto(src_dir, class_count=class_count)
            if generated:
                try:
                    (src_dir / "labels.json").write_text(
                        json.dumps(generated, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                except OSError as exc:
                    messagebox.showerror("错误", f"写入 labels.json 失败：{exc}")
                    return
        # 注册进模型库
        try:
            record = ml.register_model(src_dir, class_count=class_count,
                                       source="外部注册", architecture=arch_key)
        except (ValueError, OSError) as exc:
            messagebox.showerror("错误", f"导入失败：{exc}")
            return
        self.refresh_models()
        info = ml.get_architecture_info(arch_key)
        extra = "" if info.get("inference_supported") \
            else "\n注意：该架构仅入库管理，视觉分析页不可调用。"
        messagebox.showinfo("导入成功",
                            f"已注册外部模型：{record.get('name')}\n"
                            f"架构：{info['display_name']}{extra}")

    def _stage_weight_file(self, picked: Path) -> Path | None:
        """把单个权重文件归档到模型库新建目录，返回目录路径（失败返回 None）。

        目录名按 基名_M月D日_NNN 规则取号；.safetensors 保持标准名
        model.safetensors，其余统一复制为 best_model.pt，便于视觉系统按既有
        约定（best_model.pt / model.safetensors）定位权重。
        同时复制同目录下的标签/配置旁路文件（labels.json、label_mapping.txt、
        config.json 等），以便自动生成类别映射。
        """
        if not picked.is_file():
            messagebox.showerror("错误", f"权重文件不存在：{picked}")
            return None
        base = re.sub(r"[^A-Za-z0-9_\-]", "_", picked.stem) or "external_model"
        dir_name, _date_str, _ver = ml.next_model_name(base)
        target_dir = ml.MODEL_LIBRARY_DIR / dir_name
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if picked.suffix.lower() == ".safetensors":
                dest = target_dir / "model.safetensors"
            else:
                dest = target_dir / "best_model.pt"
            shutil.copy2(picked, dest)
            # 归档旁路文件：标签映射与 HF 配置，供类别数/类别名自动推断
            for name in ("labels.json", "label_mapping.txt", "labels.txt",
                         "classes.txt", "class_names.json", "config.json",
                         "preprocessor_config.json"):
                side = picked.parent / name
                if side.is_file() and not (target_dir / name).exists():
                    try:
                        shutil.copy2(side, target_dir / name)
                    except OSError:
                        pass
        except OSError as exc:
            messagebox.showerror("错误", f"归档权重文件失败：{exc}")
            return None
        return target_dir

    def _probe_architecture(self, src_dir: Path, weight_path) -> tuple:
        """尽力探测模型架构，返回 (架构键, 说明文本)。

        优先按权重键名精确识别（safetensors 无需 torch；.pt/.pth 需 torch）；
        GUI 进程通常无 torch，此时退化为目录特征启发式。
        """
        # 1. safetensors：safe_open 可直接读键名，不依赖 torch
        if weight_path is not None and str(weight_path).lower().endswith(".safetensors"):
            try:
                from safetensors import safe_open  # type: ignore
                with safe_open(str(weight_path), framework="pt") as handle:
                    keys = list(handle.keys())
                return ml.detect_architecture(keys), "按 safetensors 键名识别"
            except Exception:
                pass
        # 2. torch 可用时读取权重键名精确识别
        try:
            import torch  # type: ignore
            if weight_path is not None and Path(weight_path).suffix.lower() in (".pt", ".pth", ".bin"):
                try:
                    ckpt = torch.load(str(weight_path), map_location="cpu",
                                      weights_only=True)
                except Exception:
                    ckpt = torch.load(str(weight_path), map_location="cpu",
                                      weights_only=False)
                for key in ("state_dict", "model_state_dict", "model_state",
                            "model", "weights"):
                    if isinstance(ckpt, dict) and isinstance(ckpt.get(key), dict):
                        ckpt = ckpt[key]
                        break
                if isinstance(ckpt, dict) and ckpt:
                    return ml.detect_architecture(ckpt), "按权重键名识别"
        except Exception:
            pass
        # 3. 目录特征启发式（无 torch 时的兜底）
        if ml.has_hf_config(src_dir):
            return "vit_hf", "按 HuggingFace 目录结构推断（可手动改选）"
        return "resnet50", "未能读取权重，按默认 ResNet50 处理（可手动改选）"

    def _ask_import_confirm(self, model_dir: Path, arch_key: str,
                            arch_note: str, class_count) -> tuple | None:
        """导入确认对话框：架构下拉 + 类别数输入；取消或关闭返回 None。

        返回 (架构键, 类别数)。
        """
        keys = list(ml.ARCHITECTURE_REGISTRY.keys())
        display_to_key = {ml.ARCHITECTURE_REGISTRY[k]["display_name"]: k for k in keys}
        names = [ml.ARCHITECTURE_REGISTRY[k]["display_name"] for k in keys]
        default_display = ml.ARCHITECTURE_REGISTRY.get(
            arch_key, ml.ARCHITECTURE_REGISTRY["generic"])["display_name"]

        dialog = tk.Toplevel(self.root)
        dialog.title("导入外部模型")
        dialog.resizable(False, False)
        ttk.Label(dialog, text=f"模型目录：{model_dir.name}",
                  padding=(16, 12), justify=tk.LEFT).pack(anchor=tk.W)
        ttk.Label(dialog, text=f"识别说明：{arch_note}",
                  padding=(16, 0), foreground="#8a95a1").pack(anchor=tk.W)

        # 架构下拉
        row1 = ttk.Frame(dialog)
        row1.pack(fill=tk.X, padx=16, pady=(10, 0))
        ttk.Label(row1, text="模型架构：").pack(side=tk.LEFT)
        arch_var = tk.StringVar(value=default_display)
        ttk.Combobox(row1, textvariable=arch_var, values=names,
                     state="readonly", width=32).pack(side=tk.LEFT)

        # 任务类型 / 可推理性提示（随下拉联动刷新）
        tip_var = tk.StringVar()
        ttk.Label(dialog, textvariable=tip_var, padding=(16, 6),
                  foreground="#b06a00", wraplength=360,
                  justify=tk.LEFT).pack(anchor=tk.W)

        def _update_tip(*_args) -> None:
            """按当前所选架构刷新提示文本。"""
            info = ml.ARCHITECTURE_REGISTRY.get(
                display_to_key.get(arch_var.get(), "generic"), {})
            if info.get("inference_supported"):
                tip_var.set(f"任务类型：{info.get('task_type')}；"
                            f"可在视觉分析页调用推理。")
            else:
                tip_var.set(f"任务类型：{info.get('task_type')}；"
                            f"该架构仅入库管理，视觉分析页不可调用。")

        arch_var.trace_add("write", _update_tip)
        _update_tip()

        # 类别数（预填自动推断值）
        row2 = ttk.Frame(dialog)
        row2.pack(fill=tk.X, padx=16, pady=(4, 0))
        ttk.Label(row2, text="类别数：").pack(side=tk.LEFT)
        count_var = tk.StringVar(value=str(class_count) if class_count else "")
        entry = ttk.Entry(row2, textvariable=count_var, width=12)
        entry.pack(side=tk.LEFT)
        entry.focus_set()

        result: dict = {"value": None}

        def _confirm() -> None:
            """确定：校验类别数为正整数后关闭对话框。"""
            try:
                value = int(count_var.get().strip())
            except ValueError:
                messagebox.showerror("错误", "类别数必须是正整数。", parent=dialog)
                return
            if value < 1:
                messagebox.showerror("错误", "类别数必须 ≥ 1。", parent=dialog)
                return
            result["value"] = (display_to_key.get(arch_var.get(), "generic"), value)
            dialog.destroy()

        btns = ttk.Frame(dialog)
        btns.pack(pady=(12, 14))
        ttk.Button(btns, text="确定", style="Accent.TButton",
                   command=_confirm).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="取消",
                   command=dialog.destroy).pack(side=tk.LEFT, padx=6)
        entry.bind("<Return>", lambda _event: _confirm())
        dialog.transient(self.root)
        dialog.grab_set()
        self.root.wait_window(dialog)
        return result["value"]

    # ================= Pillow 可选依赖 =================
    def _prompt_pillow_install(self) -> None:
        """启动早期检测 Pillow：缺失时弹窗说明并提供一键安装（可跳过）。"""
        if HAS_PIL:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("缺少 Pillow 依赖")
        dialog.resizable(False, False)
        ttk.Label(dialog, text="未检测到 Pillow 图像库。\n"
                               "缩略图与模型预览功能需要 Pillow 支持；\n"
                               "不安装也可以继续使用其余功能。",
                  padding=(20, 16), justify=tk.CENTER).pack()
        self._pillow_dialog_btns = ttk.Frame(dialog)
        self._pillow_dialog_btns.pack(pady=(0, 16))
        ttk.Button(self._pillow_dialog_btns, text="一键安装", style="Accent.TButton",
                   command=lambda: self._install_pillow(dialog)).pack(
            side=tk.LEFT, padx=6)
        ttk.Button(self._pillow_dialog_btns, text="暂不安装",
                   command=dialog.destroy).pack(side=tk.LEFT, padx=6)
        dialog.transient(self.root)
        dialog.grab_set()

    def _install_pillow(self, dialog: tk.Toplevel) -> None:
        """后台执行 pip 安装 Pillow（用当前解释器），完成后提示重启。"""
        # 禁用按钮防止重复点击
        self._set_pillow_dialog_buttons(dialog, tk.DISABLED)
        ttk.Label(dialog, text="正在后台安装 Pillow，请稍候…",
                  foreground="#1d7a34").pack()

        def _worker() -> None:
            """后台线程：执行 pip 安装并把结果回投到主线程。"""
            try:
                completed = subprocess.run(  # noqa: S603
                    [sys.executable, "-m", "pip", "install", "Pillow"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=600)
                ok = completed.returncode == 0
                detail = ((completed.stdout or "") +
                          (completed.stderr or ""))[-600:]
            except Exception as exc:  # 网络异常/超时等
                ok, detail = False, str(exc)
            self.root.after(0, lambda: self._install_pillow_done(dialog, ok, detail))

        threading.Thread(target=_worker, daemon=True).start()

    def _install_pillow_done(self, dialog: tk.Toplevel,
                             ok: bool, detail: str) -> None:
        """Pillow 安装结束回调：成功提示重启，失败展示输出片段并允许重试。"""
        if ok:
            dialog.destroy()
            messagebox.showinfo(
                "安装完成", "Pillow 安装完成，请重启程序以启用缩略图与预览功能。")
        else:
            messagebox.showerror(
                "安装失败", f"Pillow 安装失败，请检查网络后重试：\n{detail}")
            self._set_pillow_dialog_buttons(dialog, tk.NORMAL)

    @staticmethod
    def _set_pillow_dialog_buttons(dialog: tk.Toplevel, state: str) -> None:
        """统一设置 Pillow 安装对话框内全部按钮的可用状态。"""
        for child in dialog.winfo_children():
            if isinstance(child, ttk.Frame):
                for widget in child.winfo_children():
                    if isinstance(widget, ttk.Button):
                        widget.config(state=state)

    # ---------- 标签页 5：视觉分析（惰性构建） ----------
    def _build_vision_tab(self) -> None:
        """注册「视觉分析」占位页签（内容在首次切到该页时才惰性构建）。

        目的：避免启动时立刻导入视觉系统模块（其依赖 Pillow/深度学习库），
        拖慢控制台启动速度；页签顺序位于「模型库」之后。
        """
        self._vision_tab = ttk.Frame(self.notebook)
        self.notebook.add(self._vision_tab, text=" 视觉分析 ")
        self._vision_built = False      # 是否已完成惰性构建（仅构建一次）
        self._vision_panel = None       # 嵌入的视觉问答面板（BirdVqaPanel）引用
        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

    def _on_notebook_tab_changed(self, _event=None) -> None:
        """页签切换回调：首次切到「视觉分析」页时触发惰性构建。"""
        if self._vision_built:
            return                      # 已构建过，直接返回（仅构建一次）
        if self.notebook.select() == str(self._vision_tab):
            self._build_vision_tab_content()

    def _build_vision_tab_content(self) -> None:
        """构建「视觉分析」页内容：定位视觉系统目录并嵌入问答面板。"""
        self._vision_built = True       # 无论成败只构建一次，避免重复覆盖
        page = self._vision_tab
        vision_dir = _find_vision_system_dir()
        if vision_dir is None:
            # 未找到视觉系统目录：页内显示中文指引，不弹窗、不崩溃
            hint = (
                "未找到「鸟类视觉分析系统」目录，暂时无法嵌入视觉问答面板。\n\n"
                "已探测的位置：\n"
                "  1. 设置文件 data/model_library/settings.json 中的 vision_system_dir 键"
                "（当前未配置或指向的目录不含 gui_app.py）；\n"
                "  2. 安装包内置目录 vision_system\\（本机未发现，安装包应为完整版）；\n"
                f"  3. 项目根目录 {BASE_DIR.parent.parent} 下名称以"
                "「鸟类视觉分析系统_」开头的文件夹。\n\n"
                "处理办法（任选其一）：\n"
                "  - 将鸟类视觉分析系统目录放到上述项目根目录下（目录名以"
                "「鸟类视觉分析系统_」开头，内含 gui_app.py）；\n"
                "  - 或手工编辑 data/model_library/settings.json，新增键"
                " vision_system_dir，值为视觉系统目录的完整路径，然后重启控制台。"
            )
            tk.Label(page, text=hint, justify=tk.LEFT, anchor="nw",
                     fg="#8a5a2a", bg="#fdf6ec",
                     font=("Microsoft YaHei UI", 10),
                     padx=24, pady=24).pack(fill=tk.BOTH, expand=True)
            return
        try:
            # 冲突防护：若 sys.modules 中已有名为 gui_app 的异源模块，先移除
            # 再导入，避免误用其他项目同名模块（如打包环境残留）
            existing = sys.modules.get("gui_app")
            if existing is not None:
                existing_file = str(getattr(existing, "__file__", "") or "")
                try:
                    same_source = (Path(existing_file).resolve() ==
                                   (vision_dir / "gui_app.py").resolve())
                except OSError:
                    same_source = False
                if not same_source:
                    del sys.modules["gui_app"]
            # 视觉系统内部模块平铺互引（import config 等），
            # 导入 gui_app 前必须把其目录插入 sys.path（已存在则不重复插）
            vision_str = str(vision_dir)
            if vision_str not in sys.path:
                sys.path.insert(0, vision_str)
            import gui_app              # 运行时按需导入视觉系统界面模块
            # 问答服务模式：模型推理在训练环境 Python 的子进程中运行
            # （qa_server.py，JSON 行协议），控制台 exe 无需内置 torch；
            # 解释器与「模型训练」页共用同一份配置
            service_cmd = None
            if (vision_dir / "qa_server.py").is_file():
                service_cmd = [self._train_python(), "-u",
                               str(vision_dir / "qa_server.py")]
            # 实例化问答面板并铺满页签
            self._vision_panel = gui_app.BirdVqaPanel(page,
                                                      qa_service_cmd=service_cmd)
            self._vision_panel.pack(fill=tk.BOTH, expand=True)
            # 本地模式（无服务）下导入成功后刷新模型下拉，确保读到
            # 「出厂+共享模型库」合并注册表；服务模式下由子进程提供
            if service_cmd is None and hasattr(self._vision_panel, "refresh_models"):
                self._vision_panel.refresh_models()
        except Exception as exc:        # 导入/构建失败：页内显示中文错误说明
            err = (
                "「视觉分析」面板加载失败，控制台其余功能不受影响。\n\n"
                f"视觉系统目录：{vision_dir}\n"
                f"错误信息：{exc}\n\n"
                "请检查该目录下的 gui_app.py 是否完整、依赖（Pillow 等）是否已安装，"
                "处理后重启控制台重试。"
            )
            tk.Label(page, text=err, justify=tk.LEFT, anchor="nw",
                     fg="#a02020", bg="#fdecec",
                     font=("Microsoft YaHei UI", 10),
                     padx=24, pady=24).pack(fill=tk.BOTH, expand=True)

    # ---------- 标签页 6：数据浏览 ----------
    def _build_browse_tab(self) -> None:
        page = ttk.Frame(self.notebook)
        self.notebook.add(page, text=" 数据浏览 ")

        bar = ttk.Frame(page, padding=6); bar.pack(fill=tk.X)
        ttk.Label(bar, text="关键词:").pack(side=tk.LEFT)
        self.obs_search_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.obs_search_var, width=26)
        entry.pack(side=tk.LEFT, padx=4)
        entry.bind("<Return>", lambda e: self._load_observations())
        ttk.Label(bar, text="类群:").pack(side=tk.LEFT, padx=(8, 0))
        self.obs_taxon_var = tk.StringVar(value="全部类群")
        self.obs_taxon_combo = ttk.Combobox(bar, textvariable=self.obs_taxon_var,
                                            width=24, state="readonly")
        self.obs_taxon_combo.pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="查询", command=self._search_observations).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="刷新类群列表",
                   command=self._refresh_taxon_filters).pack(side=tk.LEFT, padx=4)

        columns = ("obs_id", "species", "sci", "date", "place", "observer", "photos")
        headers = ("观察ID", "物种", "学名", "观察日期", "地点", "观察者", "照片数")
        widths = (90, 150, 160, 90, 240, 130, 60)
        self.obs_tree = ttk.Treeview(page, columns=columns, show="headings", height=22)
        for col, head, w in zip(columns, headers, widths):
            self.obs_tree.heading(col, text=head)
            self.obs_tree.column(col, width=w, anchor=tk.W)
        self.obs_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))
        self.obs_tree.bind("<Double-1>", self._open_observation_uri)

        pager = ttk.Frame(page, padding=(6, 0, 6, 6)); pager.pack(fill=tk.X)
        ttk.Button(pager, text="上一页",
                   command=lambda: self._page_observations(-1)).pack(side=tk.LEFT)
        ttk.Button(pager, text="下一页",
                   command=lambda: self._page_observations(1)).pack(side=tk.LEFT, padx=6)
        self.obs_page_label = ttk.Label(pager, text="")
        self.obs_page_label.pack(side=tk.LEFT, padx=10)
        ttk.Label(pager, text="双击行可打开 iNaturalist 页面",
                  foreground="#8a95a1").pack(side=tk.RIGHT)

    # ---------- 标签页 7：照片浏览 ----------
    def _build_photos_tab(self) -> None:
        page = ttk.Frame(self.notebook)
        self.notebook.add(page, text=" 照片浏览 ")

        bar = ttk.Frame(page, padding=6); bar.pack(fill=tk.X)
        ttk.Label(bar, text="类群:").pack(side=tk.LEFT)
        self.ph_taxon_var = tk.StringVar(value="全部类群")
        self.ph_taxon_combo = ttk.Combobox(bar, textvariable=self.ph_taxon_var,
                                           width=24, state="readonly")
        self.ph_taxon_combo.pack(side=tk.LEFT, padx=4)
        self.ph_taxon_combo.bind("<<ComboboxSelected>>", lambda e: self._load_photos())
        ttk.Button(bar, text="刷新", command=self._load_photos).pack(side=tk.LEFT, padx=4)
        if not HAS_PIL:
            ttk.Label(bar, text="（未安装 Pillow，仅显示文件名；pip install pillow 可显示缩略图）",
                      foreground="#b06818").pack(side=tk.LEFT, padx=8)

        # 缩略图画布区（可滚动）
        canvas_frame = ttk.Frame(page)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=6)
        self.ph_canvas = tk.Canvas(canvas_frame, highlightthickness=0, bg="#f4f6f9")
        scroll = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.ph_canvas.yview)
        self.ph_inner = ttk.Frame(self.ph_canvas)
        self.ph_inner.bind("<Configure>",
                           lambda e: self.ph_canvas.configure(scrollregion=self.ph_canvas.bbox("all")))
        self.ph_canvas.create_window((0, 0), window=self.ph_inner, anchor=tk.NW, width=1000)
        self.ph_canvas.configure(yscrollcommand=scroll.set)
        self.ph_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        pager = ttk.Frame(page, padding=(6, 4, 6, 6)); pager.pack(fill=tk.X)
        ttk.Button(pager, text="上一页", command=lambda: self._page_photos(-1)).pack(side=tk.LEFT)
        ttk.Button(pager, text="下一页", command=lambda: self._page_photos(1)).pack(side=tk.LEFT, padx=6)
        self.ph_page_label = ttk.Label(pager, text="")
        self.ph_page_label.pack(side=tk.LEFT, padx=10)
        ttk.Label(pager, text="双击图片用系统看图工具打开原图", foreground="#8a95a1").pack(side=tk.RIGHT)

    # ---------- 标签页 8：统计与说明 ----------
    def _build_stats_tab(self) -> None:
        page = ttk.Frame(self.notebook)
        self.notebook.add(page, text=" 统计与说明 ")

        bar = ttk.Frame(page, padding=6); bar.pack(fill=tk.X)
        ttk.Button(bar, text="刷新统计", command=self._load_stats).pack(side=tk.LEFT)

        self.stats_text = scrolledtext.ScrolledText(page, state=tk.DISABLED,
                                                    font=("Microsoft YaHei", 10))
        self.stats_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

    # ================= 数据库辅助 =================
    def _ensure_databases(self) -> None:
        """数据库不存在时自动初始化（两层数据库一次性建齐）。"""
        if not DEFAULT_MAIN_DB.exists() or not DEFAULT_RAW_DB.exists():
            try:
                from inaturalist_db_initializer import init_all_databases
                init_all_databases(str(DEFAULT_RAW_DB), str(DEFAULT_MAIN_DB))
                self._append_log(f"[初始化] 已自动创建数据库：{DEFAULT_MAIN_DB.parent}\n")
            except Exception as exc:
                messagebox.showerror("数据库初始化失败", str(exc))
        self._refresh_taxon_filters()

    def _connect_main(self) -> sqlite3.Connection:
        """只读连接主数据库，保证界面操作不会破坏爬虫写入的数据。"""
        conn = sqlite3.connect(f"file:{DEFAULT_MAIN_DB.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _refresh_taxon_filters(self) -> None:
        """从数据库读取类群列表，填充两个筛选下拉框。"""
        values = ["全部类群"]
        try:
            with self._connect_main() as conn:
                rows = conn.execute(
                    "SELECT taxon_id, common_name, scientific_name FROM taxa "
                    "ORDER BY common_name").fetchall()
            values += [f"{r['taxon_id']}  {r['common_name']}" for r in rows]
        except sqlite3.Error:
            pass
        self.obs_taxon_combo["values"] = values
        self.ph_taxon_combo["values"] = values
        if self.obs_taxon_var.get() not in values:
            self.obs_taxon_var.set("全部类群")
        if self.ph_taxon_var.get() not in values:
            self.ph_taxon_var.set("全部类群")

    @staticmethod
    def _parse_taxon_filter(text: str) -> int | None:
        """从下拉框文本（如 '20967  戴胜科'）解析 taxon_id。"""
        if not text or text == "全部类群":
            return None
        try:
            return int(text.split()[0])
        except (ValueError, IndexError):
            return None

    def _resolve_photo_path(self, local_path: str) -> Path | None:
        """将数据库中的照片相对路径解析为实际文件（文件名索引缓存）。"""
        if not local_path:
            return None
        path = Path(local_path)
        if path.is_absolute() and path.is_file():
            return path
        candidate = DATA_DIR / path
        if candidate.is_file():
            return candidate
        # 相对类群文件夹的路径（photos\\obs_xxx.jpg）→ 按文件名全盘索引
        if self.photo_index is None:
            index: dict = {}
            for item in DATA_DIR.rglob("*"):
                if item.is_file() and item.suffix.lower() in (
                        ".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    index.setdefault(item.name, item)
            self.photo_index = index
        return self.photo_index.get(path.name)

    # ================= 下载控制 =================
    def _start_download(self) -> None:
        """启动爬虫子进程（复用 batch_download_birds.py，去重逻辑在库管理器内）。"""
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("提示", "下载进程正在运行中。")
            return

        # 优先使用自定义 ID，其次使用列表选择，都不填则使用全部默认类群
        taxon_ids: list[int] = []
        custom = self.custom_taxon_var.get().strip()
        if custom:
            try:
                taxon_ids = [int(x) for x in custom.replace("，", ",").split(",") if x.strip()]
            except ValueError:
                messagebox.showerror("输入错误", "自定义类群 ID 必须是逗号分隔的数字。")
                return
        else:
            for sel in self.taxon_listbox.curselection():
                taxon_ids.append(int(self.taxon_listbox.get(sel).split()[0]))

        cmd = [sys.executable, str(BASE_DIR / "batch_download_birds.py")]
        if taxon_ids:
            cmd += ["--taxon-ids", *[str(t) for t in taxon_ids]]
        cmd += [
            "--max-per-taxon", str(self.max_var.get()),
            "--photo-size", self.size_var.get(),
            "--quality-grade", self.grade_var.get(),
            "--workers", str(self.workers_var.get()),
            "--out-dir", str(DATA_DIR),
            "--db-dir", str(DB_DIR),
        ]

        # 子进程用 UTF-8 输出，避免中文日志乱码
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env, cwd=str(BASE_DIR))

        self._append_log(f"\n===== 启动下载：{' '.join(cmd[2:])} =====\n")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        threading.Thread(target=self._read_proc_output, daemon=True).start()

    def _read_proc_output(self) -> None:
        """后台线程：读取子进程输出放入队列。"""
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.log_queue.put(line)
        self.log_queue.put("\n[下载进程已结束]\n")

    def _stop_download(self) -> None:
        """终止爬虫子进程。"""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._append_log("[用户] 已请求终止下载进程。\n")

    def _poll_log_queue(self) -> None:
        """主循环定时器：把子进程日志刷到界面。"""
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log(line)
                if "[下载进程已结束]" in line:
                    self.start_btn.config(state=tk.NORMAL)
                    self.stop_btn.config(state=tk.DISABLED)
                    self._refresh_taxon_filters()   # 下载结束后自动刷新类群列表
        except queue.Empty:
            pass
        self.root.after(200, self._poll_log_queue)

    def _append_log(self, text: str) -> None:
        """向日志框追加文本并滚动到底部。"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ================= 数据浏览 =================
    def _search_observations(self) -> None:
        """点击查询：重置到第一页。"""
        self._obs_offset = 0
        self._load_observations()

    def _page_observations(self, direction: int) -> None:
        """翻页。"""
        self._obs_offset = max(0, self._obs_offset + direction * 50)
        self._load_observations()

    def _load_observations(self) -> None:
        """按筛选条件加载观察记录到表格。"""
        q = self.obs_search_var.get().strip()
        taxon_id = self._parse_taxon_filter(self.obs_taxon_var.get())
        where, params = [], []
        if q:
            like = f"%{q}%"
            where.append("(common_name LIKE ? OR scientific_name LIKE ? OR "
                         "species_guess LIKE ? OR place_guess LIKE ? OR observer_login LIKE ?)")
            params += [like] * 5
        if taxon_id is not None:
            where.append("taxon_id = ?")
            params.append(taxon_id)
        cond = ("WHERE " + " AND ".join(where)) if where else ""

        try:
            with self._connect_main() as conn:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM observations {cond}", params).fetchone()[0]
                rows = conn.execute(
                    f"""SELECT observation_id, uri, common_name, scientific_name,
                               observed_on, place_guess, observer_login, photo_count
                          FROM observations {cond}
                         ORDER BY observed_on DESC, observation_id DESC
                         LIMIT 50 OFFSET ?""", params + [self._obs_offset]).fetchall()
        except sqlite3.Error as exc:
            messagebox.showerror("查询失败", str(exc))
            return

        self.obs_tree.delete(*self.obs_tree.get_children())
        for r in rows:
            self.obs_tree.insert("", tk.END, values=(
                r["observation_id"], r["common_name"], r["scientific_name"],
                r["observed_on"] or "—", r["place_guess"] or "—",
                r["observer_login"] or "—", r["photo_count"] or 0))
        page_no = self._obs_offset // 50 + 1
        total_pages = max(1, -(-total // 50))
        self.obs_page_label.config(text=f"第 {page_no}/{total_pages} 页，共 {total} 条")

    def _open_observation_uri(self, event) -> None:
        """双击表格行：浏览器打开该观察记录。"""
        item = self.obs_tree.identify_row(event.y)
        if not item:
            return
        values = self.obs_tree.item(item, "values")
        with self._connect_main() as conn:
            row = conn.execute("SELECT uri FROM observations WHERE observation_id = ?",
                               (values[0],)).fetchone()
        if row and row["uri"]:
            webbrowser.open(row["uri"])

    # ================= 照片浏览 =================
    def _page_photos(self, direction: int) -> None:
        self._ph_offset = max(0, self._ph_offset + direction * 24)
        self._load_photos()

    def _load_photos(self) -> None:
        """加载照片缩略图网格（每页 24 张，按类群筛选）。"""
        taxon_id = self._parse_taxon_filter(self.ph_taxon_var.get())
        where, params = ["p.download_ok = 1"], []
        if taxon_id is not None:
            where.append("o.taxon_id = ?")
            params.append(taxon_id)
        cond = "WHERE " + " AND ".join(where)

        try:
            with self._connect_main() as conn:
                total = conn.execute(
                    f"""SELECT COUNT(*) FROM photos p
                          JOIN observations o ON o.observation_id = p.observation_id {cond}""",
                    params).fetchone()[0]
                rows = conn.execute(
                    f"""SELECT p.local_path, o.common_name, o.scientific_name, o.observed_on
                          FROM photos p
                          JOIN observations o ON o.observation_id = p.observation_id
                          {cond}
                         ORDER BY o.observed_on DESC, p.photo_id DESC
                         LIMIT 24 OFFSET ?""", params + [self._ph_offset]).fetchall()
        except sqlite3.Error as exc:
            messagebox.showerror("读取失败", str(exc))
            return

        # 清空旧内容
        for child in self.ph_inner.winfo_children():
            child.destroy()
        self._thumb_refs.clear()

        page_no = self._ph_offset // 24 + 1
        total_pages = max(1, -(-total // 24))
        self.ph_page_label.config(text=f"第 {page_no}/{total_pages} 页，共 {total} 张")

        if not rows:
            ttk.Label(self.ph_inner, text="该筛选条件下暂无照片",
                      padding=40, foreground="#8a95a1").grid(row=0, column=0)
            return

        for i, r in enumerate(rows):
            frame = ttk.Frame(self.ph_inner, padding=4)
            frame.grid(row=i // 4, column=i % 4, padx=4, pady=4)
            resolved = self._resolve_photo_path(r["local_path"])
            self._create_thumb_widget(frame, resolved, r)
            cap = f"{r['common_name'] or ''}\n{r['scientific_name'] or ''} {r['observed_on'] or ''}"
            ttk.Label(frame, text=cap, justify=tk.CENTER,
                      font=("Microsoft YaHei", 8)).pack()
        self.ph_canvas.yview_moveto(0)

    def _create_thumb_widget(self, parent: ttk.Frame,
                             path: Path | None, row: sqlite3.Row) -> None:
        """创建单个缩略图控件（有 Pillow 用图片，无则用按钮）。"""
        if path is None:
            ttk.Label(parent, text="文件缺失", foreground="#b06818",
                      padding=10).pack()
            return
        if HAS_PIL:
            try:
                img = Image.open(path)
                img.thumbnail((220, 165))
                thumb = ImageTk.PhotoImage(img)
                self._thumb_refs.append(thumb)
                label = ttk.Label(parent, image=thumb, cursor="hand2")
                label.pack()
                label.bind("<Double-1>", lambda e, p=path: os.startfile(str(p)))  # noqa: S606
                return
            except Exception:
                pass  # 图片损坏时退化为按钮
        ttk.Button(parent, text=path.name[:24],
                   command=lambda p=path: os.startfile(str(p))).pack()  # noqa: S606

    # ================= 统计与说明 =================
    def _load_stats(self) -> None:
        """汇总数据库统计并展示设计说明。"""
        lines = ["=== 数据库统计 ===\n"]
        try:
            with self._connect_main() as conn:
                one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
                lines.append(f"观察记录总数: {one('SELECT COUNT(*) FROM observations')}")
                lines.append(f"照片总数:      {one('SELECT COUNT(*) FROM photos')}")
                lines.append(f"类群数:        {one('SELECT COUNT(*) FROM taxa')}")
                lines.append(f"下载批次:      {one('SELECT COUNT(*) FROM batch_runs')}\n")
                lines.append("--- 按类群统计 ---")
                rows = conn.execute(
                    """SELECT t.common_name, t.scientific_name, t.taxon_id,
                              COUNT(DISTINCT o.id) AS obs,
                              (SELECT COUNT(*) FROM photos p
                                 JOIN observations o2 ON o2.observation_id = p.observation_id
                                WHERE o2.taxon_id = t.taxon_id) AS photos
                         FROM taxa t LEFT JOIN observations o ON o.taxon_id = t.taxon_id
                        GROUP BY t.id ORDER BY obs DESC""").fetchall()
                for r in rows:
                    lines.append(f"  {r['common_name']} {r['scientific_name']} "
                                 f"(#{r['taxon_id']}): 观察 {r['obs']} 条, 照片 {r['photos']} 张")
                lines.append("\n--- 最近批次 ---")
                for b in conn.execute(
                        "SELECT * FROM batch_runs ORDER BY id DESC LIMIT 5").fetchall():
                    lines.append(f"  #{b['id']} {b['batch_time']} 类群 {b['taxa_succeeded']}/"
                                 f"{b['taxa_requested']} 观察 {b['total_observations']} "
                                 f"照片 {b['total_photos']}")
        except sqlite3.Error as exc:
            lines.append(f"读取失败: {exc}")

        lines.append("\n=== 数据库设计说明（遵循复用文档） ===")
        lines.append("1. 两层库：raw_data.db 原始留存库 + bird_observations.db 规范主库")
        lines.append("2. 去重：SHA-256 指纹（观察/照片各一）+ UNIQUE 约束 + upsert 冲突更新")
        lines.append("   - 观察指纹：物种名|观察日期|经纬度 等字段哈希")
        lines.append("   - 照片指纹：original_url|本地文件名 哈希，重复下载自动跳过")
        lines.append("3. 照片按种类分文件夹存储：data/taxon_<ID>_<名称>/photos/")
        lines.append("4. 本界面以只读方式访问主库，不影响爬虫写入")

        self.stats_text.config(state=tk.NORMAL)
        self.stats_text.delete("1.0", tk.END)
        self.stats_text.insert(tk.END, "\n".join(lines))
        self.stats_text.config(state=tk.DISABLED)

    # ================= 关闭处理 =================
    def _on_close(self) -> None:
        """关闭窗口时终止仍在运行的下载/拖入处理/训练子进程。"""
        if self.proc and self.proc.poll() is None:
            if not messagebox.askokcancel("退出", "下载仍在进行，确定终止并退出？"):
                return
            self.proc.terminate()
        if self.drop_proc and self.drop_proc.poll() is None:
            self.drop_proc.terminate()
        if self.train_proc and self.train_proc.poll() is None:
            if not messagebox.askokcancel("退出", "模型训练仍在进行，确定终止并退出？"):
                return
            self.train_proc.terminate()
        # 释放训练子进程引用，避免残留 Popen 对象持有管道
        self.train_proc = None
        # 关闭视觉问答服务子进程（服务模式时存在；幂等，未启动则空操作）
        if self._vision_panel is not None and hasattr(self._vision_panel, "shutdown"):
            try:
                self._vision_panel.shutdown()
            except Exception:
                pass
        self.root.destroy()


def run() -> int:
    """启动桌面界面（供 main.py 调用）。"""
    root = tk.Tk()
    BirdCrawlerGUI(root)
    root.mainloop()
    return 0


def main() -> int:
    """独立运行入口。"""
    return run()


if __name__ == "__main__":
    sys.exit(main())
