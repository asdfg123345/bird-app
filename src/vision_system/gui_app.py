# -*- coding: utf-8 -*-
"""
gui_app.py —— tkinter 桌面界面：模型选择 / 选图预览 / 提问识别 / 结果展示

作者：天咏（明月照今日，风摇过两川。）

说明：
1. 全项目仅本文件导入 tkinter；main.py 的 gui 分支延迟导入本模块，
   云端训练环境不 import gui_app 即无 GUI 依赖；
2. 全部界面与交互逻辑封装在可嵌入面板 BirdVqaPanel(tk.Frame) 中，
   可放入任意父容器复用；BirdVqaApp(tk.Tk) 仅作为独立窗口包壳；
3. 界面分区架构：图片区（选图 / 图库联动 / 预览）常驻可用，不依赖任何
   模型环境；视觉问答区（模型选择 / 提问 / 标记）默认收起，用户勾选
   「启用视觉问答」后才首次加载模型或启动问答服务（懒加载），加载
   失败仅影响问答，选图浏览等基础功能不受影响；
4. 识别过程在子线程执行，结果经 panel.after 回到主线程刷新界面；
5. 窗口标题、默认模型、默认标记样式等一律取自 config，无硬编码；
6. 物种模型下拉防御性接入 config.get_all_species_models()（出厂+共享库
   合并注册表）：该接口尚不存在或调用失败时自动退回 config.SPECIES_MODELS。
"""

import hashlib
import json
import os
import queue
import sqlite3
import threading
import tkinter as tk
from pathlib import Path
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

# ============ 图库联动常量（iNaturalist 爬取图库 → 视觉分析） ============

# 环境变量：显式指定图库数据目录（其下应含 db/bird_observations.db），优先级最高
GALLERY_DATA_DIR_ENV_VAR = "BIRD_GALLERY_DATA_DIR"

# 图库列表单次最多渲染条数（图库可达两万+记录，防止列表卡顿）
GALLERY_MAX_SHOW = 800

# 图库文件名索引支持识别的图片后缀（与控制台全盘索引口径一致）
_GALLERY_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _format_species_rows(rows):
    """物种识别结果格式化为中文文本（rows: [(中文名, 置信度 0~1)]）。"""
    if not rows:
        return "物种识别：无结果"
    lines = []
    for idx, (label, conf) in enumerate(rows):
        rank = "Top-1" if idx == 0 else f"Top-{idx + 1}"
        lines.append(f"{rank}：{label}（{conf * 100:.1f}%）")
    return "\n".join(lines)


class _UiMarshaller:
    """工作线程 → UI 线程的回调投递器（队列 + 主线程定时轮询）。

    tkinter 的 after() 只允许创建 Tk 解释器的主线程调用，工作线程直接
    调用会抛 RuntimeError（"main thread is not in main loop"）。本类把
    回调放入线程安全队列，由主线程每 100ms 轮询执行，任何线程均可安全
    投递界面刷新任务；容器销毁后轮询自动停止（TclError 静默忽略）。
    """

    def __init__(self, master):
        self._queue = queue.Queue()    # 待执行的 (回调, 参数) 队列
        self._master = master          # 关联的 tk 容器（面板或对话框）
        self._polling = False          # 轮询是否已启动（只启动一次）

    def post(self, func, *args):
        """工作线程调用：把回调放入队列，等待主线程执行。"""
        self._queue.put((func, args))

    def start(self):
        """在主线程启动轮询（容器构建时调用一次）。"""
        if not self._polling:
            self._polling = True
            self._master.after(100, self._poll)

    def _poll(self):
        """主线程定时执行：取出并运行队列中的回调，然后继续排队。"""
        try:
            while True:
                func, args = self._queue.get_nowait()
                try:
                    func(*args)
                except tk.TclError:
                    pass           # 控件已销毁等场景：静默丢弃
        except queue.Empty:
            pass
        try:
            self._master.after(100, self._poll)
        except tk.TclError:
            pass                   # 容器已销毁：停止轮询


def _gallery_cache_path(db_path):
    """图库文件名索引的缓存文件路径（按数据库路径哈希区分，避免互相覆盖）。

    缓存放在本目录 gallery_index_cache 文件夹内，不写入图库数据目录，
    避免污染控制台数据；全盘扫描约需 1 分钟，缓存后再次打开近乎秒开。
    """
    digest = hashlib.md5(str(db_path).encode("utf-8")).hexdigest()[:12]
    return Path(__file__).resolve().parent / "gallery_index_cache" / \
        f"gallery_index_{digest}.json"


def _find_gallery_db():
    """定位 iNaturalist 图库主数据库 bird_observations.db；找不到返回 None。

    探测顺序（不硬编码盘符路径，全部基于本文件位置与环境变量推导）：
      1. 环境变量 BIRD_GALLERY_DATA_DIR 指定的数据目录（须存在 db/主库）；
      2. 视觉系统目录上一级的 data/db/bird_observations.db（安装包内置布局）；
      3. 自上两级目录开始向上逐级通配兜底（最多 3 级），优先 iNaturalist
         项目库，其次通用 data 库；sorted 保证多命中时结果确定。

    候选可能不止一个（如安装包内置的空库与开发项目库并存），故按优先级
    依次检查记录数，返回第一个「存在已下载照片」的库；全部为空时返回
    优先级最高的已存在库（保证打开对话框能看到"图库为空"的明确提示，
    而不是误报"未找到数据库"）。
    """
    here = Path(__file__).resolve()
    candidates = []                        # 按优先级排列的候选库路径
    # ① 环境变量显式指定
    env_value = os.environ.get(GALLERY_DATA_DIR_ENV_VAR, "").strip()
    if env_value:
        candidates.append(Path(env_value) / "db" / "bird_observations.db")
    # ② 固定相对候选：安装包/源码两种布局的上一级 data 目录
    candidates.append(here.parent.parent / "data" / "db" / "bird_observations.db")
    # ③ 向上逐级通配兜底（覆盖发布包多层嵌套、独立项目库等场景）
    for ancestor in here.parents[2:5]:
        try:
            for pattern in ("*/iNaturalist*/data/db/bird_observations.db",
                            "*/data/db/bird_observations.db"):
                candidates.extend(sorted(ancestor.glob(pattern)))
        except OSError:
            continue  # 目录不可枚举时跳过该级
    # 按优先级去重并检查：返回第一个有已下载照片的库
    seen, existing = set(), []
    for cand in candidates:
        if cand in seen or not cand.is_file():
            continue
        seen.add(cand)
        existing.append(cand)
        try:
            conn = sqlite3.connect(f"file:{cand.as_posix()}?mode=ro", uri=True)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM photos WHERE download_ok = 1").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            continue  # 库损坏/表缺失时按无效候选跳过
        if count > 0:
            return cand
    # 全部候选为空库：返回优先级最高的已存在库（对话框会提示图库为空）
    return existing[0] if existing else None


class GalleryPickerDialog(tk.Toplevel):
    """iNaturalist 图库选图对话框。

    从主数据库只读读取下载成功的照片记录，将数据库相对路径解析为实际
    文件（绝对路径 → 数据目录拼接 → 文件名全盘索引三级回退，与控制台
    口径一致）；支持中文名/学名/文件名关键字过滤，双击或按钮载入选中
    图片。全部耗时操作（读库、建索引）在后台线程执行，界面不卡顿。
    """

    def __init__(self, master, db_path, on_pick):
        super().__init__(master)
        self.title("从图库选图 —— iNaturalist 爬取图库")
        self.transient(master)
        self.grab_set()                  # 模态对话框：选图期间阻塞主面板
        self.geometry("760x480")
        self.minsize(560, 360)

        self._db_path = db_path          # 主数据库路径
        self._on_pick = on_pick          # 选中回调：on_pick(图片路径)
        self._records = []               # 全部记录 [(显示文本, 解析路径或 None)]
        self._shown_records = []         # 当前列表渲染的记录（与列表项一一对应）
        self._data_dir = db_path.parent.parent  # 数据目录（db 目录的上一级）
        self._cache_path = _gallery_cache_path(db_path)  # 文件名索引缓存文件
        # 线程 → UI 回调投递器：读库与建索引在后台线程执行，
        # 结果经队列回主线程刷新列表（after 不允许跨线程调用）
        self._ui = _UiMarshaller(self)
        self._ui.start()

        self._build_widgets()
        self._load_records()

    # ---------- 界面构建 ----------

    def _build_widgets(self):
        """构建对话框界面：搜索栏 / 列表区 / 底部按钮与状态栏。"""
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="关键字过滤（中文名 / 学名 / 文件名）：").pack(side="left")
        self._search_var = tk.StringVar()
        search_entry = ttk.Entry(top, textvariable=self._search_var, width=32)
        search_entry.pack(side="left", padx=6)
        search_entry.bind("<KeyRelease>", lambda _e: self._refresh_list())
        ttk.Button(top, text="清空", command=self._clear_search).pack(side="left")

        mid = ttk.Frame(self, padding=(6, 0))
        mid.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(mid)
        scrollbar.pack(side="right", fill="y")
        self._listbox = tk.Listbox(mid, selectmode="browse",
                                   yscrollcommand=scrollbar.set)
        self._listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self._listbox.yview)
        self._listbox.bind("<Double-1>", lambda _e: self._pick_selected())

        bottom = ttk.Frame(self, padding=6)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="载入选中图片",
                   command=self._pick_selected).pack(side="left")
        self._count_var = tk.StringVar(value="正在读取图库记录...")
        ttk.Label(bottom, textvariable=self._count_var).pack(side="left", padx=10)
        self._status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._status_var, anchor="w",
                  relief="sunken", padding=(6, 2)).pack(fill="x", side="bottom")

    # ---------- 数据读取与路径解析（后台线程） ----------

    def _load_records(self):
        """后台线程读取数据库记录并解析照片路径，完成后回主线程刷新列表。"""
        def worker():
            try:
                records, missing = self._read_and_resolve()
            except Exception as exc:
                self._ui.post(self._on_load_failed, str(exc))
                return
            self._ui.post(self._on_records_ready, records, missing)
        threading.Thread(target=worker, daemon=True).start()

    def _read_and_resolve(self):
        """只读读取下载成功照片记录并解析实际路径。

        返回 (记录列表, 解析失败条数)；记录元素为 (显示文本, 路径或 None)。
        数据库相对路径（如 photos\\obs_xxx.jpg）可能是归档前的旧路径，
        因此直拼未命中时按文件名全盘索引回退（与控制台口径一致）。
        """
        conn = sqlite3.connect(
            f"file:{self._db_path.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT p.local_path, o.common_name, o.scientific_name "
                "FROM photos p LEFT JOIN observations o "
                "  ON p.observation_id = o.observation_id "
                "WHERE p.download_ok = 1 AND p.local_path IS NOT NULL "
                "ORDER BY p.local_path").fetchall()
        finally:
            conn.close()

        # 第一遍：绝对路径或数据目录直拼即可命中的直接解析
        entries = []
        need_index = False
        for local_path, common, sci in rows:
            path = self._resolve_direct(local_path)
            if path is None:
                need_index = True
            entries.append([str(local_path), str(common or ""), str(sci or ""), path])
        # 存在未命中记录时启用文件名索引（优先读缓存，无缓存才全盘扫描）
        if need_index:
            index = self._load_or_build_index(len(rows))
            for entry in entries:
                if entry[3] is None:
                    entry[3] = index.get(Path(entry[0]).name)
        # 组装展示记录
        records = []
        missing = 0
        for local_path, common, sci, path in entries:
            if path is None:
                missing += 1
            label = f"{common or '未命名'}  {sci}  ｜ {Path(local_path).name}"
            records.append((label, path))
        return records, missing

    def _resolve_direct(self, local_path):
        """直接解析：绝对路径或数据目录拼接，命中返回 Path，否则 None。"""
        path = Path(local_path)
        if path.is_absolute():
            return path if path.is_file() else None
        candidate = self._data_dir / path
        if candidate.is_file():
            return candidate
        return None

    def _build_name_index(self):
        """遍历数据目录按文件名建索引（同名文件取首个）；线程内调用。"""
        index = {}
        try:
            for item in self._data_dir.rglob("*"):
                if item.is_file() and item.suffix.lower() in _GALLERY_IMAGE_SUFFIXES:
                    index.setdefault(item.name, item)
        except OSError:
            pass  # 目录不可枚举时按空索引处理
        return index

    def _load_or_build_index(self, row_count):
        """加载文件名索引：缓存命中直接用，否则全盘扫描并写入缓存。

        缓存键包含数据库修改时间/大小与记录数：重新爬取导致库变化时
        自动重建索引。线程内调用。
        """
        try:
            cache_key = (f"{self._db_path.stat().st_mtime_ns}-"
                         f"{self._db_path.stat().st_size}-{row_count}")
        except OSError:
            cache_key = f"unknown-{row_count}"
        index = self._load_index_cache(cache_key)
        if index is not None:
            self._ui.post(self._status_var.set, "已加载图库索引缓存")
            return index
        self._ui.post(self._status_var.set,
                      "正在按文件名建立图库索引（首次较慢，完成后自动缓存）...")
        index = self._build_name_index()
        self._save_index_cache(cache_key, index)
        return index

    def _load_index_cache(self, cache_key):
        """读取文件名索引缓存；键不匹配或损坏时返回 None（触发重建）。"""
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("key") != cache_key:
            return None
        index_part = data.get("index")
        if not isinstance(index_part, dict):
            return None
        # 缓存存的是相对数据目录的路径，此处还原；文件是否仍存在
        # 延迟到"载入选中"时校验，避免加载阶段逐个 stat 拖慢速度
        return {name: self._data_dir / rel
                for name, rel in index_part.items()}

    def _save_index_cache(self, cache_key, index):
        """保存文件名索引缓存（路径存相对形式，便于整体迁移）。

        保存失败仅影响下次打开速度，不影响本次功能，静默忽略。
        """
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "key": cache_key,
                "index": {name: str(path.relative_to(self._data_dir))
                          for name, path in index.items()},
            }
            self._cache_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError):
            pass

    # ---------- 列表刷新与选中载入 ----------

    def _on_records_ready(self, records, missing):
        """记录加载完成：填充数据、刷新列表并提示统计结果。"""
        self._records = records
        self._refresh_list()
        ok_cnt = len(records) - missing
        self._count_var.set(f"共 {len(records)} 张照片，已定位 {ok_cnt} 张")
        if missing:
            self._status_var.set(
                f"有 {missing} 张照片的文件已不在图库目录中（可能被移动或删除）")
        else:
            self._status_var.set("就绪：双击或点击“载入选中图片”")

    def _on_load_failed(self, exc_text):
        """记录读取失败：给出中文提示并保持对话框可关闭。"""
        self._count_var.set("图库读取失败")
        self._status_var.set(f"读取图库数据库失败：{exc_text}")
        messagebox.showerror("图库读取失败",
                             f"读取图库数据库失败：\n{exc_text}", parent=self)

    def _refresh_list(self):
        """按关键字过滤并渲染列表（限制最多渲染条数防卡顿）。"""
        self._listbox.delete(0, "end")
        self._shown_records = []
        keyword = self._search_var.get().strip().lower()
        for label, path in self._records:
            if keyword and keyword not in label.lower():
                continue
            self._listbox.insert("end", label)
            self._shown_records.append((label, path))
            if len(self._shown_records) >= GALLERY_MAX_SHOW:
                self._listbox.insert(
                    "end", f"……（仅显示前 {GALLERY_MAX_SHOW} 条，请用关键字缩小范围）")
                break

    def _clear_search(self):
        """清空关键字并刷新列表。"""
        self._search_var.set("")
        self._refresh_list()

    def _pick_selected(self):
        """载入当前选中记录对应的图片；文件失效时给出中文提示。"""
        sel = self._listbox.curselection()
        if not sel:
            self._status_var.set("请先在列表中选中一张照片")
            return
        label, path = self._shown_records[sel[0]]
        if path is None or not path.is_file():
            self._status_var.set("该照片文件已不存在（可能被移动或删除），请换一张")
            return
        self.destroy()
        self._on_pick(path)


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

    def request(self, payload, timeout=1800, on_progress=None):
        """发送一个请求并等待最终响应；超时/进程退出/协议异常均抛中文异常。

        服务在最终响应之前可能发送零到多行进度事件
        （{"event": "progress", "stage", "current", "total"}），
        提供 on_progress(stage, current, total) 回调时逐行转发给调用方
        用于推进界面进度条，直到读到无 event 字段的最终响应行为止。
        """
        import subprocess
        with self._lock:
            if not self.running:
                raise RuntimeError("视觉服务进程未运行")
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    code = self._proc.poll()
                    raise RuntimeError(f"视觉服务进程已退出（退出码 {code}）")
                try:
                    data = json.loads(line)
                except ValueError:
                    raise RuntimeError("视觉服务响应不是合法 JSON")
                if isinstance(data, dict) and data.get("event") == "progress":
                    if on_progress is not None:
                        try:
                            on_progress(data.get("stage", ""),
                                        data.get("current"),
                                        data.get("total"))
                        except Exception:
                            pass           # 进度回调异常不影响请求
                    continue                # 进度事件不是最终响应，继续读
                return data

    def models(self):
        """拉取物种/部位模型下拉数据（含可用性标记）。"""
        return self.request({"cmd": "models"})

    def preload(self, on_progress=None):
        """请求服务端预热当前激活的物种与部位模型。"""
        return self.request({"cmd": "preload"}, on_progress=on_progress)

    def answer(self, image_path, question, marker_style, species_model,
               part_model, on_progress=None):
        """执行一次问答；返回 qa_server 的响应字典。"""
        return self.request({
            "cmd": "answer",
            "image_path": image_path,
            "question": question,
            "marker_style": marker_style or "",
            "species_model": species_model or "",
            "part_model": part_model or "",
        }, on_progress=on_progress)

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
        self._interactive_rects = []   # 结果图上可点击的部位框（画布坐标 + 部位信息）
        self._detect_button = None     # 「部位检测」按钮（问答区构建后存在）
        self._ask_button = None        # 「提问」按钮（问答区构建后存在）
        self._clear_dialog_button = None  # 「清空对话」按钮（问答区构建后存在）
        self._worker_running = False   # 识别线程是否运行中
        # 服务模式：提供 qa_service_cmd 时，模型推理整体在训练环境子进程中
        # 运行（控制台 exe 无 torch），面板只负责界面与 JSON 协议转发
        self._service = None           # QaServiceClient（服务模式时创建）
        self._service_cmd = list(qa_service_cmd) if qa_service_cmd else None

        # 问答引擎懒加载状态：未勾选启用前，模型注册表/服务进程一概不
        # 初始化（两者均会引入 torch），选图、预览、图库联动等基础功能
        # 在任何 Python 环境下都可直接使用
        self._qa_enabled = False        # 用户是否勾选启用问答
        self._qa_ready = False          # 问答引擎是否已完成初始化
        self._qa_initializing = False   # 问答引擎初始化是否进行中
        self._qa_box_built = False      # 问答区界面是否已构建
        self._species_pending = False   # 物种识别是否在等引擎就绪后继续
        self._species_options_loading = False  # 图片区模型库列表是否加载中
        self._preloading = False        # 模型后台预热是否进行中
        self._local_registry_error = None
        self._species_options = []      # 模型下拉选项（引擎初始化后填充）
        self._part_options = []
        # 线程 → UI 回调投递器：引擎初始化与识别均在后台线程执行，
        # 结果经队列回主线程刷新界面（after 不允许跨线程调用）
        self._ui = _UiMarshaller(self)

        self._build_widgets()
        self._ui.start()
        self._set_status("就绪：可先选择图片；需要部位检测或问题互动时请勾选「启用视觉问答」")

    # ---------- 问答引擎懒加载 ----------

    def _init_qa_engine(self):
        """首次勾选启用问答时调用：按模式在后台线程初始化引擎（懒加载）。

        服务模式：启动 qa_server 子进程并拉取模型下拉数据；
        本地模式：构建模型下拉选项（内部 import torch，缺失即失败）。
        初始化结果经 after 回主线程处理，期间界面给出中文进度提示。
        """
        self._qa_initializing = True
        self._set_status("问答引擎初始化中（首次加载模型较慢，请稍候）...")
        self._progress_busy()          # 初始化时长不定：进度条滚动动画
        self._populate_qa_box(loading=True)   # 先展示问答骨架与初始化提示
        threading.Thread(target=self._qa_init_worker, daemon=True).start()

    def _qa_init_worker(self):
        """初始化子线程：按服务模式/本地模式分别完成引擎就绪探测。"""
        try:
            if self._service_cmd:
                import os
                client = QaServiceClient(
                    self._service_cmd,
                    cwd=os.path.dirname(self._service_cmd[-1]))
                client.start()
                payload = client.models()
                if not payload.get("ok"):
                    raise RuntimeError(payload.get("error", "模型列表获取失败"))
                self._service = client
                self._ui.post(self._on_qa_init_done, payload)
            else:
                # 构建下拉选项需 import torch：无 torch 环境在此抛异常，
                # 由统一失败回调转为中文指引（不影响选图等基础功能）
                species_options = self._build_species_options()
                part_options = self._build_options(config.PART_MODELS)
                self._ui.post(self._on_qa_local_ready,
                              species_options, part_options)
        except Exception as exc:
            self._ui.post(self._on_qa_init_failed, str(exc))

    def _on_qa_init_done(self, payload):
        """服务模式初始化完成：保存下拉数据并构建问答区界面。"""
        self._qa_initializing = False
        self._qa_ready = True
        self._progress_finish()
        self._species_options = [(n, d) for n, d, _a in payload.get("species", [])]
        self._part_options = [(n, d) for n, d, _a in payload.get("part", [])]
        self._finish_qa_box()

    def _on_qa_local_ready(self, species_options, part_options):
        """本地模式初始化完成：保存下拉数据并构建问答区界面。"""
        self._qa_initializing = False
        self._qa_ready = True
        self._progress_finish()
        self._species_options = species_options
        self._part_options = part_options
        self._finish_qa_box()

    def _finish_qa_box(self):
        """引擎就绪后：填充图片区下拉框；若用户仍勾选着启用，再填充问答区。"""
        pending_species = self._species_pending   # 物种识别在等引擎就绪
        self._species_pending = False
        # 图片区物种下拉与问答开关无关：引擎就绪即刷新，保证不启用
        # 问答也能自主选择模型（否则下拉框永远停在"默认激活模型"）
        self._refresh_species_combo_values()
        if not self._qa_toggle_var.get():
            # 初始化期间用户已取消勾选：保留就绪状态，界面保持收起
            self._set_status("问答引擎已就绪（当前未启用，可随时勾选开启）")
            if pending_species:
                self._run_species_request()  # 继续被挂起的物种识别
            return
        self._populate_qa_box(loading=False)
        self._part_combo["values"] = [d for _, d in self._part_options]
        self._part_var.set(
            _default_display(self._part_options, config.ACTIVE_PART_MODEL))
        self._set_status("视觉问答已启用，可一键部位检测，或输入问题互动交流")
        self._start_model_preload()    # 后台预热模型，首次检测无需现场加载

    def _on_qa_init_failed(self, message):
        """引擎初始化失败：取消勾选、显示中文原因，基础功能不受影响。"""
        self._qa_initializing = False
        self._qa_ready = False
        self._progress_finish()
        self._species_pending = False       # 挂起的物种识别随失败一并取消
        self._qa_toggle_var.set(False)     # 回退为未启用状态
        self._populate_qa_box()            # 清空问答骨架（未启用不展示）
        self._result_text.delete("1.0", "end")
        self._result_text.insert(
            "1.0",
            f"视觉问答引擎启动失败：\n{message}\n\n"
            "处理办法：请到「模型训练」页确认 Python 解释器已指向本机训练环境"
            "（含 torch/transformers），然后重新勾选「启用视觉问答」重试。\n\n"
            "提示：选图、图库联动与预览不依赖问答引擎，可正常使用。")
        self._set_status("问答引擎初始化失败（选图与预览不受影响）")

    def shutdown(self):
        """关闭服务子进程（父容器销毁前调用，幂等）。"""
        if self._service is not None:
            self._service.close()
            self._service = None

    # ---------- 模型后台预热 ----------

    def _start_model_preload(self):
        """引擎就绪且问答启用后：后台预热激活模型，首次检测免现场加载。

        预热在独立线程进行，不阻塞界面；期间用户点检测/提问时请求会
        与预热串行排队（复用同一实例缓存），等待时间不劣于改造前。
        """
        if self._preloading or not self._qa_ready or not self._qa_enabled:
            return
        if self._service is None and self._service_cmd:
            return                      # 服务模式但服务未就绪：不预热
        self._preloading = True
        self._set_status("正在后台预热模型（首次加载较慢，完成后即可秒级检测）...")
        threading.Thread(target=self._preload_worker, daemon=True).start()

    def _preload_worker(self):
        """预热子线程：服务模式发 preload 命令，本地模式进程内加载。"""
        try:
            if self._service is not None:
                resp = self._service.preload()
                if not resp.get("ok"):
                    raise RuntimeError(resp.get("error", "未知原因"))
            else:
                # 本地模式：进程内加载激活的物种与部位模型（写入实例缓存）
                from part_locator import get_part_detector
                from species_classifier import get_species_backend
                get_species_backend()
                get_part_detector()
        except Exception:
            # 预热失败不致命：正式检测时按原逻辑加载/回退并给出完整提示
            self._ui.post(self._on_preload_failed)
            return
        self._ui.post(self._on_preload_done)

    def _on_preload_done(self):
        """预热完成：提示可立即检测（界面按钮全程未被禁用）。"""
        self._preloading = False
        if self._worker_running:
            return                      # 已有任务在跑：不打断其状态提示
        self._set_status("模型已预热完毕，可一键部位检测，或输入问题互动交流")

    def _on_preload_failed(self):
        """预热失败：静默降级（正式检测时再按原逻辑加载并提示原因）。"""
        self._preloading = False
        if not self._worker_running:
            self._set_status("模型预热未完成（不影响使用，首次检测时将自动加载）")

    # ---------- 可编程接口 ----------

    def set_image(self, path):
        """程序化指定待识别图片并刷新预览；返回加载是否成功。"""
        return self._load_image(path)

    def refresh_models(self):
        """重建模型下拉框选项（模型注册表变化后由父容器调用）。

        本地模式下构建选项需 import torch：缺失时保持现有选项不变，
        状态栏给出中文指引（不抛异常，避免父容器调用链崩溃）。
        """
        try:
            species_options = self._build_species_options()
            part_options = self._build_options(config.PART_MODELS)
        except Exception as exc:
            self._set_status(
                "模型下拉刷新失败（"
                + str(exc)
                + "）。请到「模型训练」页将解释器指向装有 torch 的 "
                "python.exe 后，切换其他页签再切回本页重试。")
            return
        self._species_options = species_options
        self._part_options = part_options
        self._refresh_species_combo_values()  # 同步图片区与问答区物种下拉
        if hasattr(self, "_species_combo"):   # 问答区未展开时仅刷新物种下拉
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
        """分区布局：左列图片区（常驻）+ 可开关问答区，右列结果区，底部状态栏。"""
        control = ttk.Frame(self, padding=10)
        control.grid(row=0, column=0, sticky="nsw", padx=(10, 5), pady=10)
        result = ttk.Frame(self, padding=10)
        result.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self._build_image_area(control)       # 图片区：常驻，不依赖问答引擎
        self._build_qa_toggle_area(control)   # 问答启用开关（默认关闭）
        self._qa_box = ttk.Frame(control)     # 问答区容器：启用后才填充
        self._build_status_area()

        ttk.Label(result, text="标记结果图（点击部位标记可查看名称）：").pack(anchor="w")
        # 画布承载标记图：标记图上的部位框已绘制成红色边框，此处按画布坐标
        # 登记可点击矩形，点击命中即在状态栏与对话区显示部位名称
        self._result_canvas = tk.Canvas(
            result, width=_RESULT_MAX_WIDTH, height=320,
            highlightthickness=0, bg="#f0f0f0")
        self._result_canvas.pack(anchor="w", pady=(0, 8))
        self._reset_result_canvas_placeholder()
        self._result_canvas.bind("<Button-1>", self._on_result_canvas_click)
        self._result_canvas.bind("<Motion>", self._on_result_canvas_motion)

        ttk.Label(result, text="回答文本：").pack(anchor="w")
        text_frame = ttk.Frame(result)
        text_frame.pack(fill="both", expand=True)
        self._result_text = tk.Text(text_frame, wrap="word", height=14)
        scroll = ttk.Scrollbar(text_frame, orient="vertical",
                               command=self._result_text.yview)
        self._result_text.configure(yscrollcommand=scroll.set)
        self._result_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ---------- 问答启用开关与问答区填充 ----------

    def _build_qa_toggle_area(self, parent):
        """问答启用开关：默认关闭，首次勾选时才懒加载问答引擎。"""
        box = ttk.Frame(parent)
        box.pack(fill="x", pady=(0, 10))
        self._qa_toggle_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            box, text="启用视觉问答（问题互动与部位检测，需加载模型，不启用时不加载）",
            variable=self._qa_toggle_var,
            command=self._on_qa_toggled).pack(anchor="w")

    def _on_qa_toggled(self):
        """勾选状态变化：勾选→首次懒加载引擎；取消→收起问答区。"""
        enabled = self._qa_toggle_var.get()
        self._qa_enabled = enabled
        if enabled:
            if not self._qa_ready and not self._qa_initializing:
                self._init_qa_engine()
                return              # 初始化完成回调里会继续填充问答区
            if self._qa_ready:
                self._populate_qa_box(loading=False)
            self._set_status("视觉问答已启用，可一键部位检测，或输入问题互动交流")
            self._start_model_preload()   # 引擎已就绪：后台预热（未预热过时）
        else:
            self._populate_qa_box()  # 收起问答区（清空容器即隐藏）
            self._set_status("视觉问答已关闭（选图与预览不受影响）")

    def _populate_qa_box(self, loading=False):
        """填充或收起问答区容器。

        loading=True  仅显示初始化进度提示（引擎加载中）；
        loading=False 构建完整问答区（模型选择 / 提问 / 标记样式 / 提问按钮）；
        默认（无参）  清空容器，即收起问答区。
        """
        for child in self._qa_box.winfo_children():
            child.destroy()
        self._qa_box_built = False
        if not self._qa_enabled and not loading:
            self._qa_box.pack_forget()  # 未启用：收起问答区容器
            return
        if loading:
            ttk.Label(self._qa_box,
                      text="问答引擎初始化中，完成后即可提问...",
                      foreground="#8a5a2a").pack(anchor="w", pady=6)
            self._qa_box.pack(fill="x", pady=(0, 10))
            return
        self._build_model_area(self._qa_box)
        self._build_question_area(self._qa_box)
        self._build_marker_area(self._qa_box)
        self._qa_box.pack(fill="x", pady=(0, 10))
        self._qa_box_built = True

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
        """选图区：选择按钮、模型选择、路径标签与预览区。"""
        box = ttk.LabelFrame(parent, text="图片", padding=8)
        box.pack(fill="x", pady=(0, 10))
        btn_row = ttk.Frame(box)
        btn_row.pack(anchor="w")
        ttk.Button(btn_row, text="选择图片",
                   command=self._choose_image).pack(side="left")
        # 图库联动：从 iNaturalist 爬取图库（数据库）中选图直接载入
        ttk.Button(btn_row, text="从图库选图",
                   command=self._choose_gallery_image).pack(side="left", padx=(6, 0))
        # 物种识别：加载图片后即可一键识别种类（独立于问答开关，不需提问）
        self._species_button = ttk.Button(btn_row, text="识别物种",
                                          command=self._on_species_identify)
        self._species_button.pack(side="left", padx=(6, 0))
        # 物种模型选择器：常驻显示、独立于问答开关；首次点开时才后台
        # 加载模型库列表（默认使用注册表激活模型，加载后可自主选择）
        model_row = ttk.Frame(box)
        model_row.pack(anchor="w", pady=(4, 0))
        ttk.Label(model_row, text="物种模型：").pack(side="left")
        self._img_species_var = tk.StringVar(value="（默认激活模型）")
        self._img_species_combo = ttk.Combobox(
            model_row, textvariable=self._img_species_var,
            state="readonly", width=42)
        self._img_species_combo.pack(side="left", padx=(6, 0))
        self._img_species_combo.bind(
            "<Button-1>", lambda _e: self._on_img_species_dropdown())
        self._path_label = ttk.Label(box, text="尚未选择图片", wraplength=340)
        self._path_label.pack(anchor="w", pady=(4, 0))
        self._preview_label = ttk.Label(box, text="（预览区）")
        self._preview_label.pack(anchor="w", pady=(6, 0))

    # ---------- 物种模型选择器（图片区常驻，懒加载模型库） ----------

    def _on_img_species_dropdown(self):
        """首次点开物种模型下拉框时，后台加载模型库合并注册表选项。"""
        if self._species_options or self._species_options_loading:
            return                      # 已加载或加载中：直接用现有列表
        if self._service_cmd and not self._qa_ready:
            # 服务模式：模型列表须由引擎子进程提供，走问答引擎初始化
            if not self._qa_initializing:
                self._init_qa_engine()
            return
        self._species_options_loading = True
        self._img_species_var.set("（模型库加载中…）")
        self._set_status("正在加载模型库物种模型列表（首次需数秒）...")
        threading.Thread(target=self._species_options_worker,
                         daemon=True).start()

    def _species_options_worker(self):
        """后台线程：构建「出厂 + 共享模型库」物种模型合并选项列表。

        服务模式（控制台进程无 torch）经 JSON 协议向问答子进程拉取
        models 列表；本地模式在进程内直接构建（需本机可 import torch）。
        """
        try:
            if self._service is not None:
                # 服务模式：模型注册表在训练环境子进程侧，走协议获取
                payload = self._service.models()
                if not payload.get("ok"):
                    raise RuntimeError(
                        payload.get("error") or "问答服务未返回模型列表")
                options = [(n, d)
                           for n, d, _a in payload.get("species", [])]
            else:
                # 本地模式：进程内构建合并注册表选项
                options = self._build_species_options()
        except Exception as exc:
            self._ui.post(self._on_species_options_failed, str(exc))
            return
        self._ui.post(self._on_species_options_ready, options)

    def _on_species_options_ready(self, options):
        """模型库选项加载完成：填充下拉框并恢复默认选中。"""
        self._species_options_loading = False
        self._species_options = options
        self._refresh_species_combo_values()
        self._set_status("模型库已加载，可自主选择物种模型")

    def _on_species_options_failed(self, message):
        """模型库加载失败：下拉框回退默认项并提示，识别仍可用激活模型。"""
        self._species_options_loading = False
        self._img_species_var.set("（默认激活模型）")
        self._set_status(f"模型库加载失败（{message}），将继续使用激活模型")

    def _refresh_species_combo_values(self):
        """用最新选项列表刷新图片区与问答区的物种模型下拉框。"""
        displays = [d for _, d in self._species_options]
        if not displays:
            return
        self._img_species_combo["values"] = displays
        self._img_species_var.set(
            _default_display(self._species_options, config.ACTIVE_SPECIES_MODEL))
        if hasattr(self, "_species_combo"):   # 问答区已展开时同步刷新
            self._species_combo["values"] = displays
            self._species_var.set(
                _default_display(self._species_options,
                                 config.ACTIVE_SPECIES_MODEL))

    def _build_question_area(self, parent):
        """问题区：输入框与灰色占位提示。"""
        box = ttk.LabelFrame(parent, text="问题互动", padding=8)
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
        # 动作按钮行：一键全部位检测（无需提问）/ 提问 / 清空对话
        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", pady=(4, 0))
        self._detect_button = ttk.Button(
            btn_row, text="部位检测（全部位）", command=self._on_detect_parts)
        self._detect_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._ask_button = ttk.Button(btn_row, text="提问",
                                      command=self._on_ask)
        self._ask_button.pack(side="left", fill="x", expand=True,
                              padx=(0, 4))
        self._clear_dialog_button = ttk.Button(
            btn_row, text="清空对话", command=self._on_clear_dialogue)
        self._clear_dialog_button.pack(side="left", fill="x", expand=True)

    def _build_status_area(self):
        """底部状态栏：状态文本 + 任务进度条。"""
        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
        self._status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self._status_var, anchor="w",
                  relief="sunken").pack(side="left", fill="x", expand=True)
        # 任务进度条：模型加载等无法计步的阶段用滚动动画，
        # 全部位检测等可计步任务按 当前步/总步数 推进
        self._progress_var = tk.DoubleVar(value=0.0)
        self._progress_bar = ttk.Progressbar(
            bar, mode="determinate", maximum=100.0,
            variable=self._progress_var, length=220)
        self._progress_bar.pack(side="right", padx=(8, 0))

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

    def _choose_gallery_image(self):
        """图库联动入口：从 iNaturalist 爬取图库（数据库）中选图载入面板。"""
        db_path = _find_gallery_db()
        if db_path is None:
            messagebox.showinfo(
                "未找到图库数据库",
                "未找到 iNaturalist 图库数据库（bird_observations.db）。\n\n"
                "处理方法（任选其一）：\n"
                "  1. 将环境变量 BIRD_GALLERY_DATA_DIR 指向图库数据目录\n"
                "     （其下应含 db/bird_observations.db）后重启程序；\n"
                "  2. 先在「下载控制」页爬取照片，再回到本页重试。")
            return
        # 选中后经 _load_image 载入：路径标签、预览与提问链路与手选完全一致
        GalleryPickerDialog(self, db_path, self._load_image)

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
        self._set_status("图片已加载；可点击「识别物种」，或勾选「启用视觉问答」进行部位检测与问题互动")
        return True

    # ---------- 物种识别（独立于问答开关，一键识别种类） ----------

    def _on_species_identify(self):
        """物种识别入口：使用「模型选择」下拉框中选中的物种模型识别种类。

        模型来源为「出厂 + 共享模型库」合并注册表（config.get_all_species_
        models()），模型库中新注册/设为激活的物种模型均可在此调用。
        """
        if self._worker_running:
            self._set_status("识别正在进行，请稍候...")
            return
        if not self._image_path:
            self._set_status("请先选择图片")
            return
        # 服务模式且引擎未就绪：先完成懒加载初始化，就绪后自动继续识别
        if self._service_cmd and not self._qa_ready:
            if self._qa_initializing:
                self._set_status("引擎初始化中，请稍候再试...")
                return
            self._species_pending = True
            self._init_qa_engine()
            return
        # 本地模式顺带懒加载模型库选项（不阻塞本次识别，激活模型先行）
        self._on_img_species_dropdown()
        self._run_species_request()

    def _run_species_request(self):
        """执行物种识别：服务模式经子进程协议，本地模式进程内后台分类。"""
        # 模型名取自图片区常驻「物种模型」下拉框（数据源为模型库合并
        # 注册表）；选项未加载时传 None 即用注册表激活模型
        species_name = None
        if self._species_options:
            species_name = self._registry_of(
                self._img_species_var.get(), self._species_options)
        self._worker_running = True
        self._species_button.config(state="disabled")
        self._set_status("正在识别物种（首次加载模型较慢，请稍候）...")
        self._progress_busy()          # 模型加载时长不定：先滚动动画
        threading.Thread(target=self._species_worker,
                         args=(self._image_path, species_name),
                         daemon=True).start()

    def _species_worker(self, image_path, species_name):
        """物种识别子线程：服务模式走 JSON 协议，本地模式进程内分类。"""
        progress_cb = self._make_progress_cb()
        try:
            if self._service is not None:
                # 服务模式：模型推理在训练环境子进程中执行
                resp = self._service.request({
                    "cmd": "species",
                    "image_path": str(image_path),
                    "species_model": species_name or "",
                }, on_progress=progress_cb)
                if not resp.get("ok"):
                    raise RuntimeError(resp.get("error") or "识别失败")
                rows = [(r[0], float(r[1]))
                        for r in resp.get("species_rows", [])]
            else:
                # 本地模式：进程内加载物种模型（需要 torch，缺失时报中文错）
                from species_classifier import (classify_species,
                                                get_species_backend)
                progress_cb("正在加载物种识别模型（首次加载较慢，请稍候）...",
                            None, None)
                backend = get_species_backend(species_name)
                progress_cb("正在进行物种识别...", 1, 1)
                pil_image = Image.open(image_path).convert("RGB")
                top3 = classify_species(pil_image, backend=backend)
                rows = [(p.label_cn, float(p.confidence)) for p in top3]
        except Exception as exc:
            self._ui.post(self._on_species_error, f"物种识别出错：{exc}")
            return
        self._ui.post(self._on_species_done, rows)

    def _on_species_done(self, rows):
        """物种识别完成：对话区追加 Top-3 结果并恢复按钮。"""
        self._worker_running = False
        self._species_button.config(state="normal")
        self._progress_finish()
        self._append_dialogue("物种识别", _format_species_rows(rows))
        self._set_status("物种识别完成")

    def _on_species_error(self, message):
        """物种识别失败：对话区追加中文原因并恢复按钮。"""
        self._worker_running = False
        self._species_button.config(state="normal")
        self._progress_finish()
        self._append_dialogue("物种识别出错", message)
        self._set_status("物种识别失败")

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

    # ---------- 提问与识别（问题互动） ----------

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

        self._set_action_buttons("disabled")
        self._set_status("正在识别，请稍候...")
        self._progress_busy()          # 模型加载时长不定：先滚动动画
        self._worker_running = True
        worker = threading.Thread(
            target=self._ask_worker,
            args=(self._image_path, question, marker, species_name, part_name),
            daemon=True,
        )
        worker.start()

    def _ask_worker(self, image_path, question, marker, species_name, part_name):
        """识别子线程：调用问答引擎，异常兜底转中文提示。

        统一回传 (问题, 回答文本, 标记图路径, 可点击部位框列表)，
        供对话式结果区与画布交互使用。
        """
        progress_cb = self._make_progress_cb()
        try:
            if self._service is not None:
                # 服务模式：问答整体在训练环境子进程执行，按 JSON 协议取结果
                resp = self._service.answer(
                    image_path, question, marker, species_name, part_name,
                    on_progress=progress_cb)
                if not resp.get("ok") and not resp.get("message"):
                    raise RuntimeError(resp.get("error") or "识别失败")
                message = resp.get("message") or resp.get("error") or "识别失败"
                marked = resp.get("marked_image_path")
                boxes = []
                if resp.get("part_box") and resp.get("part_cn"):
                    boxes = [(resp["part_box"], resp["part_cn"],
                              resp.get("description"), resp.get("is_estimated"))]
            else:
                answer = vqa_engine.answer_question(
                    image_path=image_path,
                    question=question,
                    marker_style=marker,
                    species_model=species_name,
                    part_model=part_name,
                    progress_callback=progress_cb,
                )
                message = answer.message
                marked = (str(answer.marked_image_path)
                          if answer.marked_image_path else None)
                boxes = []
                if answer.part_box and answer.part_cn:
                    boxes = [(answer.part_box, answer.part_cn,
                              answer.description, answer.is_estimated)]
        except Exception as exc:
            self._ui.post(self._on_ask_error, f"识别出错：{exc}")
            return
        self._ui.post(self._on_ask_done, question, message, marked, boxes)

    def _on_ask_done(self, question, message, marked, boxes):
        """提问完成：对话区追加"问/答"记录并展示可点击标记图。"""
        self._worker_running = False
        self._set_action_buttons("normal")
        self._progress_finish()
        self._append_dialogue(None, f"问：{question}\n答：{message}")
        if marked:
            self._show_result_image(marked, boxes)
            self._set_status("识别完成：点击图中部位标记可查看名称")
        else:
            self._interactive_rects = []
            self._set_status("识别完成（未生成标记图）")

    def _on_ask_error(self, message):
        """识别异常：对话区追加中文原因并恢复按钮。"""
        self._worker_running = False
        self._set_action_buttons("normal")
        self._progress_finish()
        self._append_dialogue("识别出错", message)
        self._set_status("识别失败")

    # ---------- 一键全部位检测（无需提问） ----------

    def _on_detect_parts(self):
        """一键全部位检测入口：直接对当前图片检测全部语义部位。"""
        if self._worker_running:
            self._set_status("识别正在进行，请稍候...")
            return
        if not self._image_path:
            self._set_status("请先选择图片")
            return
        part_name = self._registry_of(self._part_var.get(), self._part_options)
        marker = self._marker_var.get()
        self._set_action_buttons("disabled")
        self._set_status("正在进行全部位检测（首次加载模型较慢，请稍候）...")
        self._progress_busy()          # 模型加载时长不定：先滚动动画
        self._worker_running = True
        threading.Thread(
            target=self._detect_parts_worker,
            args=(self._image_path, marker, part_name),
            daemon=True,
        ).start()

    def _detect_parts_worker(self, image_path, marker, part_name):
        """全部位检测子线程：服务模式走 detect_parts 协议，本地模式进程内检测。

        服务模式 parts 行结构：[中文名, 英文名, x1, y1, x2, y2, 是否估计, 描述]。
        """
        progress_cb = self._make_progress_cb()
        try:
            if self._service is not None:
                resp = self._service.request({
                    "cmd": "detect_parts",
                    "image_path": str(image_path),
                    "marker_style": marker or "",
                    "part_model": part_name or "",
                }, on_progress=progress_cb)
                if not resp.get("ok") and not resp.get("message"):
                    raise RuntimeError(resp.get("error") or "检测失败")
                message = resp.get("message") or resp.get("error") or "检测失败"
                marked = resp.get("marked_image_path")
                boxes = [([r[2], r[3], r[4], r[5]], r[0],
                          r[7] if len(r) > 7 else "", r[6])
                         for r in resp.get("parts", [])]
            else:
                result = vqa_engine.detect_all_parts(
                    image_path=image_path,
                    marker_style=marker,
                    part_model=part_name,
                    progress_callback=progress_cb,
                )
                message = result.message
                marked = (str(result.marked_image_path)
                          if result.marked_image_path else None)
                boxes = [(hit.box, hit.part_cn, hit.description, hit.is_estimated)
                         for hit in result.parts]
        except Exception as exc:
            self._ui.post(self._on_detect_parts_error, f"部位检测出错：{exc}")
            return
        self._ui.post(self._on_detect_parts_done, message, marked, boxes)

    def _on_detect_parts_done(self, message, marked, boxes):
        """全部位检测完成：对话区追加汇总并展示可点击标注图。"""
        self._worker_running = False
        self._set_action_buttons("normal")
        self._progress_finish()
        self._append_dialogue("部位检测", message)
        if marked:
            self._show_result_image(marked, boxes)
            self._set_status("部位检测完成：点击图中部位标记可查看名称")
        else:
            self._interactive_rects = []
            self._set_status("部位检测完成（未生成标记图）")

    def _on_detect_parts_error(self, message):
        """全部位检测异常：对话区追加中文原因并恢复按钮。"""
        self._worker_running = False
        self._set_action_buttons("normal")
        self._progress_finish()
        self._append_dialogue("部位检测出错", message)
        self._set_status("部位检测失败")

    # ---------- 对话式结果区 ----------

    def _append_dialogue(self, header, text):
        """向结果文本区追加一条对话记录块并自动滚动到底部。

        header 为 None 时按原文追加（用于"问：…/答：…"格式），
        否则冠以【标题】头（如【部位检测】【点击部位】）。
        """
        block = f"【{header}】\n{text}" if header else text
        current = self._result_text.get("1.0", "end").strip("\n")
        if current:
            block = "\n\n" + block
        self._result_text.insert("end", block + "\n")
        self._result_text.see("end")

    def _on_clear_dialogue(self):
        """清空对话记录并恢复结果图占位提示。"""
        self._result_text.delete("1.0", "end")
        self._reset_result_canvas_placeholder()
        self._set_status("对话记录已清空")

    def _set_action_buttons(self, state):
        """统一设置问答区三个动作按钮（部位检测/提问/清空对话）的可用状态。"""
        for name in ("_detect_button", "_ask_button", "_clear_dialog_button"):
            btn = getattr(self, name, None)
            if btn is None:
                continue
            try:
                if btn.winfo_exists():
                    btn.config(state=state)
            except Exception:
                pass                       # 控件已销毁（问答区重建）时忽略

    # ---------- 结果图画布与部位点击交互 ----------

    def _show_result_image(self, path, boxes=None):
        """在结果区画布显示标记图，并登记可点击的部位框。

        boxes 为原图像素坐标部位框列表
        [(框[x1,y1,x2,y2], 中文名, 特征描述, 是否估计), ...]；
        标记图与原图同尺寸，按显示缩放比换算为画布坐标供点击命中检测。
        """
        try:
            img = Image.open(path).convert("RGB")
        except Exception as exc:
            self._set_status(f"标记图读取失败：{exc}")
            return
        orig_w, orig_h = img.size
        img.thumbnail((_RESULT_MAX_WIDTH, 1 << 20), _RESAMPLE)
        disp_w, disp_h = img.size
        self._result_photo = ImageTk.PhotoImage(img)
        self._result_canvas.delete("all")
        self._result_canvas.config(width=disp_w, height=disp_h)
        self._result_canvas.create_image(
            0, 0, image=self._result_photo, anchor="nw")
        # 原图坐标 → 画布坐标（等比缩放），登记可交互部位框
        scale = (disp_w / orig_w) if orig_w else 1.0
        self._interactive_rects = []
        for box, part_cn, description, is_estimated in (boxes or []):
            try:
                x1, y1, x2, y2 = (float(v) for v in box[:4])
            except (TypeError, ValueError):
                continue                   # 坐标异常的框跳过，不影响其余交互
            self._interactive_rects.append({
                "rect": (x1 * scale, y1 * scale, x2 * scale, y2 * scale),
                "part_cn": part_cn,
                "description": description or "",
                "is_estimated": bool(is_estimated),
            })

    def _reset_result_canvas_placeholder(self):
        """结果图画布恢复占位提示（无标记图时）。"""
        self._result_canvas.delete("all")
        self._result_canvas.config(width=_RESULT_MAX_WIDTH, height=320)
        self._result_canvas.create_text(
            _RESULT_MAX_WIDTH // 2, 160,
            text="（部位检测或提问后此处显示标记图）",
            fill="#808080", width=_RESULT_MAX_WIDTH - 40)
        self._interactive_rects = []

    def _hit_interactive_rect(self, x, y):
        """返回画布坐标 (x, y) 命中的部位框；多框重叠时取面积最小者。"""
        best = None
        best_area = None
        for item in self._interactive_rects:
            rx1, ry1, rx2, ry2 = item["rect"]
            if rx1 <= x <= rx2 and ry1 <= y <= ry2:
                area = max(1.0, (rx2 - rx1) * (ry2 - ry1))
                if best is None or area < best_area:
                    best, best_area = item, area
        return best

    def _on_result_canvas_click(self, event):
        """点击标记图上的部位标记：状态栏与对话区显示该部位名称与描述。"""
        hit = self._hit_interactive_rect(event.x, event.y)
        if hit is None:
            return                         # 未命中任何部位框：不响应
        est_note = "（空间先验估计）" if hit["is_estimated"] else "（已检出）"
        self._set_status(f"部位名称：{hit['part_cn']}{est_note}")
        text = f"部位名称：{hit['part_cn']}{est_note}"
        if hit["description"]:
            text += f"\n{hit['description']}"
        self._append_dialogue("点击部位", text)

    def _on_result_canvas_motion(self, event):
        """鼠标移入部位标记范围时光标切换为手型，提示可点击。"""
        inside = self._hit_interactive_rect(event.x, event.y) is not None
        self._result_canvas.config(cursor="hand2" if inside else "")

    # ---------- 状态与进度 ----------

    def _set_status(self, text):
        """更新状态栏文本。"""
        self._status_var.set(text)

    def _progress_busy(self):
        """进度条切换为滚动动画（模型加载等无法计步的阶段）。"""
        self._progress_bar.config(mode="indeterminate")
        self._progress_bar.start(15)

    def _progress_step(self, current, total):
        """进度条按 当前步/总步数 推进（可计步任务）。"""
        self._progress_bar.stop()
        total = max(1, int(total or 1))
        self._progress_bar.config(mode="determinate", maximum=float(total))
        self._progress_var.set(min(float(current or 0), float(total)))

    def _progress_finish(self):
        """任务结束：停止动画并把进度条归零。"""
        try:
            self._progress_bar.stop()
        except Exception:
            pass                       # 控件已销毁时忽略
        self._progress_bar.config(mode="determinate", maximum=100.0)
        self._progress_var.set(0.0)

    def _on_task_progress(self, stage, current, total):
        """任务进度主线程回调：刷新状态文本并推进进度条。

        统一进度协议：current 为 None 表示无法计步（忙碌动画），
        否则按 (current, total) 推进；由后台线程经 _ui.post 投递调用。
        """
        self._set_status(stage)
        if current is None:
            self._progress_busy()
        else:
            self._progress_step(current, total)

    def _make_progress_cb(self):
        """构造可跨线程使用的进度回调（转发到主线程刷新界面）。"""
        def _cb(stage, current, total):
            self._ui.post(self._on_task_progress, stage, current, total)
        return _cb


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
