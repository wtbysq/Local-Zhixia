# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：产出「本地智匣」文件夹（onedir）。

为什么不是 onefile：
  引擎运行时约 1 GB，单文件模式每次启动都要把它解压到 %TEMP%，
  冷启动会从 3 秒变成几十秒。onedir 解压即用，升级时也只替换 exe 与 web/。
"""
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.getcwd())

datas = [
    (os.path.join(ROOT, "web"), "web"),
    (os.path.join(ROOT, "config.json"), "."),
    # 两套引擎：llama.cpp 跑语言模型，stable-diffusion.cpp 跑图片/视频
    (os.path.join(ROOT, "runtime", "llama"), os.path.join("runtime", "llama")),
    (os.path.join(ROOT, "runtime", "sdcpp"), os.path.join("runtime", "sdcpp")),
]

binaries = []
hiddenimports = [
    "webview.platforms.edgechromium",
    "clr",
    "clr_loader",
]

# pywebview 靠 platforms 子模块与自带的 WebView2 DLL 工作，
# 静态分析追不到，必须整包收进来
for pkg in ("webview",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["app.py"],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="本地智匣",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "desktop", "本地智匣.ico"),
    version=os.path.join(ROOT, "desktop", "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="本地智匣",
)
