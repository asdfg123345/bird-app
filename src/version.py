#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================
"""版本与权属信息的唯一来源（single source of truth）。

发版约定（只改一处）：
    1. 修改本文件的 APP_VERSION；
    2. 运行 python packaging/准备打包源码.py —— 注入构建指纹/时间并校验 .iss；
    3. 运行 python main.py --version 确认输出。

引用关系：
    main.py（水印横幅、--version）与 bird_crawler_gui.py（窗口标题）均从本文件导入，
    不再各自硬编码版本号，避免出现「代码 1.5.1 / git 标签 1.7.0」的漂移。

构建脚本约束（勿改格式）：
    build_scrambled_release.py 与 build_hybrid_encrypted_release.py 会用
    `^APP_VERSION = "..."` 正则从本文件读取版本号，因此赋值语句必须顶格
    单独成行（文档字符串里也不要出现同形的赋值示例，避免被正则抢先匹配）；
    本文件也必须出现在两个脚本的 SOURCE_FILES 名单里，否则加密发布版会因
    缺少 version 模块而启动失败。
"""

from __future__ import annotations

# 语义化版本：主版本.次版本.修订号（发版时只改这一行）
APP_VERSION = "1.7.1"
APP_AUTHOR = "天咏"
APP_AUTHOR_SIGNATURE = "明月照今日，风摇过两川。"
# 控制台输出的版权行不含 ©（Windows GBK 控制台无法编码该字符）；
# PE 版本资源中的版权行由版本信息文件独立定义（含 ©）。
APP_COPYRIGHT = "2026 天咏 版权所有（明月照今日，风摇过两川。）"
