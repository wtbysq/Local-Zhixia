"""把源码打包成「本地智匣」文件夹。

产物结构：
    本地智匣/
      本地智匣.exe          ← 双击即用
      models/               ← 放 .gguf 的地方，用户自己往里丢
      runtime/llama/        ← 推理引擎与 DLL，不要动
      web/                  ← 界面文件
      config.json           ← 唯一需要手改的配置
      data/                 ← 首次运行自动生成（数据库、快照、日志）

源码仍留在工作台，本脚本只负责产出这一个可分发的目录。

两种模式：
    默认      继承上一次构建的 data/，会话、快照、生成的图都留着（自己升级用）
    --clean   丢掉全部运行痕迹，产出可以直接发给别人的干净包
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "本地智匣"
SPEC = "本地智匣.spec"
DIST = os.path.join(ROOT, NAME)

CLEAN = "--clean" in sys.argv[1:]

README = """本地智匣
========

一、怎么启动
  双击「本地智匣.exe」。
  第一次对话时需要把模型读进显存，大约等 4–10 秒，窗口里会显示「正在加载模型」。
  这不是卡死。

二、怎么加模型
  models\\ 下分四类：
  语言模型：把 .gguf 放进 models\\text\\
  图片模型：在 models\\image\\ 下建子目录（如 myanima），放一个 bundle.json
  视频模型：在 models\\video\\ 下建子目录，同上
  组件文件：扩散主干 models\\components\\diffusion\\
            VAE models\\components\\vae\\、音频 VAE models\\components\\audio_vae\\
            文本编码器 models\\components\\text_encoders\\
  bundle.json 的 components 里只写文件名，程序会先在模型目录找，找不到再去
  components\\ 对应角色的目录找——同一份 VAE 多个模型可以共用，省磁盘。
  放好重启程序，就能在「参数 → 模型」里看到。

  参考图与蒙版（图片工作区）：
    拖一张图进「参考素材」就是图生图，文字仍是主控。
    「重绘强度」0.15 几乎只改细节，0.5 明显变化，0.7 以上基本重画。
    点「涂抹蒙版」涂出要重画的地方，只有涂过的区域会变，其余保持原样。
    涂得越小、提示词越具体，效果越自然。
  图生视频：切到「视频」，选 wan2.2-ti2v-5b，图片当首帧即可。

  程序不下载模型，也不联网。

  8GB 显存建议：语言 8B + Q4_K_M + 8k 上下文；图片 SD1.5 512×512 约 12 秒；
  视频 Wan2.1-1.3B 用 256×256 / 9 帧 / 20 步，约 32 秒。
  视频的分水岭是分辨率：256² 32 秒不降级；320² 88 秒、384² 105 秒，
  后两个 VAE 会显存不足而降级分块，片子能出但慢三倍。
  步数别低于 20，否则画面糊成色块。想要更大画面先减帧数，别先加分辨率。
  显存估算在参数面板里直接显示，装不下会标红。

三、数据在哪
  data\\app.db          会话与消息
  data\\snapshots\\      每次请求的快照（参数、耗时、上下文决策）
  logs\\                日志
  整个 data 目录可以直接备份；换机器时把它和 models 一起拷走即可。

四、常见问题
  1) 双击没反应：看 logs\\app.log；或右键「以管理员身份运行」试试。
  2) 提示模型装不下：参数里把上下文降到 4k，或把 config.json 的 kv_cache 改成 q4_0。
  3) 想换引擎：改 config.json 的 adapter 与 adapters 段，无需改代码。
  4) 杀毒软件报警：这是 PyInstaller 打包的程序常见的误报，加信任即可。

五、目录说明
  models\\            放模型的地方
    models\\text\\      语言模型
    models\\image\\     图片模型（每个子目录一个 bundle.json）
    models\\video\\     视频模型（同上）
    models\\components\\ 共享组件池（diffusion / vae / audio_vae / text_encoders）
  config.json        端口、上下文、采样参数、快照密度（直接改这个）
  data\\              运行后自动生成：
    data\\图片\\       生成的图片（.png），每张旁边有同名 .json 记录参数
    data\\视频\\       生成的视频（.webm，容器可用 config.json 的 video_format 改）
    data\\app.db       会话与消息
  _internal\\        程序本体：Python 运行时、界面文件、推理引擎
    _internal\\runtime\\llama\\   语言引擎 llama-server
    _internal\\runtime\\sdcpp\\   图片/视频引擎 sd-cli
    _internal\\web\\             界面文件
  需要替换引擎时，也可以在根目录建一个 runtime\\llama\\（或 runtime\\sdcpp\\）
  放新版本，程序会优先使用 exe 旁边的那份。
"""


def run_pyinstaller(dist_dir):
    cmd = [
        sys.executable, "-m", "PyInstaller", SPEC,
        "--noconfirm",
        "--distpath", dist_dir,
        "--workpath", os.path.join(ROOT, "build"),
    ]
    print("执行：%s" % " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        raise SystemExit("打包失败，退出码 %d" % proc.returncode)


def link_models():
    """把工作台的模型以硬链接方式放进产物，避免再占一份磁盘。

    递归处理子目录，镜像源目录结构：
    语言模型在 models/text/，图片模型在 models/image/，
    视频模型是 models/video/<名字>/ 这样一个含 bundle.json 的目录。
    """
    src_root = os.path.join(ROOT, "models")
    dst_root = os.path.join(DIST, "models")
    os.makedirs(dst_root, exist_ok=True)
    linked = 0
    for dirpath, _dirs, files in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        target_dir = dst_root if rel == "." else os.path.join(dst_root, rel)
        os.makedirs(target_dir, exist_ok=True)
        for fn in files:
            src = os.path.join(dirpath, fn)
            dst = os.path.join(target_dir, fn)
            if os.path.exists(dst):
                continue
            try:
                os.link(src, dst)          # 同盘硬链接：瞬间完成，不占空间
            except OSError:
                shutil.copy2(src, dst)     # 跨盘时才真复制
            linked += 1
    if not linked:
        with open(os.path.join(dst_root, "把模型放在这里.txt"), "w",
                  encoding="utf-8") as f:
            f.write("把模型文件放进对应的子文件夹，然后重启本地智匣。\n"
                    "  models\\text\\        语言模型（.gguf）\n"
                    "  models\\image\\       图片模型：一个子目录一个配方（bundle.json）\n"
                    "  models\\video\\       视频模型：同上\n"
                    "  models\\components\\  共享组件池，按角色分：\n"
                    "                        diffusion / vae / audio_vae / text_encoders\n"
                    "多组件模型在 bundle.json 里只写文件名，程序先在模型目录找，\n"
                    "找不到再去 components\\ 下对应角色的目录找，同一份组件可被多个模型复用。\n"
                    "程序不下载模型，也不会联网。\n")
    return linked


def to_recycle_bin(path):
    """把旧产物送进回收站，而不是直接删掉。

    打包会反复覆盖同一个目录，直接 rmtree 既危险也容易被安全策略拦截；
    送回收站既可还原，也不会误伤。
    """
    if os.name != "nt" or not os.path.isdir(path):
        return False
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.wFunc = 3                                     # FO_DELETE
    op.pFrom = path + "\0\0"                         # 必须以双 NUL 结尾
    op.fFlags = 0x40 | 0x10 | 0x04                   # ALLOWUNDO | NOCONFIRMATION | SILENT
    try:
        return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0
    except Exception:
        return False


def clean_runtime_traces(dist):
    """清掉开发期跑出来的痕迹：会话库、快照、生成的图、日志、模型缓存。

    默认模式不动 data/（升级时用户的会话与产物必须活着）；
    --clean 模式才调用它，产出可以直接发给别人的包。
    """
    removed = []
    data = os.path.join(dist, "data")
    if os.path.isdir(data):
        shutil.rmtree(data, ignore_errors=True)
        removed.append("data")
    logs = os.path.join(dist, "logs")
    if os.path.isdir(logs):
        for fn in os.listdir(logs):
            try:
                os.remove(os.path.join(logs, fn))
            except OSError:
                pass
        removed.append("logs/*")
    return removed


def main():
    print("模式：%s" % ("--clean（干净分发包）" if CLEAN else "默认（保留上次的 data）"))

    # 在临时目录里全新构建：PyInstaller 的 COLLECT 会先清空目标目录，
    # 直接指向 本地智匣/ 会触发批量删除保护，也让旧产物暴露在风险里。
    stage = tempfile.mkdtemp(prefix="_stage_", dir=ROOT)

    # 用户的 data 目录必须活着穿过这次替换：里面是会话数据库、
    # 请求快照、以及他熬夜生成的图片和视频。丢了不可接受。
    # --clean 是例外：它的目的就是交出一个没有运行痕迹的包。
    keep_data = os.path.join(ROOT, "_keep_data")
    had_data = False
    if not CLEAN and os.path.isdir(os.path.join(DIST, "data")):
        if os.path.isdir(keep_data):
            shutil.rmtree(keep_data)
        shutil.move(os.path.join(DIST, "data"), keep_data)
        had_data = True
        print("暂存用户数据：%s" % keep_data)

    try:
        run_pyinstaller(stage)
        built = os.path.join(stage, NAME)
        if not os.path.isdir(built):
            raise SystemExit("构建产物缺失：%s" % built)

        if os.path.isdir(DIST):
            moved = to_recycle_bin(DIST)
            print("旧产物已%s：%s" % ("送入回收站" if moved else "[!] 未能移除", DIST))
            if not moved:
                raise SystemExit("旧产物无法移除，请手动把 本地智匣 文件夹删掉再重试")

        os.rename(built, DIST)       # 同盘重命名，瞬时完成
        print("产物就位：%s" % DIST)
    finally:
        try:
            os.rmdir(stage)          # 此时应为空目录
        except OSError:
            pass

    if had_data and os.path.isdir(keep_data):
        dst_data = os.path.join(DIST, "data")
        if os.path.isdir(dst_data):
            shutil.rmtree(dst_data)          # 新产物里的空壳，用旧的替掉
        shutil.move(keep_data, dst_data)
        print("用户数据已恢复：%s" % dst_data)

    n = link_models()
    # config.json 复制一份到根部：用户要改的就是它，不能埋在 _internal 里
    src = os.path.join(DIST, "_internal", "config.json")
    if os.path.isfile(src):
        shutil.copy2(src, os.path.join(DIST, "config.json"))

    if CLEAN:
        gone = clean_runtime_traces(DIST)
        print("已清理运行痕迹：%s" % ("、".join(gone) if gone else "无"))

    with open(os.path.join(DIST, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(README)

    size = sum(os.path.getsize(os.path.join(d, f))
               for d, _s, fs in os.walk(DIST) for f in fs)
    exe = os.path.join(DIST, NAME + ".exe")
    print("-" * 58)
    print("产物目录：%s" % DIST)
    print("可执行文件：%s" % ("存在" if os.path.isfile(exe) else "缺失"))
    print("模型链接：%d 个" % n)
    print("总体积：%.1f MB" % (size / 1024 / 1024))


if __name__ == "__main__":
    main()
