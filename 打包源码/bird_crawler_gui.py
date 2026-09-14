#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================


from __future__ import annotations

import os
import queue
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk


def _v5e1f9a2b() -> Path:
    # 打包为 exe 后返回 exe 所在目录，源码运行时返回本文件所在目录
    import sys as _sys
    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# 路径均相对脚本目录解析，避免硬编码
_vd4ca91d7 = _v5e1f9a2b()
_va1aa2a9b = _vd4ca91d7 / "data"
_va6de3477 = _va1aa2a9b / "db"
_v39e35af8 = _va6de3477 / "raw_data.db"
_v29bac30f = _va6de3477 / "bird_observations.db"

# Pillow 为可选依赖：存在则显示缩略图，否则显示文件名按钮（不强制安装）
try:
    from PIL import Image, ImageTk  # type: ignore
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# 默认鸟类类群（与 batch_download_birds.py 的 DEFAULT_TAXA 保持一致）
_vd7707025 = [
    (574, "雉科"), (6912, "鸭科"), (5067, "鹰科"), (19728, "鸱鸮科"),
    (17599, "啄木鸟科"), (2715, "鸠鸽科"), (1627, "杜鹃科"), (13685, "雀科"),
    (12704, "鹟科"), (15977, "鸫科"), (4929, "鹭科"), (4342, "鸥科"),
    (3835, "鹬科"), (154, "秧鸡科"), (20967, "戴胜科"),
]


class _vbdfec7ce:

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None       # 爬虫子进程
        self.log_queue: queue.Queue = queue.Queue()      # 子进程日志队列
        self.drop_proc: subprocess.Popen | None = None   # 拖入处理子进程
        self.drop_queue: queue.Queue = queue.Queue()     # 拖入处理日志队列
        self.photo_index: dict | None = None             # 照片文件名索引缓存
        self._thumb_refs: list = []                      # 防止缩略图被垃圾回收
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
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================= 界面构建 =================
    def _build_banner(self) -> None:
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
        bar = ttk.Frame(self.root, padding=(10, 3))
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(bar, text=f"数据库：{_v29bac30f}",
                  foreground="#6b7683").pack(side=tk.LEFT)
        ttk.Label(bar, text="就绪", foreground="#6b7683").pack(side=tk.RIGHT)

    def _build_notebook(self) -> None:
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
        for tid, name in _vd7707025:
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
                   command=lambda: os.startfile(str(_va1aa2a9b))).pack(side=tk.LEFT, padx=6)  # noqa: S606

        # 右侧：运行日志
        right = ttk.Frame(page, padding=(0, 8, 8, 8))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(right, text="运行日志（含去重统计：new=新增，existed=指纹重复跳过）").pack(anchor=tk.W)
        self.log_text = scrolledtext.ScrolledText(right, height=20, state=tk.DISABLED,
                                                  font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=4)

    # ---------- 标签页 2：拖入文件爬取 ----------
    def _build_drop_tab(self) -> None:
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
        path = filedialog.askdirectory(title="选择图片文件夹（按文件夹名解析种类）")
        if path:
            self._drop_paths = [Path(path)]
            self.drop_file_var.set(path)
            self.drop_start_btn.config(state=tk.NORMAL)

    def _drop_common_args(self) -> list[str]:
        extra: list[str] = ["--max-per-taxon", str(self.drop_max_var.get()),
                            "--workers", str(self.drop_workers_var.get()),
                            "--photo-size", self.drop_size_var.get(),
                            "--quality-grade", self.drop_grade_var.get()]
        taxon = self.drop_taxon_var.get().strip()
        if taxon:
            extra += ["--taxon", taxon]
        return extra

    def _spawn_drop_proc(self, extra: list[str]) -> None:
        if self.drop_proc and self.drop_proc.poll() is None:
            messagebox.showinfo("提示", "拖入处理进程正在运行中，请先停止。")
            return
        cmd = [sys.executable, "drop", *extra,
               *self._drop_common_args(), "--inbox", str(_va1aa2a9b / "drop_inbox")]
        # 子进程用 UTF-8 输出，避免中文报告乱码
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.drop_proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env, cwd=str(_vd4ca91d7))
        self._append_drop_report(f"\n===== 启动拖入处理：{' '.join(cmd[2:])} =====\n")
        self.drop_stop_btn.config(state=tk.NORMAL)
        threading.Thread(target=self._read_drop_output, daemon=True).start()

    def _start_drop_file(self) -> None:
        paths = getattr(self, "_drop_paths", [])
        if not paths:
            messagebox.showinfo("提示", "请先选择文件或文件夹。")
            return
        self._spawn_drop_proc(["--file", *[str(p) for p in paths]])

    def _toggle_drop_watch(self) -> None:
        if self.drop_proc and self.drop_proc.poll() is None:
            self._stop_drop()
            return
        self._spawn_drop_proc(["--watch"])
        self.drop_watch_state.config(text="监视中：等待放入文件…", foreground="#1d7a34")
        self.drop_watch_btn.config(text="停止监视")

    def _stop_drop(self) -> None:
        if self.drop_proc and self.drop_proc.poll() is None:
            self.drop_proc.terminate()
            self._append_drop_report("[用户] 已请求终止拖入处理进程。\n")

    def _open_drop_inbox(self) -> None:
        inbox = _va1aa2a9b / "drop_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        os.startfile(str(inbox))  # noqa: S606

    def _read_drop_output(self) -> None:
        assert self.drop_proc is not None and self.drop_proc.stdout is not None
        for line in self.drop_proc.stdout:
            self.drop_queue.put(line)
        self.drop_queue.put("\n[拖入处理进程已结束]\n")

    def _poll_drop_queue(self) -> None:
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
        self.drop_report.config(state=tk.NORMAL)
        self.drop_report.insert(tk.END, text)
        self.drop_report.see(tk.END)
        self.drop_report.config(state=tk.DISABLED)

    # ---------- 标签页 3：数据浏览 ----------
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

    # ---------- 标签页 4：照片浏览 ----------
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

    # ---------- 标签页 5：统计与说明 ----------
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
        if not _v29bac30f.exists() or not _v39e35af8.exists():
            try:
                from inaturalist_db_initializer import _v31e07de3
                _v31e07de3(str(_v39e35af8), str(_v29bac30f))
                self._append_log(f"[初始化] 已自动创建数据库：{_v29bac30f.parent}\n")
            except Exception as exc:
                messagebox.showerror("数据库初始化失败", str(exc))
        self._refresh_taxon_filters()

    def _connect_main(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{_v29bac30f.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _refresh_taxon_filters(self) -> None:
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
        if not text or text == "全部类群":
            return None
        try:
            return int(text.split()[0])
        except (ValueError, IndexError):
            return None

    def _resolve_photo_path(self, local_path: str) -> Path | None:
        if not local_path:
            return None
        path = Path(local_path)
        if path.is_absolute() and path.is_file():
            return path
        candidate = _va1aa2a9b / path
        if candidate.is_file():
            return candidate
        # 相对类群文件夹的路径（photos\\obs_xxx.jpg）→ 按文件名全盘索引
        if self.photo_index is None:
            index: dict = {}
            for item in _va1aa2a9b.rglob("*"):
                if item.is_file() and item.suffix.lower() in (
                        ".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    index.setdefault(item.name, item)
            self.photo_index = index
        return self.photo_index.get(path.name)

    # ================= 下载控制 =================
    def _start_download(self) -> None:
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

        cmd = [sys.executable, "batch"]
        if taxon_ids:
            cmd += ["--taxon-ids", *[str(t) for t in taxon_ids]]
        cmd += [
            "--max-per-taxon", str(self.max_var.get()),
            "--photo-size", self.size_var.get(),
            "--quality-grade", self.grade_var.get(),
            "--workers", str(self.workers_var.get()),
            "--out-dir", str(_va1aa2a9b),
            "--db-dir", str(_va6de3477),
        ]

        # 子进程用 UTF-8 输出，避免中文日志乱码
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env, cwd=str(_vd4ca91d7))

        self._append_log(f"\n===== 启动下载：{' '.join(cmd[2:])} =====\n")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        threading.Thread(target=self._read_proc_output, daemon=True).start()

    def _read_proc_output(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.log_queue.put(line)
        self.log_queue.put("\n[下载进程已结束]\n")

    def _stop_download(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._append_log("[用户] 已请求终止下载进程。\n")

    def _poll_log_queue(self) -> None:
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
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ================= 数据浏览 =================
    def _search_observations(self) -> None:
        self._obs_offset = 0
        self._load_observations()

    def _page_observations(self, direction: int) -> None:
        self._obs_offset = max(0, self._obs_offset + direction * 50)
        self._load_observations()

    def _load_observations(self) -> None:
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
        if self.proc and self.proc.poll() is None:
            if not messagebox.askokcancel("退出", "下载仍在进行，确定终止并退出？"):
                return
            self.proc.terminate()
        if self.drop_proc and self.drop_proc.poll() is None:
            self.drop_proc.terminate()
        self.root.destroy()


def _v7dab2a83() -> int:
    root = tk.Tk()
    _vbdfec7ce(root)
    root.mainloop()
    return 0


def _v9a12dab4() -> int:
    return _v7dab2a83()


if __name__ == "__main__":
    sys.exit(_v9a12dab4())
