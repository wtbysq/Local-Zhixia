"""桌面入口：把已有的本地 HTTP 服务装进一个 WebView 窗口。

四条设计约束：
1. **不重写界面** —— 窗口加载的就是 web/ 那套页面，只是地址换成 127.0.0.1 的临时端口。
2. **不碰核心逻辑** —— 只调用 server.main 的 start_in_thread / shutdown。
3. **关窗即回收** —— HTTP 线程与引擎子进程一起收掉，不留下占着显存的孤儿。
4. **有回退路径** —— WebView2 不可用时退回系统浏览器，功能不中断。

用法：
    python app.py              # 桌面窗口
    python app.py --browser    # 强制浏览器模式（调试前端时用）
    python app.py --selftest   # 只检查环境，不建窗口
"""
import argparse
import ctypes
import json
import os
import sys
import time
import urllib.request

from server import config, llm, models, snapshot, store
from server import main as srv

MUTEX_NAME = "Local\\LocalModelWorkbench.Instance"
WINDOW_STATE = os.path.join(config.DATA_DIR, "window.json")
APP_TITLE = "本地智匣"
APP_VERSION = "0.2"

_mutex_handle = None


# ---------------------------------------------------------------- 环境

def already_running():
    """命名互斥量做单实例。两个实例会各起一个引擎，8GB 显存直接爆。"""
    global _mutex_handle
    if os.name != "nt":
        return False
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() == 183      # ERROR_ALREADY_EXISTS


def notify(msg, title=APP_TITLE):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, msg, title, 0x40)
    else:
        print(msg)


def wait_ready(port, timeout=20):
    """等 HTTP 线程真的能应答，再开窗。否则窗口会白屏几秒。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.time() + timeout
    while time.time() < end:
        try:
            with op.open("http://127.0.0.1:%d/api/health" % port, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def webview2_present():
    paths = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     "Microsoft", "EdgeWebView", "Application"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Microsoft", "EdgeWebView", "Application"),
    ]
    for p in paths:
        if os.path.isdir(p) and any(x[:1].isdigit() for x in os.listdir(p)):
            return True
    return False


def load_window_state():
    try:
        with open(WINDOW_STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return max(880, int(d.get("w", 1180))), max(620, int(d.get("h", 820)))
    except Exception:
        return 1180, 820


def save_window_state(win):
    try:
        os.makedirs(os.path.dirname(WINDOW_STATE), exist_ok=True)
        with open(WINDOW_STATE, "w", encoding="utf-8") as f:
            json.dump({"w": int(win.width), "h": int(win.height)}, f)
    except Exception:
        pass


# ---------------------------------------------------------------- 前端可调用的 API

class Api:
    """只暴露桌面独有的三件事；其余能力前端直接走 HTTP。

    窗口控制不在这里：保留系统边框后，最小化/最大化/关闭由 Windows 负责。
    """

    def __init__(self, port):
        self._port = port
        self._win = None

    def bind(self, win):
        self._win = win

    # 能力适配
    def open_external(self, url):
        """外链交给系统浏览器，别让应用变成一个奇怪的浏览器。"""
        url = str(url or "")
        if not url.lower().startswith(("http://", "https://")):
            return False
        try:
            os.startfile(url)
            return True
        except Exception:
            return False

    def save_export(self, name, content):
        """导出走原生保存对话框，并返回真实落盘路径。"""
        import webview
        try:
            path = self._win.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=str(name or "export.md"))
        except Exception:
            return {"saved": None, "error": "对话框打开失败"}
        if isinstance(path, (list, tuple)):
            path = path[0] if path else None
        if not path:
            return {"saved": None}
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content or "")
            return {"saved": path}
        except Exception as exc:
            return {"saved": None, "error": str(exc)}

    def save_generated(self, url, name=""):
        """把生成的图片/视频另存到用户挑的位置。

        桌面包里 WebView2 不会为 <a download> 弹下载框，点「下载」看起来毫无反应。
        这里改走原生保存对话框：先按产物地址从本地服务取字节，再写进目标文件。
        走 HTTP 而不是直接拼磁盘路径，是为了不把目录结构复制到前端。
        """
        import webview
        url = str(url or "")
        if not url.startswith("/generated/"):
            return {"saved": None, "error": "无效的产物地址"}
        base = os.path.basename(url.split("?")[0]) or "output"
        save_name = str(name or base)
        if not os.path.splitext(save_name)[1]:
            save_name = base
        try:
            path = self._win.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=save_name)
        except Exception:
            return {"saved": None, "error": "对话框打开失败"}
        if isinstance(path, (list, tuple)):
            path = path[0] if path else None
        if not path:
            return {"saved": None}          # 用户取消
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open("http://127.0.0.1:%d%s" % (self._port, url), timeout=120) as r:
                data = r.read()
            with open(path, "wb") as f:
                f.write(data)
            return {"saved": path, "bytes": len(data)}
        except Exception as exc:
            return {"saved": None, "error": str(exc)[:200]}

    def open_folder(self, kind):
        """在资源管理器里打开产物目录。

        生成完的图/片子在 data\\图片 或 data\\视频 下，界面上只显示缩略图，
        用户想拿原文件必须能一键跳到目录——这是最高频的「文件存哪了」问题。
        """
        folder = config.gen_dir(str(kind or "image"))
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
            return {"opened": folder}
        except Exception as exc:
            return {"opened": None, "error": str(exc), "path": folder}

    def app_info(self):
        entries = llm.list_models()
        return {
            "mode": "desktop", "version": APP_VERSION, "port": self._port,
            "data_dir": config.DATA_DIR, "models_dir": models.models_dir(),
            "models": len(entries), "snapshot": snapshot.level(),
        }


# ---------------------------------------------------------------- 原生边框美化

# Windows 11 的 DWM 属性号。旧系统不认识会返回失败，此时保持系统默认配色，不影响使用。
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


def _colorref(r, g, b):
    """DWM 要的是 0x00BBGGRR，不是 RGB——这里写错会得到诡异的颜色。"""
    return (b << 16) | (g << 8) | r


FRAME_STYLE = {
    "caption": _colorref(0xEE, 0xF4, 0xF2),   # 标题栏：极浅青灰，和侧栏同一个色系
    "text": _colorref(0x0A, 0x4F, 0x44),      # 标题文字：深青
    "border": _colorref(0xBC, 0xDC, 0xD3),    # 外边框：主题的浅描边
}


def beautify_native_frame(hwnd):
    """保留系统边框，只改配色。

    不做无边框：拖拽、贴边分屏、高 DPI 缩放、系统阴影全部交给 Windows，
    我们只覆盖标题栏/文字/边框三个颜色。这是「美化」而不是「重造」。
    """
    if os.name != "nt" or not hwnd:
        return False
    try:
        dwm = ctypes.windll.dwmapi
    except Exception:
        return False
    ok = False
    for attr, val in ((DWMWA_CAPTION_COLOR, FRAME_STYLE["caption"]),
                      (DWMWA_TEXT_COLOR, FRAME_STYLE["text"]),
                      (DWMWA_BORDER_COLOR, FRAME_STYLE["border"])):
        try:
            v = ctypes.c_uint(val)
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v),
                                         ctypes.sizeof(v)) == 0:
                ok = True
        except Exception:
            pass
    return ok


def _decorate_when_ready():
    """窗口创建是异步的，句柄要等一会儿才拿得到。"""
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    for _ in range(60):
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if hwnd:
            beautify_native_frame(hwnd)
            return
        time.sleep(0.1)


# ---------------------------------------------------------------- 回退：浏览器模式

def run_browser(url, httpd):
    notify("桌面窗口不可用，已改用系统浏览器打开。\n\n%s" % url)
    try:
        os.startfile(url)
    except Exception:
        pass
    print("已用浏览器打开：%s\n按 Ctrl+C 结束服务。" % url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown(httpd)
        llm.stop_engine()


# ---------------------------------------------------------------- 自检

def _selftest_body():
    print("=" * 58)
    print(" 桌面环境自检")
    print("=" * 58)
    ok = True

    print(" Python       %s" % sys.version.split()[0])
    print(" 基准目录     %s" % config.ROOT)
    print(" 数据目录     %s" % config.DATA_DIR)
    print(" 模型目录     %s" % models.models_dir())

    try:
        import webview
        print(" pywebview    %s" % getattr(webview, "__version__", "已安装"))
    except Exception as exc:
        ok = False
        print(" pywebview    [X] 不可用：%s" % exc)

    try:
        import clr  # noqa: F401
        print(" pythonnet    可用")
    except Exception as exc:
        ok = False
        print(" pythonnet    [X] 不可用：%s" % exc)

    has_wv2 = webview2_present()
    print(" WebView2     %s" % ("已安装" if has_wv2 else "[!] 未找到（会回退到浏览器）"))

    build = getattr(sys.getwindowsversion(), "build", 0)
    print(" 边框着色     %s" % ("Windows 11，可用 DWM 给标题栏配色"
                                if build >= 22000 else "系统低于 Win11，保持默认配色"))

    config.ensure_dirs()
    store.init_db()
    httpd, port, _th = srv.start_in_thread("127.0.0.1", 0)
    ready = wait_ready(port, timeout=10)
    print(" 临时端口     %d %s" % (port, "就绪" if ready else "[X] 未就绪"))
    srv.shutdown(httpd)
    ok = ok and ready

    print("-" * 58)
    print(" 结论：%s" % ("桌面环境可用" if ok else "存在阻断项，见上方 [X]"))
    print("=" * 58)
    return ok


def selftest():
    """窗口模式没有控制台，所以要自己把结论收起来再给出。"""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = _selftest_body()
    text = buf.getvalue()

    try:
        print(text)
    except Exception:
        pass
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        with open(os.path.join(config.LOG_DIR, "desktop-selftest.txt"), "w",
                  encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        notify(text)
    return 0 if ok else 1


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="本地模型工作台（桌面版）")
    ap.add_argument("--browser", action="store_true", help="强制用浏览器打开")
    ap.add_argument("--selftest", action="store_true", help="只做环境自检")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if already_running():
        notify("已经有一个窗口在运行了。\n请在任务栏切换到它，而不是再开一个——"
               "两个实例会各自加载一次模型，显存会直接占满。")
        return 1

    config.ensure_dirs()
    store.init_db()
    restored = srv.apply_persisted_settings()
    httpd, port, _th = srv.start_in_thread("127.0.0.1", 0)   # 端口交给系统分配
    if not wait_ready(port):
        srv.shutdown(httpd)
        notify("服务启动失败：20 秒内没有就绪。\n\n日志：%s"
               % os.path.join(config.LOG_DIR, "app.log"))
        return 1

    url = "http://127.0.0.1:%d/" % port
    print("服务已就绪 %s（恢复默认参数 %d 项）" % (url, restored))

    if args.browser:
        run_browser(url, httpd)
        return 0

    try:
        import webview
    except Exception as exc:
        run_browser(url, httpd)
        print("原因：%s" % exc)
        return 0

    if not webview2_present():
        print("提示：未检测到 WebView2 运行时，尝试启动窗口…")

    api = Api(port)
    w, h = load_window_state()
    window = webview.create_window(
        APP_TITLE, url, js_api=api,
        width=w, height=h, min_size=(880, 620),
        background_color="#fbfbfa",
    )
    api.bind(window)
    try:
        # 第二个参数是 GUI 就绪后执行的函数：在那里给系统标题栏上色
        webview.start(_decorate_when_ready, debug=False)
    except Exception as exc:
        print("窗口启动失败：%s" % exc)
        run_browser(url, httpd)
        return 0
    finally:
        save_window_state(window)
        srv.shutdown(httpd)
        llm.stop_engine()
    return 0


if __name__ == "__main__":
    sys.exit(main())
