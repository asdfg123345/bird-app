#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================
"""项目打乱（混淆）发布版构建脚本。

功能：
    将项目源码打乱后输出到独立发布文件夹，供打包发送他人运行：
    1. 移除所有文档字符串与注释（作者签名保留）；
    2. 将全部模块级函数 / 类 / 常量重命名为随机名，且跨模块一致
       （导入引用同步重命名，程序行为完全不变）；
    3. 复制启动脚本与使用说明，排除数据与文档；
    4. 构建完成后自动做语法校验。

用法：
    python build_scrambled_release.py [--out 发布目录]
"""

from __future__ import annotations

import argparse
import ast
import io
import random
import re
import shutil
import tokenize
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 参与打乱的项目源文件（模块间互相导入，必须一起处理）
# 覆盖全部业务模块：GUI 会按文件名启动 dataset_exporter / model_trainer
# 等子进程，任何遗漏都会导致发布版功能缺失。
SOURCE_FILES = [
    "main.py",
    "download_inaturalist.py",
    "batch_download_birds.py",
    "inaturalist_db_initializer.py",
    "inaturalist_db_manager.py",
    "inaturalist_db_migrator.py",
    "bird_db_panel.py",
    "bird_crawler_gui.py",
    "drop_crawl_interface.py",
    "db_health_check.py",
    "dataset_exporter.py",
    "dataset_registry.py",
    "model_library.py",
    "model_trainer.py",
    "training_dataset_importer.py",
    "cleanup_misimported_observations.py",
]

# 原样复制的启动脚本与资源文件
BAT_FILES = ["启动爬虫界面.bat", "拖入文件自动爬取.bat"]
ASSET_FILES = ["鸟类.ico"]


def remove_docstrings(text: str, tree: ast.AST) -> str:
    """按 AST 定位所有文档字符串并删除其行。

    模块级文档字符串直接整段删除（避免破坏 from __future__ 的位置限制）；
    函数/类内若文档字符串是唯一语句，则用缩进的 pass 顶替保持代码块有效。
    """
    ranges: list[tuple[int, int, bool]] = []  # (起始行, 结束行, 是否需以pass顶替)

    def scan(node: ast.AST) -> None:
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                need_pass = not isinstance(node, ast.Module) and len(body) == 1
                ranges.append((body[0].lineno, body[0].end_lineno, need_pass))
        for child in ast.iter_child_nodes(node):
            scan(child)

    scan(tree)
    lines = text.splitlines(keepends=True)
    # 从后往前删除，避免行号偏移
    for start, end, need_pass in sorted(ranges, reverse=True):
        if need_pass:
            first = lines[start - 1]
            indent = first[: len(first) - len(first.lstrip())]
            lines[start - 1:end] = [f"{indent}pass\n"]
        else:
            del lines[start - 1:end]
    return "".join(lines)


def collect_top_level_names(trees: list[ast.AST]) -> set[str]:
    """收集全部模块级函数 / 类 / 常量名（跨模块统一重命名对象）。"""
    names: set[str] = set()
    for tree in trees:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def collect_keyword_args(trees: list[ast.AST]) -> set[str]:
    """收集全部关键字参数名，避免重命名与参数名冲突。"""
    kwargs: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg:
                kwargs.add(node.arg)
    return kwargs


def collect_attr_names(trees: list[ast.AST]) -> set[str]:
    """收集全部属性访问名（x.name 形式，含模块属性调用）。

    属性访问在词法重命名中会被跳过（点号后的名字不改），若其
    对应的模块级定义被重命名，运行时将产生 AttributeError。
    因此这些名字必须从重命名候选中排除，保持定义与调用一致。
    """
    attrs: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                attrs.add(node.attr)
    return attrs


def build_rename_map(trees: list[ast.AST], rng: random.Random) -> dict[str, str]:
    """生成「原名 → 随机名」映射（跳过关键字参数名与双下划线名）。"""
    candidates = collect_top_level_names(trees)
    kwargs = collect_keyword_args(trees)
    # 排除被属性访问引用的名字：属性访问处不会重命名（见
    # apply_token_edits），定义处若改名将导致运行时 AttributeError
    candidates -= collect_attr_names(trees)
    used: set[str] = set()
    rename_map: dict[str, str] = {}
    for name in sorted(candidates):
        # 跳过：关键字参数同名、双下划线、与关键字参数冲突的名字
        if name in kwargs or name.startswith("__"):
            continue
        while True:
            new_name = f"_v{rng.randrange(16**8):08x}"
            if new_name not in used:
                used.add(new_name)
                rename_map[name] = new_name
                break
    return rename_map


def rename_fstring_names(literal: str, rename_map: dict[str, str]) -> str:
    """重命名 f-string 插值表达式中的标识符。

    f-string 在 tokenize 中整体是 STRING token，内部的 NAME 不会被常规
    词法重命名覆盖，需单独处理：仅处理 {...} 插值区域，按词边界替换。
    （不支持嵌套花括号的格式说明符，本项目代码中不存在该用法。）
    """
    # 分离前缀（f/r/b 等）与引号体
    quote_idx = min(
        (i for i, ch in enumerate(literal) if ch in "'\""), default=-1)
    if quote_idx < 0:
        return literal
    prefix, body = literal[:quote_idx], literal[quote_idx:]
    if "f" not in prefix.lower():
        return literal

    def rebuild(match: re.Match) -> str:
        inner = match.group(1)
        for old, new in rename_map.items():
            inner = re.sub(rf"\b{re.escape(old)}\b", new, inner)
        return "{" + inner + "}"

    return prefix + re.sub(r"\{([^{}]*)\}", rebuild, body)


def apply_token_edits(text: str, rename_map: dict[str, str]) -> str:
    """基于词法token重命名标识符并删除注释（签名与编码声明保留）。

    注意：属性访问（前面带点号的 NAME）不重命名——
    否则会误改 parser.parse_args() 这类内置方法调用。
    f-string 插值内的标识符由 rename_fstring_names 单独处理。
    """
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    # 收集单行编辑：(行号, 起列, 末列, 新文本)
    edits: list[tuple[int, int, int, str]] = []
    prev_significant = None  # 上一个有效 token（跳过注释/空行）(类型, 文本)
    for tok_type, tok_str, start, end, _line in tokens:
        if tok_type in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT,
                        tokenize.INDENT, tokenize.DEDENT):
            continue
        is_attr_access = (prev_significant is not None
                          and prev_significant[0] == tokenize.OP
                          and prev_significant[1] == ".")
        if (tok_type == tokenize.NAME and tok_str in rename_map
                and not is_attr_access):
            edits.append((start[0], start[1], end[1], rename_map[tok_str]))
        elif tok_type == tokenize.STRING:
            new_literal = rename_fstring_names(tok_str, rename_map)
            if new_literal != tok_str:
                edits.append((start[0], start[1], end[1], new_literal))
        elif tok_type == tokenize.COMMENT:
            keep = (tok_str.startswith("#!") or "-*-" in tok_str
                    or "天咏（明月照今日" in tok_str)
            if not keep:
                edits.append((start[0], start[1], end[1], ""))
        prev_significant = (tok_type, tok_str)
    # 按行分组，行内从右往左应用
    lines = text.splitlines(keepends=True)
    by_row: dict[int, list] = {}
    for row, col, ecol, new in edits:
        by_row.setdefault(row, []).append((col, ecol, new))
    for row, row_edits in by_row.items():
        line = lines[row - 1]
        # 统一换行符处理：编辑只作用于行内容
        newline = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
        body = line[: len(line) - len(newline)] if newline else line
        for col, ecol, new in sorted(row_edits, reverse=True):
            body = body[:col] + new + body[ecol:]
        lines[row - 1] = body + newline
    return "".join(lines)


def scramble_source(text: str, rename_map: dict[str, str]) -> str:
    """完整打乱流程：去文档字符串 → 重命名/去注释 → 语法自检。"""
    tree = ast.parse(text)
    text = remove_docstrings(text, tree)
    text = apply_token_edits(text, rename_map)
    ast.parse(text)  # 语法自检（失败将抛异常终止构建）
    return text


def compute_source_fingerprint(sources: dict[str, str]) -> str:
    """计算全部源码的联合 SHA-256 指纹（取前 16 位十六进制）。

    指纹基于打乱前的原始源码内容，同一份源码的任何改动都会
    改变指纹，用于版权水印与构建溯源。
    """
    import hashlib

    hasher = hashlib.sha256()
    for name in sorted(sources):
        hasher.update(name.encode("utf-8"))
        hasher.update(sources[name].encode("utf-8"))
    return hasher.hexdigest()[:16]


def stamp_build_placeholders(text: str, fingerprint: str, build_time: str) -> str:
    """将 main.py 中的构建占位符替换为真实指纹与构建时间。

    打乱后的 main.py 中占位符仍以普通字符串字面量保留，
    直接文本替换即可；该指纹即数字水印 APP_WATERMARK_ID 的组成部分。
    """
    text = text.replace('"@@BUILD_FINGERPRINT@@"', f'"{fingerprint}"')
    text = text.replace('"@@BUILD_TIME@@"', f'"{build_time}"')
    return text


def build_release(out_dir: Path, seed: int | None = None) -> None:
    """构建打乱发布版。"""
    rng = random.Random(seed)
    trees = []
    sources: dict[str, str] = {}
    for name in SOURCE_FILES:
        text = (BASE_DIR / name).read_text(encoding="utf-8")
        sources[name] = text
        trees.append(ast.parse(text))

    # 源码指纹与构建时间（版权水印组成部分，先计算后替换）
    from datetime import datetime

    fingerprint = compute_source_fingerprint(sources)
    build_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rename_map = build_rename_map(trees, rng)
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, text in sources.items():
        scrambled = scramble_source(text, rename_map)
        if name == "main.py":
            # 仅主入口替换构建占位符（水印标识 = 版本 + 源码指纹）
            scrambled = stamp_build_placeholders(scrambled, fingerprint, build_time)
        (out_dir / name).write_text(scrambled, encoding="utf-8")
        print(f"已打乱: {name}")

    # 复制启动脚本与图标资源
    for bat in BAT_FILES:
        shutil.copy2(BASE_DIR / bat, out_dir / bat)
        print(f"已复制: {bat}")
    for asset in ASSET_FILES:
        shutil.copy2(BASE_DIR / asset, out_dir / asset)
        print(f"已复制: {asset}")

    # 生成使用说明（含版权水印与构建指纹，用于权属标识）
    # 版本号从 main.py 源码中提取，避免说明与程序版本漂移
    version_match = re.search(r'APP_VERSION = "([^"]+)"', sources["main.py"])
    app_version = version_match.group(1) if version_match else "未知"
    usage = "\n".join([
        "iNaturalist 鸟类数据采集与图像下载系统（发布版）",
        "=" * 44,
        "作者签名：天咏（明月照今日，风摇过两川。）",
        f"版权声明：2026 天咏 版权所有（明月照今日，风摇过两川。）",
        f"数字水印：TYWM-BIRD-v{app_version}-{fingerprint}",
        f"构建指纹：{fingerprint}",
        f"构建时间：{build_time}",
        "",
        "【环境要求】",
        "  Windows 10/11 + Python 3.10 及以上（无需安装任何第三方库）",
        "",
        "【运行方式】",
        "  1. 双击「启动爬虫界面.bat」打开桌面控制台（推荐）",
        "     控制台内含「拖入文件爬取」标签页：可直接选择 txt/csv 处理，",
        "     或启动常驻监视 data/drop_inbox，放入文件即自动爬取。",
        "  2. 或命令行运行：python main.py            （查看全部命令）",
        "     python main.py batch                    （批量下载 15 个鸟类科）",
        "     python main.py gui                      （桌面控制台）",
        "     python main.py panel                    （浏览器数据面板）",
        "     python main.py stats                    （数据库统计）",
        "",
        "【拖入文件自动爬取】",
        "  1. 文本清单：将包含「类群ID 或 物种名」的 txt/csv 文件拖到",
        "     「拖入文件自动爬取.bat」图标上，即自动解析并爬取。",
        "     文件内容示例：",
        "         20967",
        "         Upupa epops",
        "  2. 本地图片入库：将图片文件夹（或若干图片文件）拖到同一",
        "     bat 图标上，按文件夹名/子文件夹名解析物种（如文件夹名",
        "     为「戴胜」或「20967」），图片按种类依次指纹编码入库：",
        "     内容 SHA-256 去重，重复图片自动跳过，源文件保留原位。",
        "  3. 桌面控制台「拖入文件爬取」标签页同样支持以上两种方式。",
        "",
        "【数据存储】",
        "  照片按种类存于 data/taxon_<ID>_<名称>/photos/",
        "  数据库存于 data/db/（raw_data.db 原始留存 +",
        "  bird_observations.db 规范主库，SHA-256 指纹自动去重）",
        "",
    ])
    (out_dir / "使用说明.txt").write_text(usage, encoding="utf-8")
    print("已生成: 使用说明.txt")
    print(f"\n发布版构建完成: {out_dir}")
    print(f"源码指纹: {fingerprint}  构建时间: {build_time}")
    print(f"数字水印: TYWM-BIRD-v{app_version}-{fingerprint}")
    print(f"重命名条目数: {len(rename_map)}")


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="项目打乱（混淆）发布版构建脚本")
    parser.add_argument("--out", type=Path, default=None,
                        help="输出目录（默认 上级目录/发布_混淆版_日期）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（可复现构建）")
    args = parser.parse_args()
    out_dir = args.out or BASE_DIR.parent / f"发布_混淆版_{time_str()}"
    build_release(out_dir, seed=args.seed)
    return 0


def time_str() -> str:
    """当前日期字符串。"""
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d")


if __name__ == "__main__":
    raise SystemExit(main())
