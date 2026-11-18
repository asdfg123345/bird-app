#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：鸟类（iNaturalist 鸟类数据采集与图像下载系统）
# 文件：packaging/sign_release.py —— 发布产物代码签名（signtool 封装）
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

"""给发布程序与安装包做 Authenticode 代码签名（消除 SmartScreen「无法识别的应用」提示的根治手段）。

背景：
    未签名的安装包在 Windows 10/11 上首次运行会被 Microsoft Defender
    SmartScreen 拦截（「Windows 已保护你的电脑 / 无法识别的应用启动」）。
    这是信誉机制，不是病毒告警；唯一根治方式是使用受信任的代码签名证书签名，
    并让安装包与主程序 鸟类.exe 都带上签名。

证书要求：
    - 代码签名证书（Code Signing EKU），CA/B Forum 要求私钥存放于硬件令牌/USB Key/云 HSM；
    - EV 证书（扩展验证）可获得接近即时的 SmartScreen 信誉；
    - OV 证书签名后仍需积累下载信誉，初期部分机器可能仍提示一次。

用法（在仓库根目录执行）：
    # 1) 证书已装入「当前用户\\个人」证书库（硬件令牌已插入）：
    python packaging/sign_release.py
    python packaging/sign_release.py --thumbprint 1A2B3C... --timestamp http://timestamp.digicert.com

    # 2) 使用 PFX 文件：
    python packaging/sign_release.py --pfx D:\\cert\\bird.pfx --password ********

    # 3) 只签指定文件（可多次指定）：
    python packaging/sign_release.py --file release/鸟类_v1.7.1/发布程序/鸟类/鸟类.exe

默认签名目标：
    release/鸟类_v1.7.1/发布程序/鸟类/鸟类.exe      （主程序，需在编译安装包前签名）
    release/鸟类_v1.7.1/安装输出/鸟类_安装包_v1.7.1.exe（安装包）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "鸟类"
APP_VERSION = "1.7.1"
REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = REPO_ROOT / "release" / f"{APP_NAME}_v{APP_VERSION}"
DEFAULT_TARGETS = [
    RELEASE_DIR / "发布程序" / APP_NAME / f"{APP_NAME}.exe",
    RELEASE_DIR / "安装输出" / f"{APP_NAME}_安装包_v{APP_VERSION}.exe",
]
DEFAULT_TIMESTAMP = "http://timestamp.digicert.com"


def log(message: str) -> None:
    print(message, flush=True)


def find_signtool() -> Path | None:
    """定位 signtool.exe（Windows SDK），优先最高版本。"""
    candidates: list[Path] = []
    kits = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
    if kits.is_dir():
        for version_dir in sorted(kits.iterdir(), reverse=True):
            for arch in ("x64", "x86"):
                exe = version_dir / arch / "signtool.exe"
                if exe.is_file():
                    candidates.append(exe)
    for name in ("signtool.exe",):
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if directory:
                exe = Path(directory) / name
                if exe.is_file():
                    candidates.append(exe)
    return candidates[0] if candidates else None


def list_code_signing_certs() -> list[dict[str, str]]:
    """列出证书库中带代码签名用途的证书（Subject / Thumbprint / NotAfter）。"""
    script = (
        "Get-ChildItem Cert:\\CurrentUser\\My, Cert:\\LocalMachine\\My "
        "-CodeSigningCert -ErrorAction SilentlyContinue | "
        "Select-Object Subject,Thumbprint,NotAfter,HasPrivateKey | "
        "ConvertTo-Json -Compress")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
        data = json.loads(proc.stdout) if proc.stdout.strip() else []
        if isinstance(data, dict):
            data = [data]
        return [item for item in data if item.get("HasPrivateKey")]
    except Exception:
        return []


def sign_file(target: Path, signtool: Path, thumbprint: str | None,
              pfx: str | None, password: str | None,
              timestamp: str) -> None:
    """用 signtool 对单个文件签名（SHA-256 + RFC3161 时间戳）。"""
    if not target.is_file():
        raise RuntimeError(f"签名目标不存在：{target}")
    cmd = [str(signtool), "sign", "/fd", "SHA256", "/td", "SHA256",
           "/tr", timestamp, "/v"]
    if pfx:
        cmd += ["/f", pfx]
        if password:
            cmd += ["/p", password]
    elif thumbprint:
        cmd += ["/sha1", thumbprint]
    else:
        raise RuntimeError("必须指定 --thumbprint 或 --pfx")
    cmd.append(str(target))
    log(f"  [签名] {target.name} ...")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"签名失败：{target}\n--- stdout ---\n{proc.stdout[-2000:]}\n"
            f"--- stderr ---\n{proc.stderr[-2000:]}")
    log(f"  [签名] 完成：{target.name}")


def verify_file(target: Path, signtool: Path) -> None:
    """校验签名有效性（/pa 使用默认验证策略）。"""
    proc = subprocess.run(
        [str(signtool), "verify", "/pa", "/v", str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(
            f"签名校验失败：{target}\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}")
    log(f"  [校验] 签名有效：{target.name}")


def default_thumbprint(certs: list[dict[str, str]]) -> str | None:
    """未显式指定时：证书库中恰好一个可私钥的代码签名证书则自动使用。"""
    if len(certs) == 1:
        return str(certs[0].get("Thumbprint", "")).strip()
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION} 发布产物代码签名")
    parser.add_argument("--thumbprint", default="", help="证书指纹（证书库选择模式）")
    parser.add_argument("--pfx", default="", help="PFX 证书文件路径")
    parser.add_argument("--password", default="", help="PFX 密码")
    parser.add_argument("--timestamp", default=DEFAULT_TIMESTAMP,
                        help=f"RFC3161 时间戳服务（默认 {DEFAULT_TIMESTAMP}）")
    parser.add_argument("--file", action="append", default=[],
                        help="仅签指定文件（可多次指定，覆盖默认目标）")
    parser.add_argument("--list-certs", action="store_true", help="只列出可用代码签名证书")
    args = parser.parse_args(argv[1:])

    certs = list_code_signing_certs()
    if args.list_certs:
        if not certs:
            log("证书库中未找到带私钥的代码签名证书。")
            return 0
        for item in certs:
            log(f"  Subject={item.get('Subject')}\n  Thumbprint={item.get('Thumbprint')}"
                f"\n  NotAfter={item.get('NotAfter')}\n")
        return 0

    signtool = find_signtool()
    if signtool is None:
        log("未找到 signtool.exe：请安装 Windows SDK（含 Signing Tools）。")
        return 2

    thumbprint = args.thumbprint.strip() or default_thumbprint(certs)
    if not args.pfx and not thumbprint:
        log("未找到可用的代码签名证书（证书库为空或存在多张证书）。")
        log("请插入 EV/OV 硬件令牌后装入证书，或使用 --pfx 指定证书文件；")
        log("如仅想列出证书：python packaging/sign_release.py --list-certs")
        return 2

    targets = ([Path(item) for item in args.file] if args.file
               else list(DEFAULT_TARGETS))
    log(f"== {APP_NAME} v{APP_VERSION} 代码签名 ==")
    log(f"  signtool：{signtool}")
    log(f"  证书：{'PFX ' + args.pfx if args.pfx else 'Thumbprint ' + thumbprint}")
    log(f"  时间戳：{args.timestamp}")
    for target in targets:
        sign_file(target, signtool, thumbprint, args.pfx or None,
                  args.password or None, args.timestamp)
        verify_file(target, signtool)
    log(f"\n== 签名完成：{len(targets)} 个文件 ==\n"
        f"  下一步：重新编译安装包（signed 主程序会被打进安装包），"
        f"再对安装包签名，见 docs/安装包_代码签名与SmartScreen说明.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
