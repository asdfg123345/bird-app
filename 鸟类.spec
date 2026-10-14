# -*- mode: python ; coding: utf-8 -*-
# ============================================================
# 项目：鸟类 v1.5 PyInstaller 打包配置（含数字水印）
# 作者签名：天咏（明月照今日，风摇过两川。）
# 说明：onedir + console 模式（中文日志与子命令交互依赖控制台）；
#       model_trainer.py / model_library.py 不进 Analysis（torch 不打入
#       主程序），由构建脚本以数据文件形式部署到 exe 旁；
#       excludes 必须显式排除 torch/torchvision/model_trainer——
#       PyInstaller 6.18 会跟踪 bird_crawler_gui 中函数级 try 保护的
#       torch 导入，不排除会把 2GB+ torch 全套 DLL 打入安装包
#       （v1.4 修复项，26MB 级安装包为体积回归线）；
#       version 参数把 PE 版本资源（公司/版权/数字水印）写入 exe，
#       exe 文件属性「详细信息」页可检。
# ============================================================

import os

a = Analysis(
    ['打包源码/main.py'],
    pathex=['打包源码'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'model_trainer'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='鸟类',
    # PE 版本资源水印：文件属性详细信息页展示公司、版权与水印标识
    version=os.path.join(SPECPATH, '版本信息_v1.5.txt'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='鸟类',
)
