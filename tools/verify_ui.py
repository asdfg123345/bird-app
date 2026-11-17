"""界面验收脚本：量化检查各页签关键控件是否可见、可点。

检查项：
  UI-P0-A  下载控制页「开始下载 / 停止下载」按钮可见（非 0 高、在窗口内）
  UI-P0-B  模型库选中行后预览/详情刷新
  UI-P0-C  训练数据集页右栏控件不被窗口右边界裁切
  UI-4     9 个页签文案不被截断
  UI-A3    页签按流程分组顺序正确
  UI-A1    无裸色值字面量（全部走主题常量）

窗口尺寸按当前 DPI 缩放系数换算，保证在本机与高分屏上测到的是等效布局。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import tkinter as tk  # noqa: E402

import bird_crawler_gui as g  # noqa: E402

results = []

# 期望的页签顺序（按业务流程分组：采集 → 数据 → 模型 → 分析 → 系统）
EXPECTED_TAB_ORDER = [
    "下载控制", "拖入文件爬取",        # 采集
    "训练数据集", "数据浏览", "照片浏览",  # 数据
    "模型训练", "模型库",              # 模型
    "视觉分析",                        # 分析
    "统计与说明",                      # 系统
]


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def tab_index(app, label):
    """按页签文案取索引（不依赖硬编码序号，页签重排后仍然有效）。"""
    for i, t in enumerate(app.notebook.tabs()):
        if app.notebook.tab(t, "text").strip() == label:
            return i
    raise KeyError(f"未找到页签「{label}」")


def widget_visible_in_window(win, w):
    """控件是否完整位于窗口可视区域内（按屏幕坐标判断）。"""
    if not w.winfo_ismapped():
        return False, "未映射"
    win.update_idletasks()
    wx, wy = w.winfo_rootx(), w.winfo_rooty()
    ww, wh = w.winfo_width(), w.winfo_height()
    rx, ry = win.winfo_rootx(), win.winfo_rooty()
    rw, rh = win.winfo_width(), win.winfo_height()
    if ww <= 1 or wh <= 1:
        return False, f"尺寸塌陷 {ww}x{wh}"
    if wx < rx or wy < ry or wx + ww > rx + rw or wy + wh > ry + rh:
        return False, f"越界 控件({wx - rx},{wy - ry},{ww}x{wh}) 窗口({rw}x{rh})"
    return True, f"{ww}x{wh}"


def run(dw, dh):
    """dw/dh 为「设计稿像素」，内部按 DPI 换算成实际像素。"""
    W, H = g.px(dw), g.px(dh)
    root = tk.Tk()
    app = g.BirdCrawlerGUI(root)
    root.geometry(f"{W}x{H}")
    for _ in range(5):
        root.update()

    print()
    print("=" * 70)
    print(f"设计稿 {dw}x{dh} -> 实际 {W}x{H}（DPI x{g.ui_scale():g}）")
    print("=" * 70)

    # ---------- UI-P0-A 下载控制页 ----------
    app.notebook.select(app.notebook.tabs()[tab_index(app, "下载控制")])
    for _ in range(3):
        root.update()
    ok1, d1 = widget_visible_in_window(root, app.start_btn)
    ok2, d2 = widget_visible_in_window(root, app.stop_btn)
    check(f"[{dw}x{dh}] UI-P0-A 下载页「开始下载」可见", ok1, d1)
    check(f"[{dw}x{dh}] UI-P0-A 下载页「停止下载」可见", ok2, d2)

    # ---------- UI-P0-C 训练数据集页右栏 ----------
    app.notebook.select(app.notebook.tabs()[tab_index(app, "训练数据集")])
    for _ in range(3):
        root.update()
    ok3, d3 = widget_visible_in_window(root, app.ds_generate_btn)
    check(f"[{dw}x{dh}] UI-P0-C 数据集页「生成数据集」可见", ok3, d3)
    right = app.ds_generate_btn.master.master
    over = right.winfo_reqwidth() - right.winfo_width()
    check(f"[{dw}x{dh}] UI-P0-C 数据集页右栏未被裁切",
          over <= 1, f"请求 {right.winfo_reqwidth()} / 实际 {right.winfo_width()}")

    # ---------- UI-4 页签条不溢出 ----------
    # 注：notebook.bbox() 在窗口未完全映射时恒返回 0，不能用来判断截断，
    # 因此改为「字体实测文本宽度 + 已知内边距」估算页签条总宽。
    from tkinter import font as tkfont  # noqa: E402
    tab_font = tkfont.Font(family=g.UI_FONT_UI[0], size=g.UI_FONT_UI[1])
    nb_w = app.notebook.winfo_width()
    total = 0
    for t in app.notebook.tabs():
        text = app.notebook.tab(t, "text").strip()
        try:
            pad = int(app.notebook.tab(t, "padding")[0])
        except Exception:
            pad = 10
        total += tab_font.measure(text) + 2 * pad + 2 * 2   # 左右内边距 + 边框余量
    check(f"[{dw}x{dh}] UI-4 页签条不超出窗口",
          total <= nb_w, f"估算页签总宽 {total} / 页签区 {nb_w}")

    # ---------- UI-A3 页签顺序 ----------
    if (dw, dh) == (1080, 760):
        actual = [app.notebook.tab(t, "text").strip() for t in app.notebook.tabs()]
        check("UI-A3 页签按流程分组排序", actual == EXPECTED_TAB_ORDER,
              f"实际 {actual}")

    return root, app


def check_model_preview():
    W, H = g.px(1080), g.px(760)
    root = tk.Tk()
    app = g.BirdCrawlerGUI(root)
    root.geometry(f"{W}x{H}")
    for _ in range(4):
        root.update()
    app.notebook.select(app.notebook.tabs()[tab_index(app, "模型库")])
    for _ in range(3):
        root.update()
    children = app.model_tree.get_children()
    if not children:
        check("UI-P0-B 模型库预览随选中刷新", False, "模型库为空，无法验证")
        root.destroy()
        return
    before = app.model_info_label.cget("text")
    app.model_tree.selection_set(children[0])
    app.model_tree.focus(children[0])
    for _ in range(3):
        root.update()
    after = app.model_info_label.cget("text")
    check("UI-P0-B 模型库预览随选中刷新", after != before and bool(after),
          f"选中后详情长度 {len(after)}")
    root.destroy()


def check_no_bare_colors():
    """界面代码不得再出现裸色值字面量（必须走主题常量）。"""
    src = (ROOT / "src" / "bird_crawler_gui.py").read_text(encoding="utf-8")
    # 常量定义块自身允许出现色值，其余部分不允许
    tail_start = src.index("_LOCAL_OVERRIDE_FILE = ")
    tail = src[tail_start:]
    bare = re.findall(r"#[0-9a-fA-F]{6}", tail)
    check("UI-A1 主题常量之外无裸色值", not bare, f"残留 {bare[:5]}")
    consts = re.findall(r"^(CLR_[A-Z_]+) = ", src, re.M)
    check("UI-A1 主题常量数量合理", len(consts) >= 20, f"{len(consts)} 个")


def main():
    for dw, dh in [(1080, 760), (960, 660), (1400, 900)]:
        root, app = run(dw, dh)
        root.destroy()

    print()
    print("=" * 70)
    print("模型库预览联动 / 主题常量")
    print("=" * 70)
    check_model_preview()
    check_no_bare_colors()

    fails = [r for r in results if not r[1]]
    print()
    print("=" * 70)
    print(f"结果：{len(results) - len(fails)}/{len(results)} 通过")
    for n, _, d in fails:
        print(f"  FAIL {n}  {d}")
    print("=" * 70)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
