"""界面截图脚本（PrintWindow 抓取，含被遮挡区域）。"""

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tkinter as tk  # noqa: E402
from PIL import Image  # noqa: E402

import bird_crawler_gui as g  # noqa: E402

OUT = Path(sys.argv[1])
W = int(sys.argv[2]) if len(sys.argv) > 2 else 1080
H = int(sys.argv[3]) if len(sys.argv) > 3 else 760
OUT.mkdir(parents=True, exist_ok=True)

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
user32.SetProcessDPIAware()

root = tk.Tk()
app = g.BirdCrawlerGUI(root)
root.geometry(f"{g.px(W)}x{g.px(H)}+60+60")   # 按 DPI 换算，与界面内部一致
for _ in range(6):
    root.update()
    time.sleep(0.25)

hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()


class BIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def capture(path):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    user32.PrintWindow(hwnd, mem, 2)
    bi = BIH()
    bi.biSize = ctypes.sizeof(BIH)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(path)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    return w, h


tabs = [app.notebook.tab(t, "text").strip() for t in app.notebook.tabs()]
print("tabs:", tabs)
for idx, name in enumerate(tabs):
    if name.startswith("视觉分析"):   # 跳过视觉分析（会惰性拉起模型子进程）
        continue
    app.notebook.select(app.notebook.tabs()[idx])
    for _ in range(3):
        root.update()
        time.sleep(0.25)
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "_-")
    print(f"saved {idx:02d} {name}", capture(OUT / f"{idx:02d}_{safe}.png"))
root.destroy()
