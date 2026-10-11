# -*- mode: python ; coding: utf-8 -*-
# ============================================================
# 项目：鸟类 v1.3 PyInstaller 打包配置
# 作者签名：天咏（明月照今日，风摇过两川。）
# 说明：onedir + console 模式（中文日志与子命令交互依赖控制台）；
#       model_trainer.py / model_library.py 不进 Analysis（torch 不打入
#       主程序），由构建脚本以数据文件形式部署到 exe 旁。
# ============================================================

a = Analysis(
    ['打包源码/main.py'],
    pathex=['打包源码'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
