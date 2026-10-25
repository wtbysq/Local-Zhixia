"""按清单从 ModelScope 下载模型，支持断点续传。

为什么单独写一个而不是用现成工具：目标机器完全离线、零 pip 依赖，
这份脚本只用标准库，拷到哪台机器都能跑；断点续传写在 .part 上，
网络断了重跑继续，不会从头再来。

用法：
    python scripts/fetch_models.py            # 下载清单里还没下完的
    python scripts/fetch_models.py --list     # 只看清单与体积
    python scripts/fetch_models.py --only ornith,anima

清单里的 dest 都是相对项目根目录的路径。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://modelscope.cn/models/%s/resolve/master/%s"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 关掉代理：本机开着 HTTP_PROXY，走代理到 ModelScope 反而慢/不通
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

GB = 1024 ** 3

JOBS = [
    # 视频：MiniMax-H3 音视频联合。本机 15 GB 内存 + 33 GB 页面文件，
    # 用最小量化（Q2 去噪器 + Q2 文本编码器 + int8 VAE）才谈得上能跑；
    # ComfyUI 那边能出低质量小视频，就是靠逐层卸载 + 小分辨率 + 124 帧起步。
    {"name": "h3-diff", "repo": "unsloth/MiniMax-H3-GGUF",
     "file": "minimax_h3_fl2va_pruned-Q2_K.gguf",
     "dest": "models/components/diffusion/minimax_h3_fl2va_pruned-Q2_K.gguf", "size": 6720000000},
    {"name": "h3-te", "repo": "unsloth/MiniMax-H3-GGUF",
     "file": "qwen3vl_32b_minimax_h3-Q2_K_M.gguf",
     "dest": "models/components/text_encoders/qwen3vl_32b_minimax_h3-Q2_K_M.gguf", "size": 13100000000},
    {"name": "h3-vae", "repo": "Comfy-Org/MiniMax-H3",
     "file": "vae/minimax_h3_video_vae_int8_convrot.safetensors",
     "dest": "models/components/vae/minimax_h3_video_vae_int8_convrot.safetensors", "size": 2810000000},
    {"name": "h3-avae", "repo": "Comfy-Org/MiniMax-H3",
     "file": "vae/minimax_h3_audio_vae_fp32.safetensors",
     "dest": "models/components/audio_vae/minimax_h3_audio_vae_fp32.safetensors", "size": 610000000},

    # 图片：Krea 2 Turbo int8 + Qwen3-VL 4B int8 + Qwen-Image VAE
    {"name": "krea2-diff", "repo": "Comfy-Org/Krea-2",
     "file": "diffusion_models/krea2_turbo_int8_convrot.safetensors",
     "dest": "models/components/diffusion/krea2_turbo_int8_convrot.safetensors", "size": 13490000000},
    {"name": "krea2-te", "repo": "Comfy-Org/Krea-2",
     "file": "text_encoders/qwen3vl_4b_int8_convrot.safetensors",
     "dest": "models/components/text_encoders/qwen3vl_4b_int8_convrot.safetensors", "size": 4860000000},
    {"name": "krea2-vae", "repo": "Comfy-Org/Krea-2",
     "file": "vae/qwen_image_vae.safetensors",
     "dest": "models/components/vae/qwen_image_vae.safetensors", "size": 250000000},

    # 图片：Qwen-Image 2.1 Turbo int8（7B DiT）+ Qwen3-VL 8B int8 + 专用 VAE
    {"name": "qwen21-diff", "repo": "Comfy-Org/Qwen-Image-2.1",
     "file": "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
     "dest": "models/components/diffusion/qwen_image_2.1_turbo_int8_convrot.safetensors",
     "size": 7260000000},
    {"name": "qwen21-te", "repo": "Comfy-Org/Qwen-Image-2.1",
     "file": "text_encoders/qwen3vl_8b_int8_convrot.safetensors",
     "dest": "models/components/text_encoders/qwen3vl_8b_int8_convrot.safetensors",
     "size": 9350000000},
    {"name": "qwen21-vae", "repo": "Comfy-Org/Qwen-Image-2.1",
     "file": "vae/qwen_image_2.1_vae_bf16.safetensors",
     "dest": "models/components/vae/qwen_image_2.1_vae_bf16.safetensors",
     "size": 680000000},

    # 局部重绘：SD1.5-inpaint 是单文件（UNet + VAE + CLIP 全在一个 ckpt 里），
    # sd.cpp 直接 -m 加载。放在模型目录内而不是组件池：它自包含，拷走就能用。
    {"name": "sd15-inpaint", "repo": "AI-ModelScope/stable-diffusion-inpainting",
     "file": "sd-v1-5-inpainting.ckpt",
     "dest": "models/image/sd1.5-inpaint/sd-v1-5-inpainting.ckpt", "size": 4260000000},

    # 图生视频：Wan2.2 TI2V-5B 是 t2v + i2v 的统一模型。
    # 为什么不选 Wan2.1 i2v：那个只有 14B（fp8 也要 13GB），8GB 显存根本进不去；
    # 5B 的 Q4_K_M 只有 3.2GB，配合 te=cpu 能跑。
    {"name": "wan22-diff", "repo": "unsloth/Wan2.2-TI2V-5B-GGUF",
     "file": "Wan2.2-TI2V-5B-Q4_K_M.gguf",
     "dest": "models/components/diffusion/Wan2.2-TI2V-5B-Q4_K_M.gguf",
     "size": 3436000000},
    # 5B 用的是专用 VAE，2.1 的换不了
    {"name": "wan22-vae", "repo": "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
     "file": "split_files/vae/wan2.2_vae.safetensors",
     "dest": "models/components/vae/wan2.2_vae.safetensors", "size": 1406000000},
]


def human(n):
    if n >= GB:
        return "%.2f GB" % (n / GB)
    return "%.1f MB" % (n / 1024 ** 2)


def remote_size(url):
    """问服务器要真实长度。清单里的 size 只是估计值，差几 MB 就会让续传越界。"""
    try:
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        with OPENER.open(req, timeout=30) as r:
            n = int(r.headers.get("Content-Length") or 0)
            return n if n > 0 else 0
    except Exception:
        return 0


def fetch(job):
    dest = os.path.join(ROOT, job["dest"])
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    url = BASE % (job["repo"], job["file"])
    want = remote_size(url) or job.get("size") or 0

    if os.path.isfile(dest) and (not want or abs(os.path.getsize(dest) - want) < 1024 ** 2):
        print("[skip] %-12s 已存在 %s" % (job["name"], human(os.path.getsize(dest))), flush=True)
        return True

    have = os.path.getsize(part) if os.path.isfile(part) else 0
    if want and have >= want:
        os.replace(part, dest)
        print("[done] %-12s 续传完成 %s" % (job["name"], human(have)), flush=True)
        return True

    for attempt in range(1, 7):
        try:
            headers = dict(UA)
            if have:
                headers["Range"] = "bytes=%d-" % have
            req = urllib.request.Request(url, headers=headers)
            t0 = time.time()
            with OPENER.open(req, timeout=90) as r:
                mode = "ab" if (have and r.status == 206) else "wb"
                if mode == "wb":
                    have = 0
                total = want or (have + int(r.headers.get("Content-Length") or 0))
                got = have
                last = time.time()
                with open(part, mode) as f:
                    while True:
                        chunk = r.read(1024 * 512)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        now = time.time()
                        if now - last >= 5:
                            sp = (got - have) / (now - t0) / 1e6
                            pct = (got / total * 100) if total else 0
                            print("       %-12s %5.1f%%  %s / %s  %.2f MB/s" % (
                                job["name"], pct, human(got), human(total), sp), flush=True)
                            last = now
            if total and os.path.getsize(part) < total:
                raise IOError("长度不足（%d/%d）" % (os.path.getsize(part), total))
            os.replace(part, dest)
            print("[ok]   %-12s %s 用时 %.0fs" % (job["name"], human(os.path.getsize(dest)),
                                                 time.time() - t0), flush=True)
            return True
        except urllib.error.HTTPError as exc:
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            # 416 = 请求的区间已经越过文件末尾，说明续传起点 >= 实际长度，当作下完了
            if exc.code == 416 and have:
                os.replace(part, dest)
                print("[ok]   %-12s 416 视为已完成 %s" % (job["name"], human(have)), flush=True)
                return True
            print("[retry %d] %-12s HTTP %s（已下 %s）" % (attempt, job["name"], exc.code,
                                                         human(have)), flush=True)
            time.sleep(3 * attempt)
        except Exception as exc:
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            print("[retry %d] %-12s %s（已下 %s）" % (attempt, job["name"],
                                                     str(exc)[:90], human(have)), flush=True)
            time.sleep(3 * attempt)
    print("[fail] %-12s 放弃" % job["name"], flush=True)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    jobs = JOBS
    if args.only:
        keys = {x.strip() for x in args.only.split(",") if x.strip()}
        jobs = [j for j in JOBS if j["name"] in keys or j["name"].split("-")[0] in keys]

    if args.list:
        for j in jobs:
            print("%-12s %10s  %s" % (j["name"], human(j.get("size") or 0), j["dest"]))
        return

    print("清单：%d 个文件，合计 %s" % (len(jobs), human(sum(j.get("size") or 0 for j in jobs))), flush=True)
    ok = 0
    for j in jobs:
        if fetch(j):
            ok += 1
    print("完成 %d / %d" % (ok, len(jobs)), flush=True)


if __name__ == "__main__":
    sys.exit(0 if main() is None else 1)
