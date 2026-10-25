# 本地智匣

自用的本地推理工作台：**模型文件归你，上下文可见，每次请求可复现**。
完全离线运行，数据不出本机。当前版本 0.1（版本号只显示在窗口左下角）。

不是「又一个模型管理器」：它只管三件事——**资产登记、上下文管理、请求记录**；
推理交给可替换的引擎：

| 模态 | 引擎 | 说明 |
|---|---|---|
| 语言 | llama.cpp `llama-server` | 对话、摘要、上下文压缩 |
| 图片 | stable-diffusion.cpp `sd-cli` | SD1.5 / SDXL / FLUX（GGUF 或 safetensors） |
| 视频 | 同上 | Wan 2.1/2.2、HunyuanVideo、LTX |

两套引擎都是单二进制、零 Python 依赖，运行时随包携带。它们共享同一块显存，
**生成图片/视频前会自动卸载语言模型**，回到对话时再懒加载回来。

---

## 一、三种用法

### 1. 桌面版（给使用者）

```
双击  本地智匣\本地智匣.exe
```

首次对话需要把模型读进显存，等 4–15 秒（装完第一次会更久，杀毒软件在扫描新文件）。
窗口里会显示「正在加载模型」，不是卡死。

产物目录结构：

```
本地智匣\
  本地智匣.exe          双击这个
  models\               把模型放这里（用户自己导入）
    text\               语言模型（.gguf，一个文件一个模型）
    image\              图片模型：一个子目录一个配方（bundle.json）
    video\              视频模型：同上
    components\         共享组件池（diffusion / vae / audio_vae / text_encoders）
  config.json           唯一需要手改的配置
  使用说明.txt
  data\                 首次运行自动生成：数据库、快照、日志、生成产物
  _internal\            程序本体：Python 运行时、界面、两套推理引擎
```

### 2. 图片 / 视频生成

点左侧「图片」或「视频」，主区会切成生成工作区：

1. 在下方输入框写提示词
2. 右侧「参数」面板调分辨率、步数、CFG、种子（视频还有帧数与帧率）
3. 按 Enter 或点「生成」

进度是实时的：会依次显示「加载扩散模型 → 文本编码 → 第 N/M 步 → VAE 解码 → 写入文件」。
产物分开存放，打开目录就能找到：

```
data\图片\     生成的图片（.png）
data\视频\     生成的视频（.webm，可用 config.json 的 video_format 改容器）
```

每个产物旁边有一个同名 `.json`，记录当时的提示词与全部参数，可以照着复现同一条结果。

**首次生成会慢**（40–90 秒）：引擎要编译 Vulkan shader、把权重读进显存。
之后同一会话里每次生成 512×512/20 步约 **12 秒**，8 步约 **9 秒**。

**视频的分水岭是分辨率**，8GB 显存实测（Wan2.1-1.3B，9 帧 20 步）：

| 分辨率 | 耗时 | 说明 |
| --- | --- | --- |
| 256×256 | **32 秒** | 不触发降级，**推荐默认** |
| 320×320 | 88 秒 | VAE 显存不足，降级分块解码 |
| 384×384 | 105 秒 | VAE 两次 OOM 降级到 192 分块，解码单独 63 秒 |

面积翻倍就会撑爆 VAE，引擎会自己降级分块把片子跑完，但耗时翻三倍。
步数不要低于 20：4–6 步出来的画面糊成色块。想要更大画面，先减帧数（9→5），别先加分辨率。
降级发生时，结果卡上会显示「VAE 显存不足，已降级分块解码」。

### 3. 开发版（改代码时用）

浏览器刷新即可看到效果，不必每次打包。

```
双击  启动.bat          # 或 python server/main.py
双击  自检.bat          # 环境与依赖体检
python app.py           # 用桌面窗口打开开发版
python app.py --selftest  # 只检查桌面环境，不建窗口
```

### 重新打包

```bash
python scripts/build.py            # 保留上一版的 data（自己升级用）
python scripts/build.py --clean    # 丢掉全部运行痕迹，产出可直接发人的干净包
```

流程：临时目录全新构建 → 旧产物送回收站（可还原）→ 新产物就位 →
把 `models/*.gguf` 以硬链接放进 `本地智匣/models`（同盘不占双份空间）→ 生成使用说明。

`--clean` 额外做三件事：不继承旧 `data/`、删掉产物里的 `data/`（会话库、
快照、生成的图）与 `logs/`、连 `models.json` 缓存一起清掉。
**给别人发包用 `--clean`，自己升级用默认模式。**


---

## 二、放模型

本程序**不下载模型**，按角色放进 `models/` 的四个位置：

| 位置 | 放什么 | 形式 |
|---|---|---|
| `models/text/` | 语言模型 | `.gguf`，一个文件一个模型 |
| `models/image/` | 图片模型 | 一个子目录一个配方，目录里放 `bundle.json` |
| `models/video/` | 视频模型 | 同上 |
| `models/components/` | 共享组件 | `diffusion/` 扩散主干、`vae/` 图像与视频 VAE、`audio_vae/` 音频 VAE、`text_encoders/` 文本编码器 |

**组件查找顺序**：`bundle.json` 的 `components` 里只写文件名，程序先在
该模型目录内找，找不到再去 `components/<角色>/` 找。所以：

- 同一个 VAE / 文本编码器只存一份，多个模型复用（`anima` 与 `krea2` 共用
  `components/vae/qwen_image_vae.safetensors`，省了 242 MB）
- 想单独把一个模型目录拷给别人时，把组件一起放回该目录即可，老布局照样能跑
- 顶层散放的 `.gguf` 仍按语言模型识别，但规范位置是 `models/text/`

`components` 下按角色分目录，角色名与 `bundle.json` 的键对应：

| bundle 键 | 池子目录 |
|---|---|
| `diffusion` / `model` | `components/diffusion/` |
| `vae` | `components/vae/` |
| `audio_vae` | `components/audio_vae/` |
| `llm` / `llm_vision` / `t5xxl` / `clip_l` / `clip_g` | `components/text_encoders/` |

启动时会自动解析 GGUF 头，读出真实结构（层数、KV 头数、head_dim、原生上下文）
并估算显存。当前这台机器：

| 项 | 值 |
|---|---|
| 模型 | Qwen3-8B-Q4_K_M（4.68 GB） |
| 结构 | 36 层 / 8 个 KV 头 / head_dim 128 / 原生上下文 40960 |
| ctx 8192 + KV q8_0 | 估算 7070 MiB，**实测 6996 MiB** |
| ctx 16384 + KV q8_0 | 估算 7646 MiB，**实测 7656 MiB**（只剩 ~500 MiB，临界） |
| ctx 32768 | 估算 8798 MiB，**装不下**，配置里的 `ctx_ceiling` 会直接挡住 |

显存公式：`权重 + 2 × 层数 × KV头数 × head_dim × ctx × 每元素字节 + 1700 MiB 附加开销`。
最后那个 1700 是本机实测的固定开销（CUDA 上下文 + 计算缓冲 + 桌面合成），
换机器可在 `config.json` 的 `vram_overhead_mb` 调。

---

## 三、会话与模型的关系（重要）

- **模型在会话创建时绑定，对话途中不能换。** 想换模型请点「新对话」。
  - 服务端会拒绝：`409 MODEL_LOCKED`
  - 界面侧下拉框会变成灰色并标注「已绑定，不可更换」
- 参数（温度、上下文、输出长度）随时可改，对下一次请求生效。
- 自动压缩只影响**发送视图**，数据库里的原始消息一条都不会删。

### 参数会记住（重启也不丢）

参数分两层，别混淆：

| 层 | 存在哪 | 影响谁 |
|---|---|---|
| **会话参数** | `sessions.params_json` | 只影响这一次对话 |
| **默认参数** | `settings` 表 | 决定**新建对话的起点**，重启后仍生效 |

界面上改任何参数，会同时写这两处：当前会话记住它的值，同时成为下次新建对话的默认值。
所以改完温度、触发阈值、保留轮数之后重启，打开新对话仍是改过的值，不会回到代码里的默认数。

`settings` 表是纯文本 kv，可以直接看、也可以备份：

```bash
sqlite3 data/app.db "select key,value from settings"
```

只有白名单里的键可以持久化（见 `server/config.py` 的 `PERSIST_KEYS`）；
传其他键会被 `rejected` 明确拒绝，而不是静默忽略。

---

## 四、配置（`config.json`）

只改这一个文件，改完重启服务生效。

| 键 | 作用 | 当前值 |
|---|---|---|
| `adapter` | 用哪个执行适配器：`llamacpp` 或 `openai` | `llamacpp` |
| `adapters.llamacpp.binary` | 引擎可执行文件（相对项目根） | `runtime\llama\llama-server.exe` |
| `adapters.llamacpp.kv_cache` | KV 缓存精度：`q8_0` / `q4_0` / `f16` | `q8_0` |
| `adapters.llamacpp.chat_template_kwargs` | 对话模板参数。Qwen3 关思考，否则正文会空 | `enable_thinking: false` |
| `ctx_max` | 默认上下文 | `8192` |
| `ctx_ceiling` | 硬上限，任何请求都不能超过 | `16384` |
| `snapshot.level` | 请求快照密度：`off` / `basic` / `full` | `basic` |
| `vram_overhead_mb` | 权重与 KV 之外的固定开销 | `1700` |
| `video_format` | 视频容器：`webm`（真视频）/ `webp`（动画图）/ `avi`（老格式，最大） | `webm` |

### 换引擎

改 `adapter` 与对应配置即可，核心代码不动：

```json
"adapter": "openai",
"adapters": { "openai": { "base": "http://127.0.0.1:1234/v1" } }
```

任何暴露 `/v1/chat/completions` 的服务都能接（另一台机器上的 vLLM、别人的 API、
测试用 mock 服务）。本程序不会替它启动进程。

---

## 五、请求快照（可复现性）

每次生成都会在 `data/snapshots/YYYY-MM-DD.jsonl` 追加一条记录，密度由
`snapshot.level` 决定：

| 档位 | 内容 | 用途 |
|---|---|---|
| `off` | 不写 | 只聊天 |
| `basic` | 模型、参数、上下文用量、压缩决策、首字延迟、tok/s、结束原因 | 复盘「为什么慢 / 为什么忘了前文」 |
| `full` | 额外保存实际发送的消息序列与摘要全文 | 重放、对比参数 |

界面参数面板里可直接切换。查看：

```bash
curl --noproxy "*" "http://127.0.0.1:8787/api/snapshots?limit=10"
```

---

## 六、目录

```
启动.bat / 自检.bat      入口
config.json              唯一需要手改的配置
server/
  adapters/              执行层：base 契约 + llamacpp（托管进程）+ openai
  models.py              资产层：GGUF 解析、指纹、显存估算
  context.py             上下文层：预算、压缩、三级降级
  snapshot.py            请求快照
  store.py               SQLite（WAL）
  main.py                HTTP + SSE，零依赖
web/                     原生 JS，无构建、无 CDN
runtime/llama/           llama-server 运行时（已含 CUDA 库，可整体搬迁）
models/                  text/ image/ video/ 三类 + components/ 共享组件池
data/                    app.db、models.json 缓存、snapshots/
logs/                    app.log（结构化）、engine.log（引擎原始输出）
scripts/                 mock_llm.py、smoke.py（66 项断言）、build.py、doctor.py
本地智匣.spec            PyInstaller 配置（onedir）
```

> 缓存说明：`data/models.json` 以绝对路径为键。项目目录改名或模型挪位置后，
> 旧条目会被自动剔除（`models.scan()` 保存前会检查文件是否还在），
> 不需要手工删缓存；万一出现幽灵条目，删掉这个文件重启即可重建。

---

### 本机已部署的模型（8 个，均实测过）

| 模型 | 类型 | 体积 | 实测 | 备注 |
|---|---|---|---|---|
| `Ornith-1.5-9B-Q4_K_M` | 语言 | 5.38 GB | 31.3 tok/s @8k，显存 6561 MiB | GGUF arch `qwen35`（Gated DeltaNet 混合栈），只有 9 层留 KV，所以 16k 也放得下 |
| `Qwen3-8B-Q4_K_M` | 语言 | 4.68 GB | 28 tok/s | 老基线 |
| `sd-v1-5-fp16` | 图片 | 1.99 GB | 512²/20 步 12 s | 快，质量一般 |
| `anima`（Turbo v1.1） | 图片 | 5.24 GB | 512²/8 步 21–47 s | 2B 动漫向，Danbooru 标签吃得好 |
| `krea2`（Turbo int8） | 图片 | 17.33 GB | 512²/8 步 **164 s** | 画质最好，13.5 GB 权重超显存，必须 offload |
| `qwen-image-2.1`（Turbo int8） | 图片 | 16.10 GB | 512²/8 步 **110 s** | 支持 RGBA 透明图与图片编辑，同样要 offload |
| `wan2.1-t2v-1.3b` | 视频 | 5.01 GB | 256²/9 帧/20 步 32 s | 老基线 |
| `minimax-h3`（Q2_K） | 视频+音频 | 21.65 GB | 480×256/124 帧/4 步 **50.6 min** | 音视频联合生成，出 5.13 s 带音轨 WebM；画质粗糙，属于"能跑"级别 |

三个超显存模型（`krea2` / `qwen-image-2.1` / `minimax-h3`）的 bundle.json 里都带：

```json
"extra_args": ["--offload-to-cpu", "--backend", "te=cpu,vae=cpu,diffusion=Vulkan0"]
```

**为什么必须这么写**：只加 `--offload-to-cpu` 时，13 GB 的文本编码器仍会被塞进 8 GB 显存，引擎直接 `device lost` 退出。把文本编码器与 VAE 显式钉在 CPU、只让去噪器上显卡，才稳定。换机器时按 `sd-cli --list-devices` 的实际设备名改这段。

---

## 七、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 卡在「正在加载模型」十几秒 | 正常，首次加载要读 4.7GB 权重 | 等；`logs/engine.log` 有进度 |
| `ENGINE_BINARY_MISSING` | `binary` 路径不对 | 跑 `自检.bat` 看具体路径 |
| 引擎启动即退出，退出码 `-1073741515` | 缺 DLL（0xC0000135） | CUDA 库必须在二进制同目录 |
| `VRAM_INSUFFICIENT` | 估算超显存，被提前拦住 | 按提示降 `ctx_max` 或改 `kv_cache: q4_0` |
| 回答是空的 | 模型在「思考」，内容在 `reasoning_content` | 参数面板勾「展示思考过程」；默认已关思考 |
| `MODEL_LOCKED` | 会话已绑定模型 | 新建对话再选模型 |
| 文字整段蹦出 | 被中间代理缓冲 | 本实现已带 `X-Accel-Buffering: no` |
| 端口占用 | 上次进程没退 | 关掉旧窗口；或改 `port` |

---

## 八、验证改动没坏

不需要真模型，用 mock 跑完整链路：

```bash
python scripts/mock_llm.py --port 11999
ADAPTER=openai LLM_BASE=http://127.0.0.1:11999/v1 python server/main.py
python scripts/smoke.py --base http://127.0.0.1:8787
```

当前基线：**66 / 66 通过**（流式逐帧、自动压缩、压缩不删原文、中断落盘、
导出、模型绑定、请求快照、批量删除、默认参数持久化、生成参数校验与产物登记、
显存估算、压缩降级链）。
