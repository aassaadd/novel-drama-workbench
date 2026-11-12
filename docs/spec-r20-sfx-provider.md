# spec-r20：音效（SFX）provider —— 本地 Stable Audio 3 Small-SFX

状态：已实现并实测出音（2026-11-12）；页面尚未接入。

## 1. 目标与边界

- 用本地方案生成**语义准确的拟音**（脚步、开门、撞击、雨声等），替代现有 ffmpeg 占位合成。
- 形态与配音一致：**独立 Python 环境 + 常驻 worker + NDJSON 协议**，不经过 ComfyUI 音频节点。
- 本期只做 CLI 最小闭环：给定中文描述与时长，落盘到工程 `assets/audio/sfx/`；**不接页面、不改时间轴/混流**。
- 音效请求仍由既有 `SfxAdapter` 协议承载（`adapters/base.py`），模型 provider 是它的实现来源；页面接入时再补适配器分支。

## 2. 为什么不用 ComfyUI 模板

官方给 Stable Audio 3 的 ComfyUI 支持是 **subgraph 蓝图**，不能直接当本项目模板；自建平面模板虽能出文件，但：

- 套用 medium 的 50 步 / cfg 7 → 输出全零；
- 8 步 / cfg 1.0 → 只得「一声咔哒」（削波、低频占 43%、包络抖动）。

对照实验与详细指标见 [research-sfx-local-commercial-options.md](research-sfx-local-commercial-options.md) §5.1。
结论：small-sfx 的采样参数与 medium 不同，模板接线也不可控，因此改成本项目内 provider，直接调用官方库。

## 3. 结构

```
src/novel_drama/sfx/
├── types.py                       # SfxSpec / SfxRequest / SfxResult / SfxHealth / SfxError
├── base.py                        # SfxProvider 协议 + BaseSfxProvider
├── prompting.py                   # 中文描述 → 英文 + `Length: N seconds`（最长匹配词典）
├── registry.py                    # 从 config.sfx.providers 组装实例、选默认 provider
└── providers/
    ├── stable_audio3.py           # 客户端：起常驻 worker、NDJSON 收发、懒启动、空闲退出、close()
    └── stable_audio3_worker.py    # 在官方 venv 里跑：懒加载模型、生成、写 wav、回报指标
```

CLI：`src/novel_drama/cli_sfx.py`，挂在既有 `sfx` 命令下（与 `sfx plan/run` 并存）：

```bash
ndw --project <工程目录> sfx generate --prompt "沉重的木门被猛地关上，发出低沉的撞击声" --duration 2
ndw --project <工程目录> sfx generate --prompt "雨点密集地打在铁皮屋顶上" --duration 8 \
    --prompt-en "continuous heavy rain drumming on a tin roof, dense raindrops, steady ambience"
ndw sfx providers      # 环境/权重自检
```

产物：`<工程>/assets/audio/sfx/sfx_<时间戳>_<标签>_<请求哈希8>.wav` + 同名 `.json` 侧车
（原始描述、改写后提示词、steps/cfg/seed、实测时长/采样率/耗时/峰值内存）。

## 4. 环境与权重

| 项 | 值 |
| --- | --- |
| 官方仓库 | `https://github.com/Stability-AI/stable-audio-3`，克隆到 `/Volumes/other/ndw_venvs/stable-audio-3` |
| 独立环境 | 同目录 `.venv`（Python 3.11 / torch 2.7.1 / transformers 5.8.0，未装 flash-attn） |
| 权重目录 | `/Volumes/other/models/ndw/sa3_small_sfx/`（`paths.models_root` 下） |
| 权重文件 | `model.safetensors` 2,270,384,940 B（与 Comfy-Org 公开源同哈希）+ `model_config.json`（公开镜像）+ `t5gemma-b-b-ul2/` 1,183,022,944 B（HF 格式，含分词器） |
| 许可 | Stability AI Community License：年收入 <100 万美元可商用；文本编码器另受 Gemma Terms 约束 |
| 清单 | `tts/model_store.py` 的 CATALOG 新增 `sa3_small_sfx`（`license_gate: none`），并写了 `.ndw-model.json` 供 `ndw models verify` 逐文件核对 |

权重目录内的 `model_config.json` 已把文本编码器改成相对目录（`model_path` 指向 `t5gemma-b-b-ul2/`），
worker 启动时也会再覆写一次，保证**完全离线**（`HF_HUB_OFFLINE=1`），不访问受限 HF 仓库。

**下载入口（2026-11-12 起）**：设置页「模型与下载 → 音频模型」已把 `sa3_small_sfx` 列为可下载项，
显示整包体积（约 3.3 GiB）、许可证与「整目录下载」来源；下载在后台线程执行，页面按 1.2 s 轮询，
显示百分比与已下载/总量，失败后按钮变为「重试」。
官方 `stabilityai/stable-audio-3-small-sfx` 是 gated 仓库，CATALOG 的 `hf_repo` 指向内容同哈希的公开镜像
`cocktailpeanut/stable-audio-3-small-sfx`，`hf_repo_origin` 记录官方来源；
必需文件由 CATALOG 的 `required_files` 描述（`model.safetensors` / `model_config.json` / `t5gemma-b-b-ul2/model.safetensors`），
`ndw models verify` 与设置页状态都按它核对。

## 5. 参数与实测（M4 Pro / MPS）

默认：**8 步 / cfg 1.0 / MPS**，输出 44.1 kHz 立体声。

| 指标 | 结果 |
| --- | --- |
| 首次加载模型 | 3.7~4.7 s |
| 热推理 2 s / 8 步 | 0.69 s |
| 热推理 15 s / 8 步 | 1.69 s |
| 热推理 2 s / 16 步 | 1.33 s |
| 进程峰值 RSS | 约 5.2 GB |
| CLI 单次调用总耗时 | 5.1~5.5 s（含模型加载） |

- **cfg 固定 1.0**：cfg 3.0 开始削波，5.0 明显崩坏（质心飙到 7 kHz、4469 个削波样本）。
- **最短可用时长 1.0 s**：0.5 s 会退化成噪声（12827 个削波样本），已在 CLI 与 worker 双向下限拦截。
- 1.5 s 的冲击类偶发削波（模型原始输出超 ±1 后被 clamp），需要绝对干净时建议 ≥2 s。

**空闲退出实测（`--serve --idle-timeout 6`，worker 自行退出）**

| 时刻 | 进程内存 |
| --- | --- |
| 启动、模型未加载 | 27 MiB |
| 生成一条 2 s 音频后 | 4669 MiB（worker 自报峰值 5212.6 MiB） |
| 空闲计时中 | 4609 MiB |
| 空闲超时后（6 s + 4 s 余量） | 进程已退出（exit 0），内存归零，wav 已完整落盘 |

空闲超时是配置项：全局 `sfx.idle_timeout_s`（默认 300），单个 provider 可用
`sfx.providers[].runtime.idle_timeout_s` 覆盖（runtime 优先）。

**中文改写实测（LLM 优先、词典兜底）**

- LLM 可达（本地 OpenAI 兼容 stub 验证协议）：返回 `mode=llm`，提示词为
  `heavy wooden door slams shut with a deep low-frequency thud. Length: 8 seconds`，不再走词典。
- LLM 不可达（本机 omlx 未启动）：`mode=dict`，输出词典结果并附中文提示
  「LLM 改写不可用（AdapterError: omlx 不可达…），已降级为词典改写」。
- 配置 `sfx.prompt.rewrite=dict` 或 CLI `--no-llm`：直接词典，不发网络请求。

两条已交付样例（`/Volumes/other/ndw_sfx_verify/samples/sa3_provider/`）：

| 文件 | 内容 | 指标 |
| --- | --- | --- |
| `impact_door_slam_2s.wav` | 木门重关 | 2.00 s / RMS -26.0 dBFS / 峰值 0.61 / 静音帧 60% / 质心 430 Hz |
| `rain_tin_roof_8s.wav` | 雨打铁皮屋顶 | 8.00 s / RMS -28.6 dBFS / 峰值 0.55 / 静音帧 7% / 质心 2176 Hz |

## 6. 已知限制

1. **中文改写默认依赖本地 LLM（omlx）**：LLM 不可达时自动降级词典，氛围类长描述仍可能被改写得单薄，
   此时建议用 `--prompt-en` 给完整英文；`rewrite_prompt` 已把路线与降级原因写进 CLI 输出和侧车 JSON。
2. 常驻 worker 每个 CLI 进程各起一次，模型会重复加载（约 4 s）。批量化时应复用同一 provider 实例。
3. 尚未做 loop/seam 拼接；长环境音靠单次生成（最长 120 s）。
4. 采样率固定 44.1 kHz，与工程 `output.sample_rate`（48 kHz）不同；接入时间轴/混音时需转码。
5. 尚未接页面生成，`adapters/sfx.py` 仍是占位实现；下载已在设置页可用。

## 7. 复现步骤

```bash
# 1) 官方仓库与独立环境
git clone --depth 1 https://github.com/Stability-AI/stable-audio-3 /Volumes/other/ndw_venvs/stable-audio-3
cd /Volumes/other/ndw_venvs/stable-audio-3 && uv sync --python 3.11

# 2) 权重：设置页「模型与下载 → 音频模型」点 sa3_small_sfx 的「下载」（约 3.3 GiB）
#    或命令行：ndw models install sa3_small_sfx --yes

# 3) 自检与生成
ndw sfx providers
ndw --project <工程> sfx generate --prompt "玻璃杯摔碎在地上" --duration 2
```
