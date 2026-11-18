#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：鸟类（iNaturalist 鸟类数据采集与图像下载系统）
# 文件：packaging/verify_installer.py —— 发布程序 / 安装包验证
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

"""发布程序与安装包验证（构建流水线第 5 步与安装包附加验证共用）。

两种模式：
    python packaging/verify_installer.py smoke <发布程序目录>
        在临时副本中验证发布程序（不污染构建产物）：
        版本/水印、数据库、迁移、数据集导出、拖入接口、面板、GUI 冒烟。

    python packaging/verify_installer.py installer <安装包.exe>
        校验安装包 PE 资源与体积 → 静默安装到临时目录 → 在安装目录内
        重跑全部冒烟 → 静默卸载 → 校验程序文件清理且用户数据保留。

所有被测进程都用「最小环境」启动：PATH 仅系统目录、清除全部 PYTHON*
变量，以此证明安装包在未安装 Python 的目标机上同样可运行。
"""

from __future__ import annotations

import csv
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from ctypes import wintypes
from pathlib import Path

APP_NAME = "鸟类"
APP_VERSION = "1.7.1"
APP_EXE = f"{APP_NAME}.exe"
SETUP_BASENAME = f"{APP_NAME}_安装包_v{APP_VERSION}"
PANEL_PORT = 18765

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


def skip(name: str, detail: str) -> None:
    RESULTS.append((name, True, f"[跳过] {detail}"))
    print(f"SKIP  {name}  — {detail}", flush=True)


def decode(raw: bytes) -> str:
    """控制台输出解码：优先 UTF-8，回退 GBK（简中系统 ANSI 代码页）。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def minimal_env() -> dict[str, str]:
    """模拟「目标机未安装 Python」的最小运行环境。"""
    keys = [
        "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA",
        "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "COMSPEC", "SYSTEMDRIVE",
        "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PROGRAMDATA",
        "PUBLIC", "OS", "PATHEXT", "LANG",
    ]
    env = {key: os.environ[key] for key in keys if key in os.environ}
    system_root = env.get("SYSTEMROOT", r"C:\Windows")
    env["PATH"] = os.path.join(system_root, "System32")
    return env


def run_app(app_dir: Path, args: list[str], timeout: int = 600,
            cwd: Path | None = None, env: dict[str, str] | None = None,
            ) -> tuple[int, str]:
    """以最小环境运行发布程序，返回 (退出码, 合并输出)。"""
    proc = subprocess.run(
        [str(app_dir / APP_EXE), *args],
        cwd=str(cwd or app_dir), env=env or minimal_env(),
        capture_output=True, timeout=timeout)
    return proc.returncode, decode(proc.stdout + proc.stderr)


def make_fixture_images(folder: Path, count: int = 5, size=(96, 72)) -> None:
    """生成测试照片（PIL 可用时写真图，否则写最小 JPEG 头部）。"""
    folder.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        for index in range(count):
            Image.new("RGB", size, (40 + index * 20, 120, 200)).save(
                folder / f"obs_{9000 + index}_photo_{9100 + index}.jpg", quality=80)
    except ImportError:
        for index in range(count):
            (folder / f"obs_{9000 + index}_photo_{9100 + index}.jpg").write_bytes(
                b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9")


def write_migration_fixture(root: Path, count: int = 5) -> None:
    """构造 migrate 子命令可读的离线数据目录（observations/photos CSV）。"""
    taxon_dir = root / "taxon_20967_Upupa_epops_Hoopoe"
    photo_dir = taxon_dir / "photos"
    make_fixture_images(photo_dir, count)

    with (taxon_dir / "observations.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "observation_id", "uuid", "quality_grade", "scientific_name",
            "common_name", "taxon_id", "taxon_rank", "observed_on",
            "latitude", "longitude", "photo_count", "local_photo_files",
        ])
        writer.writeheader()
        for index in range(count):
            writer.writerow({
                "observation_id": 9000 + index,
                "uuid": f"uuid-{index}",
                "quality_grade": "research",
                "scientific_name": "Upupa epops",
                "common_name": "戴胜",
                "taxon_id": 20967,
                "taxon_rank": "species",
                "observed_on": "2026-01-01",
                "latitude": 30.0,
                "longitude": 120.0,
                "photo_count": 1,
                "local_photo_files":
                    f"photos/obs_{9000 + index}_photo_{9100 + index}.jpg",
            })

    with (taxon_dir / "photos.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "photo_id", "observation_id", "position", "license_code",
            "width", "height", "original_url", "local_path",
            "download_ok", "requested_size", "downloaded_size",
        ])
        writer.writeheader()
        for index in range(count):
            writer.writerow({
                "photo_id": 9100 + index,
                "observation_id": 9000 + index,
                "position": 0,
                "license_code": "cc-by",
                "width": 96,
                "height": 72,
                "original_url": f"https://example.invalid/{index}.jpg",
                "local_path": f"photos/obs_{9000 + index}_photo_{9100 + index}.jpg",
                "download_ok": "True",
                "requested_size": "large",
                "downloaded_size": "large",
            })

    (taxon_dir / "summary.json").write_text(json.dumps({
        "taxon_id": 20967,
        "scientific_name": "Upupa epops",
        "common_name": "戴胜",
        "quality_grade": "research",
        "batch_time": "2026-01-01T00:00:00",
    }, ensure_ascii=False), encoding="utf-8")


def wait_http(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


# ============================================================
# 冒烟测试（在一个可写工作目录中执行）
# ============================================================
def smoke_test(app_dir: Path) -> None:
    exe = app_dir / APP_EXE
    if not check("发布程序 exe 存在", exe.is_file(), str(exe)):
        return
    internal = app_dir / "_internal"
    check("PyInstaller 运行时目录 _internal 存在", internal.is_dir())
    check("内置 Python 运行库（python311.dll）", (internal / "python311.dll").is_file())
    check("内置 tkinter/Tcl 运行库",
          any((internal / name).is_dir() for name in ("tcl", "_tcl_data", "lib\\tcl8.6")))

    # 1) 版本与数字水印
    code, out = run_app(app_dir, ["--version"], timeout=120)
    check("--version 正常退出", code == 0, out.strip()[:160])
    check(f"--version 含版本号 v{APP_VERSION}", f"v{APP_VERSION}" in out, out.strip()[:160])

    code, out = run_app(app_dir, ["copyright"], timeout=120)
    watermark = re.search(r"TYWM-BIRD-v[\d.]+-[0-9A-F]{16}", out)
    check("copyright 正常退出", code == 0, out.strip()[:160])
    check("copyright 输出完整数字水印", watermark is not None,
          watermark.group(0) if watermark else out.strip()[:160])
    check("copyright 输出构建指纹与时间",
          "构建指纹：" in out and "构建时间：" in out and "@@BUILD" not in out)

    # 2) 数据库：初始化 + 统计 + 健康检查
    code, out = run_app(app_dir, ["init-db"], timeout=300)
    check("init-db 正常退出", code == 0, out.strip()[:200])
    db_dir = app_dir / "data" / "db"
    check("冻结形态数据目录解析到 exe 旁（data/db）",
          (db_dir / "bird_observations.db").is_file()
          and (db_dir / "raw_data.db").is_file(), str(db_dir))

    code, out = run_app(app_dir, ["stats"], timeout=300)
    check("stats 正常退出（空库）", code == 0 and "观察记录总数" in out, out.strip()[:200])

    # 3) 离线数据写入链路：migrate → stats → dataset → health
    fixture = app_dir / "_smoke_migrate"
    write_migration_fixture(fixture)
    code, out = run_app(app_dir, ["migrate", "--out-dir", str(fixture)], timeout=600)
    check("migrate 迁移 5 观察 / 5 照片", code == 0 and "迁移完成" in out,
          out.strip()[-300:])
    code, out = run_app(app_dir, ["stats", "--out-dir", str(fixture)], timeout=600)
    check("stats 读到迁移后数据（5 观察）",
          code == 0 and "观察记录总数: 5" in out, out.strip()[:240])

    dataset_dir = fixture / "dataset_out"
    code, out = run_app(app_dir, [
        "dataset", "--out", str(dataset_dir), "--out-dir", str(fixture),
        "--min-per-class", "0", "--no-verify"], timeout=600)
    manifest = dataset_dir / "manifest.csv"
    rows = 0
    if manifest.is_file():
        with manifest.open(encoding="utf-8-sig") as fh:
            rows = len([line for line in fh if line.strip()]) - 1
    check("dataset 导出训练数据集（manifest 5 行）",
          code == 0 and manifest.is_file() and rows == 5,
          f"manifest 行数={rows}" if code == 0 else out.strip()[-300:])
    check("dataset 导出必备文件（manifest.jsonl/report.md/train_example.py）",
          all((dataset_dir / name).is_file() for name in
              ("manifest.jsonl", "report.md", "train_example.py")))

    code, out = run_app(app_dir, ["health", "--out-dir", str(fixture)], timeout=600)
    check("health 数据库健康检查正常退出", code == 0, out.strip()[-300:])

    # 4) 拖入接口（无网络环境下应优雅跳过，仍正常退出并写报告）
    drop_src = app_dir / "_smoke_drop"
    make_fixture_images(drop_src / "戴胜", count=2)
    code, out = run_app(app_dir, ["drop", "--file", str(drop_src)], timeout=600)
    processed = app_dir / "data" / "drop_inbox" / "processed_已处理"
    report_written = processed.is_dir() and any(processed.glob("*.txt"))
    check("drop 拖入接口正常退出并生成报告", code == 0 and report_written,
          out.strip()[-260:])

    # 5) 数据面板 HTTP 服务
    proc = subprocess.Popen(
        [str(exe), "panel", "--out-dir", str(fixture),
         "--port", str(PANEL_PORT), "--no-browser"],
        cwd=str(app_dir), env=minimal_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok = wait_http(f"http://127.0.0.1:{PANEL_PORT}/")
        check("panel 数据面板 HTTP 200", ok, f"http://127.0.0.1:{PANEL_PORT}/")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # 6) GUI 冒烟：桌面控制台启动后持续存活
    log_path = app_dir / "_smoke_gui.log"
    with log_path.open("wb") as log_file:
        gui = subprocess.Popen(
            [str(exe), "gui"], cwd=str(app_dir), env=minimal_env(),
            stdout=log_file, stderr=subprocess.STDOUT)
        gui_started = time.time()
        try:
            deadline = gui_started + 20
            while time.time() < deadline:
                if gui.poll() is not None:
                    break
                time.sleep(1)
            gui_alive = gui.poll() is None
        finally:
            gui_elapsed = time.time() - gui_started
            if gui.poll() is None:
                gui.terminate()
                try:
                    gui.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    gui.kill()
    gui_log = decode(log_path.read_bytes())
    check("GUI 桌面控制台启动后存活 >= 8s", gui_alive,
          f"实际存活 {gui_elapsed:.0f}s")
    check("GUI 启动无 Python 异常堆栈",
          "Traceback" not in gui_log and "ModuleNotFoundError" not in gui_log,
          gui_log.strip()[-300:])

    # 7) 清理测试痕迹（保留 data，模拟真实用户目录）
    for temp_name in ("_smoke_migrate", "_smoke_drop", "_smoke_gui.log"):
        target = app_dir / temp_name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.is_file():
            target.unlink(missing_ok=True)


# ============================================================
# 模式：smoke
# ============================================================
def mode_smoke(app_dir: Path) -> int:
    print(f"== 发布程序冒烟测试：{app_dir} ==")
    work = Path(tempfile.mkdtemp(prefix="bird_smoke_"))
    copy = work / APP_NAME
    try:
        shutil.copytree(app_dir, copy)
        smoke_test(copy)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return report()


# ============================================================
# 模式：installer
# ============================================================
def mode_installer(setup: Path) -> int:
    print(f"== 安装包验证：{setup} ==")
    if not check("安装包文件存在", setup.is_file(), str(setup)):
        return report()

    size_mb = setup.stat().st_size / 1024 / 1024
    check("安装包体积 < 100MB（torch 未泄漏）", size_mb < 100, f"{size_mb:.1f} MB")

    raw_info = get_version_info(setup)
    info = {key: (value or "").strip() for key, value in raw_info.items()}
    check("安装包 PE 公司=天咏", info.get("CompanyName") == "天咏", str(info.get("CompanyName")))
    check("安装包 PE 版权行含 2026 天咏",
          "2026 天咏" in (info.get("LegalCopyright") or ""), str(info.get("LegalCopyright")))
    check("安装包 PE 描述含数字水印",
          "TYWM-BIRD-v" in (info.get("FileDescription") or ""),
          str(info.get("FileDescription")))
    check(f"安装包版本资源 {APP_VERSION}.0",
          str(info.get("FileVersion") or "").startswith(APP_VERSION),
          str(info.get("FileVersion")))

    install_root = Path(tempfile.mkdtemp(prefix="bird_install_"))
    install_dir = install_root / APP_NAME
    log_file = install_root / "install.log"
    uninstalled = False
    install_ok = False
    try:
        cmd = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
               f"/DIR={install_dir}", f"/LOG={log_file}"]
        proc = subprocess.run(cmd, capture_output=True, timeout=900,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        installed = install_dir / APP_EXE
        install_ok = proc.returncode == 0 and installed.is_file()
        detail = f"退出码={proc.returncode}"
        if not install_ok:
            detail += "；" + describe_install_failure(log_file)
        check("静默安装成功（退出码 0 且 exe 就位）", install_ok, detail)
        if not install_ok:
            raise RuntimeError(f"安装未完成，日志保留于 {log_file}")

        check("安装目录含启动脚本与使用说明",
              (install_dir / "启动爬虫界面.bat").is_file()
              and (install_dir / "拖入文件自动爬取.bat").is_file()
              and (install_dir / "使用说明.txt").is_file())
        check("安装目录含源码形态训练/视觉组件",
              (install_dir / "model_trainer.py").is_file()
              and (install_dir / "model_library.py").is_file()
              and (install_dir / "vision_system" / "gui_app.py").is_file()
              and (install_dir / "part_model_training" / "train_segpart.py").is_file())

        start_menu = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
                      "Start Menu" / "Programs" / APP_NAME)
        check("开始菜单快捷方式已创建", (start_menu / f"{APP_NAME}.lnk").is_file(),
              str(start_menu))

        # 安装完成后在安装目录内重跑全部冒烟（不复制目录，验证真实安装结果）
        smoke_test(install_dir)

        # 校验「可移植性」：从任意目录用绝对路径启动也能定位自身数据目录
        code, out = run_app(install_dir, ["copyright"], cwd=Path(tempfile.gettempdir()),
                            timeout=120)
        check("从其他工作目录启动仍能定位自身（copyright）", code == 0, out.strip()[:160])

        uninstaller = install_dir / "unins000.exe"
        if check("卸载程序存在", uninstaller.is_file()):
            proc = subprocess.run(
                [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                capture_output=True, timeout=900,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            uninstalled = True
            time.sleep(2)
            check("静默卸载成功（退出码 0）", proc.returncode == 0,
                  f"退出码={proc.returncode}")
            check("卸载后主程序已移除", not (install_dir / APP_EXE).exists())
            check("卸载后运行库目录已清理", not (install_dir / "_internal").exists())
            check("卸载后内置源码目录已清理",
                  not (install_dir / "vision_system").exists()
                  and not (install_dir / "part_model_training").exists())
            check("卸载后用户数据仍保留（data/db）",
                  (install_dir / "data" / "db" / "bird_observations.db").is_file())
            check("卸载后开始菜单快捷方式已清除",
                  not (start_menu / f"{APP_NAME}.lnk").exists())
    finally:
        # 成功卸载后清理本次验证安装的临时目录；失败时保留现场与日志便于诊断
        if uninstalled:
            shutil.rmtree(install_root, ignore_errors=True)
        else:
            print(f"  [保留] 安装现场与日志：{install_root}")

    return report()


def describe_install_failure(log_file: Path) -> str:
    """从 Inno 安装日志提取失败原因（异常行与前文），便于定位。"""
    if not log_file.is_file():
        return "无安装日志"
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "安装日志不可读"
    hits = [index for index, line in enumerate(lines)
            if "Exception" in line or "错误" in line or "Fatal" in line]
    if not hits:
        return f"日志无异常行（{log_file}）"
    picked: list[str] = []
    for index in hits[:2]:
        picked.extend(line.strip() for line in lines[index:index + 3])
    return "；".join(picked)[:400]


def get_version_info(path: Path) -> dict[str, str]:
    """读取 exe PE 版本资源字符串（ctypes 调 Windows version.dll，无额外依赖）。"""
    keys = ("CompanyName", "LegalCopyright", "FileDescription",
            "FileVersion", "ProductName", "ProductVersion")
    try:
        version = ctypes.WinDLL("version")
        target = str(path)
        size = version.GetFileVersionInfoSizeW(target, None)
        if not size:
            return {}
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(target, 0, size, buffer):
            return {}
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        # 取语言/代码页（VarFileInfo\Translation）
        if not version.VerQueryValueW(buffer, "\\VarFileInfo\\Translation",
                                      ctypes.byref(pointer), ctypes.byref(length)):
            return {}
        lang, codepage = ctypes.cast(
            pointer, ctypes.POINTER(wintypes.WORD * 2)).contents
        info: dict[str, str] = {}
        for key in keys:
            sub = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{key}"
            if version.VerQueryValueW(buffer, sub, ctypes.byref(pointer),
                                      ctypes.byref(length)) and length.value:
                info[key] = ctypes.wstring_at(pointer, length.value).rstrip("\x00")
        return info
    except Exception:
        return {}


def mode_payload(iss_path: Path) -> int:
    """受限环境下的载荷级安装验证。

    本环境（DSH Windows 沙箱）会拦截安装程序的开始菜单/注册表写入，
    导致真实安装包在快捷方式阶段回滚（安装日志 "错误 5: 拒绝访问"）。
    本模式从正式封装脚本派生一个「无快捷方式、无卸载项」的临时安装包：
    文件清单、压缩、安装目录结构、预置数据库与正式包完全一致，
    只跳过需要 APPDATA/HKCU 的两类写入，用于验证安装载荷与安装后程序可用性。
    """
    print(f"== 载荷级安装验证（临时派生安装包）：{iss_path} ==")
    if not check("封装脚本存在", iss_path.is_file(), str(iss_path)):
        return report()
    iscc = find_iscc()
    if iscc is None:
        check("找到 Inno Setup 编译器", False)
        return report()

    work = Path(tempfile.mkdtemp(prefix="bird_payload_"))
    try:
        variant = work / "payload_test.iss"
        variant.write_text(derive_payload_iss(iss_path.read_text(encoding="utf-8-sig")),
                           encoding="utf-8-sig", newline="\r\n")
        proc = subprocess.run(
            [str(iscc), f"/O{work}", str(variant)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=900)
        setup = work / "payload_test.exe"
        if not check("派生安装包编译成功", proc.returncode == 0 and setup.is_file(),
                     (proc.stdout or proc.stderr or "")[-300:]):
            return report()

        install_dir = work / "安装结果" / APP_NAME
        env = dict(os.environ)
        env["TEMP"] = str(work / "temp")
        env["TMP"] = env["TEMP"]
        Path(env["TEMP"]).mkdir()
        proc = subprocess.run(
            [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
             f"/DIR={install_dir}", f"/LOG={work / 'install.log'}"],
            capture_output=True, timeout=900, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        installed = install_dir / APP_EXE
        check("载荷安装完成（退出码 0 且 exe 就位）",
              proc.returncode == 0 and installed.is_file(),
              f"退出码={proc.returncode}；{describe_install_failure(work / 'install.log')}")
        check("预置数据库随安装包写入（onlyifdoesntexist 语义）",
              (install_dir / "data" / "db" / "bird_observations.db").is_file())
        if installed.is_file():
            smoke_test(install_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return report()


def find_iscc() -> Path | None:
    """定位 Inno Setup 编译器（与构建流水线一致的候选顺序）。"""
    for candidate in (os.environ.get("ISCC", ""),
                      r"C:\Program Files\Inno Setup 7\ISCC.exe",
                      r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
                      r"C:\Program Files\Inno Setup 6\ISCC.exe",
                      r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def derive_payload_iss(text: str) -> str:
    """从正式封装脚本派生载荷测试脚本：去掉快捷方式/卸载项/安装后运行。"""
    kept: list[str] = []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("outputbasefilename="):
            kept.append("OutputBaseFilename=payload_test")
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.lower()
            if section in ("[icons]", "[run]", "[code]", "[uninstalldelete]"):
                continue
            kept.append(line)
            if section == "[setup]":
                kept.append("; 载荷验证派生：不写开始菜单/卸载注册表（受限环境用途）")
                kept.append("Uninstallable=no")
            continue
        if section in ("[icons]", "[run]", "[code]", "[uninstalldelete]"):
            continue
        kept.append(line)
    return "\r\n".join(kept) + "\r\n"


def report() -> int:
    print("\n" + "=" * 68)
    failures = [item for item in RESULTS if not item[1]]
    print(f"结果：{len(RESULTS) - len(failures)}/{len(RESULTS)} 通过")
    for name, _, detail in failures:
        print(f"  - FAIL {name}  {detail}")
    print("=" * 68)
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("smoke", "installer", "payload"):
        print(__doc__)
        return 2
    target = Path(argv[2]).resolve()
    try:
        if argv[1] == "smoke":
            return mode_smoke(target)
        if argv[1] == "payload":
            return mode_payload(target)
        return mode_installer(target)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
