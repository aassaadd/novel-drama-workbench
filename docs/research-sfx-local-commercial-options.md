# 本地可商用音效（SFX）生成方案调研

- 状态：调研定稿，**未改动任何生产代码，未下载任何模型权重**
- 调研时间：2026-11-12
- 目标读者：决定「音效链路下一步接谁」的人
- 判定口径：商用 = 本项目产出的音效可用于对外发布（含商业用途）；证据分级见 §1.3

---

## 1. 结论速览

1. **推荐路线 A**：接 **Stable Audio 3 Small-SFX**（0.6B，ComfyUI 原生支持，官方音效专用档）。
   许可为 **Stability AI Community License**：年收入 < 100 万美元的组织可商用，输出归使用者所有；超过门槛需 Enterprise 许可。
   与本项目现有模板机制高度同构（CheckpointLoaderSimple + CLIPLoader(stable_audio) + EmptyLatentAudio + KSampler + VAEDecodeAudio + SaveAudio）。
2. **保守备选 B**：接 **MOSS-SoundEffect v2.0**（Apache-2.0，无收入门槛，30 秒 / 48 kHz，中英双语提示词），以独立 sidecar 子进程方式接入（类似现有 IndexTTS 的形态），不走 ComfyUI。
   代价是权重约 11.2 GB、官方只给 CUDA 路径，Mac/MPS 可行性未获官方确认。
3. **零下载兜底**：继续用现有 ffmpeg 占位合成 + ACE-Step（MIT / Apache-2.0，已在本机跑通）做氛围铺底，把「音效」限定为 ambience 类，冲击音与拟音留空。
4. 明确不建议：**MMAudio（权重 CC-BY-NC 4.0）**、**ThinkSound（README 原文禁止商用）**、**AudioGen / AudioLDM 2（CC-BY-NC 系）**、**Woosh 公开权重（非商用）**——这些恰好是「视频对齐」能力最强的几个，商用许可上都过不去。

---

## 2. 候选清单（事实表）

体积列只写**可核实**的字节数/官方标注，查不到写「未查证」。

| 模型 | 许可（原文口径） | 可商用 | 体积 | 文本→音效 | 视频对齐/条件 | ComfyUI 支持 | 出处 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Stable Audio 3 Small-SFX**（0.6B） | Stability AI Community License；训练数据为 licensed + CC | 是（年收入 < $1M） | ckpt 2,270,384,940 B（≈2.27 GB）+ 文本编码器 1,187,264,003 B（≈1.19 GB） | 是，专用音效档，最长 2:00 | 否（文本条件） | **原生**：ComfyUI v0.22.0+，`supported_models.StableAudio3`，官方模板 `audio_stable_audio_3_medium(_base)` | [HF 模型卡](https://huggingface.co/stabilityai/stable-audio-3-small-sfx)、[ComfyUI 博客](https://blog.comfy.org/p/stable-audio-3-day-0-support)、[ComfyUI 文档](https://docs.comfy.org/tutorials/audio/stable-audio/stable-audio-3) |
| **Stable Audio Open 1.0**（1.21B，F32） | Stability AI Community License（gated） | 是（年收入 < $1M） | ckpt 4.52 GB + t5-base 850.3 MB（ComfyUI 模板标注） | 是（音效/短音乐，最长 47.6 s） | 否 | **原生**：`supported_models.StableAudio`，官方模板 `audio_stable_audio_example` | [HF](https://huggingface.co/stabilityai/stable-audio-open-1.0)、[官方模板](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/audio_stable_audio_example.json) |
| **Stable Audio Open Small**（341M+156M） | `stability-ai-community` | 是（年收入 < $1M） | 未标注总量；仓库含 tflite（Arm 路径） | 是，最长 11 s | 否 | 未见 ComfyUI 官方/社区节点（tflite 独立路径） | [HF](https://huggingface.co/stabilityai/stable-audio-open-small)、[Stability × Arm 公告](https://stability.ai/news-updates/stability-ai-and-arm-release-stable-audio-open-small-enabling-real-world-deployment-for-on-device-audio-control) |
| **MOSS-SoundEffect v2.0**（1.3B DiT + Flow Matching） | **Apache-2.0** | 是，无收入门槛 | 约 11.2 GB（第三方统计，官方未标字节数） | 是，最长 30 s，48 kHz，**中英双语** | 否 | 未检索到节点 | [HF 模型卡](https://huggingface.co/OpenMOSS-Team/MOSS-SoundEffect-v2.0) |
| **ACE-Step 1.5**（已在用） | 官方模型卡 MIT；Comfy-Org 单文件包 Apache-2.0 | 是 | 本机已有 `ace_step_1.5_turbo_aio.safetensors` | 音乐/氛围为主，非音效专用 | 否 | **原生**：`ace1.5` 模型类 + 官方模板 | [HF: ACE-Step/Ace-Step1.5](https://huggingface.co/ACE-Step/Ace-Step1.5)、[HF: Comfy-Org/ACE-Step-1.5](https://huggingface.co/Comfy-Org/ACE-Step-1.5) |
| **MMAudio**（CVPR 2025） | 代码 MIT，**权重 CC-BY-NC 4.0** | **否** | 官方：约 6 GB 显存（16-bit） | 是（文本可选） | **是**（视频+文本，同步模块） | 社区节点 `kijai/ComfyUI-MMAudio` | [HF](https://huggingface.co/hkchengrex/MMAudio)、[GitHub](https://github.com/hkchengrex/MMAudio) |
| **ThinkSound**（NeurIPS 2025） | README 同时写 Apache-2.0 与「for research and educational purposes only. Commercial use is NOT permitted」；内置 VAE 来自 Stable Audio Open（需 Stability 许可） | **否**（按原文） | 未查证 | 是 | **是**（视频/文本/编辑） | 未检索到节点 | [GitHub](https://github.com/FunAudioLLM/ThinkSound) |
| **AudioGen**（audiocraft，1.5B） | 代码 MIT，**权重 CC-BY-NC 4.0** | **否** | 1.5B；官方要求 ≥16 GB 显存 | 是（16 kHz） | 否 | 无官方节点 | [AudioCraft 文档](https://facebookresearch.github.io/audiocraft/docs/AUDIOGEN.html) |
| **AudioLDM 2**（1.1B / 1.5B） | **CC-BY-NC-SA 4.0** | **否** | 官方表：1.1B / 1.5B 总参数 | 是（16 kHz，示例 10 s） | 否 | 无官方节点 | [HF](https://huggingface.co/cvssp/audioldm2) |
| **Woosh**（Sony AI） | 公开权重**非商用**（官方声明） | **否** | 未查证 | 是 | **是**（文本/视频→SFX） | 未检索到节点 | [Sony AI 博客](https://ai.sony/blog/introducing-woosh-sony-ais-sound-effect-foundation-model) |
| **FoleyCrafter**（IJCV 2026） | 代码 Apache-2.0（第三方汇总）；官方要求商用前**自行核对 Auffusion 许可** | **待定**（许可链未闭合） | 未查证 | 是 | **是**（视频→音效，含时间对齐） | 无官方节点 | [GitHub](https://github.com/open-mmlab/FoleyCrafter) |
| **TangoFlux**（515M） | 仓库内附 `STABILITY_AI_COMMUNITY_LICENSE.md`，HF 卡片未标 license 字段 | **待宏观复核** | 未查证 | 是（44.1 kHz，最长 30 s） | 否 | 未检索到节点 | [HF](https://huggingface.co/declare-lab/TangoFlux) |

### 2.1 许可细节（决定能不能用）

- **Stability AI Community License**：官方 FAQ 原文——允许研究、非商业与商业使用，**条件是个人/组织年收入低于 100 万美元**（按关联方合计、不论收入来源）；超过门槛需 Enterprise 许可。[stability.ai/license](https://stability.ai/license)
- **Stable Audio 3 的输出归属**：官方公告原文「Under the Stability AI Community License, you own your outputs and can distribute and commercialize them freely」，仍受 Acceptable Use Policy 约束。[Stability 公告](https://stability.ai/news-updates/meet-stable-audio-3-the-model-family-built-for-artistic-experimentation-with-open-weight-models)
- **Stable Audio 3 的文本编码器是 T5Gemma**：模型卡原文写明 T5Gemma 按 **Gemma Terms of Use** 再分发 → 这是叠加在社区许可之上的第二套条款，接入前必须单独过一遍。
- **ThinkSound 的自相矛盾**：仓库 badge 与 LICENSE 写 Apache-2.0，正文第 186–188 行又写「仅研究与教育用途、不允许商用」；且 VAE 来自 Stable Audio Open，需 Stability 另行授权。按最保守读法 = 不可商用。
- **FoleyCrafter**：官方 README 的 LICENSE 段只写「商用请自行核对 Auffusion 许可」，Auffusion 又叠在 AudioLDM 系组件上，许可链未闭合，不能当可商用方案。

---

## 3. 与本项目的匹配度

### 3.1 现有链路事实（本机核对）

| 事实 | 位置 |
| --- | --- |
| 模板清单已支持任意 `kind`，ACE-Step 即以 `kind: "music"` 登记，含 `placeholders` / `bindings` / `fixed` / `models` | [manifest.json](file:///Users/zhaohaochen/git/novel-drama-workbench/config/comfy/manifest.json) |
| 模板解析的内置映射只有 `image / image_ref / video / video_i2v / video_t2v`，**没有 sfx** | [comfy_media.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/media/comfy_media.py#L34-L40) |
| `plan_media` 显式拒绝 sfx：「plan_media 只支持 image/video」 | [runner.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/media/runner.py#L849-L851) |
| `run_media` 的 sfx 分支目前固定走 `render_sfx`（ffmpeg 合成），`adapter` 记为 `offline-sfx` | [runner.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/media/runner.py#L988-L1000) |
| `MEDIA_PATHS` 已含 `sfx → assets/sfx/*.wav`，产物落点无需新增 | [runner.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/media/runner.py#L93) |
| `models.json` 已有 `license / commercial_use / license_gate / bytes / templates` 字段，可直接登记新权重并做许可门禁 | [models.json](file:///Users/zhaohaochen/git/novel-drama-workbench/config/comfy/models.json) |
| `SfxAdapter` 协议已定义（`generate / search / health`），当前只有 `PlaceholderSfxAdapter` | [sfx.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/sfx.py)、[base.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/base.py#L262-L273) |
| 音效事件 schema：`kind` 五类（ambience/foley/impact/transition/music_sting）、`duration_ms` 100–600000、支持 `loop` | [sfx-event.schema.json](file:///Users/zhaohaochen/git/novel-drama-workbench/schema/sfx-event.schema.json) |

### 3.2 本机 ComfyUI 实测（只读接口查询，未跑生成、未下权重）

- 版本 **0.44.1**，设备 `mps`，统一内存 51,539,607,552 B（48 GiB）。
- `CLIPLoader` 的 `type` 已包含 `stable_audio` → Stable Audio 系文本编码器可加载。
- 已存在节点：`ConditioningStableAudio`、`EmptyLatentAudio`、`VAEDecodeAudio`、`SaveAudio / SaveAudioAdvanced`、`TextEncodeAceStepAudio1.5`。
- `EmptyLatentAudio.seconds` 约束为 **min 1.0 / max 1000.0** → 小于 1 秒的冲击音必须生成 ≥1 s 再裁剪（项目 schema 允许 100 ms 起，两者不一致，接入时要在适配层处理）。
- 当前 `checkpoints/` 内**没有任何 Stable Audio 权重**，只有 ACE-Step 1.5 等 5 个。

### 3.3 Mac 可跑性

| 模型 | 官方对 Mac 的说法 | 结论 |
| --- | --- | --- |
| Stable Audio 3 | 模型卡原文：生成「less than a few seconds on a MacBook Pro M4」（走 `stable-audio-3` / `stable-audio-tools`，非 ComfyUI） | 官方承认 Mac 可跑；**ComfyUI+MPS 路径未实测** |
| Stable Audio Open 1.0 | 无官方 Mac 说明 | 未知 |
| SA Open Small | 官方主打 Arm CPU / 手机端（11 s 内出音） | 有官方 Arm 证据，但走 tflite，与本项目链路不同 |
| MOSS-SoundEffect v2.0 | 官方 Quick Start 只给 `torch-cu128` + Triton CUDA Graph，并提示可用 `TORCHDYNAMO_DISABLE=1` 关闭 | 官方未声明 MPS；第三方封装称支持 Apple Silicon（二手证据） |
| ACE-Step 1.5 | 本项目已实测：60 s 音乐耗时约 86.8 s，93% 在 LM 阶段（历史实测基线） | 可用，但不等价于音效质量 |

### 3.4 速度与内存预估

**不做数字外推。** 能确认的只有：权重体积（§2 表）与官方口径的「M4 上数秒级」「H200 上 <2 s」。
ComfyUI + MPS 下的实际耗时、峰值内存**均未实测**，不填推测值。

---

## 4. 推荐路线

### 4.1 路线 A（推荐）：Stable Audio 3 Small-SFX

> **2026-11-12 落地结论（已改道）**：SA3 small-sfx 的 ComfyUI 模板路线**已停用**——
> 自建平面模板能出文件，但套用 medium 的采样参数会得到「咔哒声」，参数对照实验见 §5.1。
> 现已改为**本项目内 provider**（与配音同构：独立 venv + 常驻 NDJSON worker），
> 直接调用官方 `stable_audio_3` 库并产出 wav，不再经过 ComfyUI 音频节点，
> CLI 为 `ndw sfx generate`。下文保留当初的模板路线设计，作为背景与失败记录。

**理由**
1. 音效专用档，最长 2 分钟，覆盖 schema 里 ambience（长）+ impact/foley/transition（短，用 ≥1 s 生成后裁切）两类极值。
2. 与本项目模板机制同构：官方 `medium_base` 工作流（subgraph 展开后）就是 `CheckpointLoaderSimple + CLIPLoader(stable_audio) + CLIPTextEncode + EmptyLatentAudio + KSampler + VAEDecodeAudio + SaveAudioAdvanced` 的标准组合，**可以复刻成平面 API 模板**，`placeholders/bindings` 直接可写。
3. 许可证可商用（年收入 < $1M），训练数据为 AudioSparx 授权 + Freesound CC（模型卡已列明来源与过滤口径），来源比多数开源音效模型干净。
4. 只新增 2 个权重文件（ckpt + 文本编码器），不动现有图片/视频/音乐模板。

**主要风险**
1. **ComfyUI 官方模板是 subgraph（节点类型为 UUID），不能直接当本项目模板用**，必须自建平面模板；官方只发布了 medium / medium_base 两个模板，**没有 small-sfx 模板**，参数需自行确定。
2. **文本编码器叠加 Gemma Terms of Use**，比纯社区许可多一层约束。
3. 模型卡语言只写 English → 项目里的中文音效描述需要中→英改写（可复用现有 LLM 通道，但会多一次调用与失败面）。
4. `KSampler` 参数在官方 medium 模板里是 50 步 / cfg 7 / lcm；small-sfx 的 HF 示例是 8 步 / cfg 1.0 / pingpong。**ComfyUI 下 small-sfx 的推荐参数没有官方结论**，需实测选定。
5. MPS 兼容性未知；一旦不行，回落 SA Open 1.0（同许可证、官方模板、参数已知）代价较小。

**落地改造点（仅设计，未实施）**
1. `config/comfy/` 新增平面模板 `sa3_small_sfx.json`，`manifest.json` 增加一条 `kind: "sfx"` 登记，`bindings` 建议：`prompt → CLIPTextEncode.text`、`duration → EmptyLatentAudio.seconds`、`seed → KSampler.seed`、`filename_prefix → SaveAudio*.filename_prefix`。
2. `comfy_media.py` 的 `_BUILTIN_TEMPLATES` 增加 `"sfx": ["sa3_small_sfx"]`，并同步 `builtin_template_ids` 的中文报错可选值文案。
3. `comfy_media`/`runner` 放开 `plan_media` 对 sfx 的限制，`run_media` 在 `providers.sfx == "comfyui"` 时走 Comfy 分支，否则维持 `render_sfx` 离线合成（保持向后兼容）（**未上线阶段暂不要求**：无需保留旧链路兜底）。
4. `config/comfy/models.json` 登记两个新文件并标注 `license: "Stability AI Community License"`、`license_gate: "community_1m_usd"`、`commercial_use: true`。
5. 配置层：`providers.sfx` 增加 `comfyui` 取值；`comfyui.templates.sfx` 指向新模板。
6. 适配层：新增 `ComfySfxAdapter` 实现 `SfxAdapter.generate`，内部做「中文描述→英文提示词」与 `Length: N seconds` 后缀拼接（官方音效 reprompt 示例即此格式），并把 `<1 s` 的条目按 1 s 生成后用 ffmpeg 裁切。
7. 时长与采样率：生成后统一按工程 `output.sample_rate`（48 kHz）转码，避免与时间轴/混音不一致。

### 4.2 路线 B（保守备选）：MOSS-SoundEffect v2.0 独立 sidecar

**理由**
1. **Apache-2.0，无收入门槛**——如果对外发布的商业模式会跨过 100 万美元，或不想引入 Gemma 条款，这是唯一干净的选项。
2. 原生支持中文提示词，省掉翻译层；48 kHz 输出与本项目采样率一致；30 s 上限够 ambience。
3. 形态与现有 IndexTTS sidecar 一致（独立 Python 环境 + 子进程 + 常驻 worker），不需要动 ComfyUI 模板体系。

**主要风险**
1. 官方只提供 CUDA 路径（`torch-cu128` + Triton CUDA Graph），**MPS 支持无官方说明**；最坏情况是 CPU 推理，速度未知。
2. 权重约 11.2 GB，外加 Qwen3 文本编码器，下载与常驻内存成本明显高于路线 A。
3. 需要新写一整套 sidecar 生命周期（启动、超时、崩溃降级），与「不加封装」的项目约束存在张力。
4. 无 ComfyUI 节点 → 无法复用现有 GPU 串行队列与显存释放逻辑，需自行与图片/视频任务互斥。

### 4.3 零下载兜底（现状）

`providers.sfx = offline` 时继续走 `render_sfx`（粉噪/脉冲/低频冲击/扫频/和弦，确定性可复现），配合 ACE-Step 生成氛围床。
适用：只要求「有声、不违和」的验收阶段；不适用：需要语义准确的拟音（脚步、开门、玻璃碎）。

---

## 5. 明确「不可查证」项

以下 11 项为调研阶段的待查清单；2026-11-12 已按实测逐条标注状态。
「仍未查证」项不做推测填空。

| # | 项目 | 状态 | 依据 |
|---|------|------|------|
| 1 | SA3 在 ComfyUI + MPS 下的耗时与峰值内存 | **已查证（ComfyUI 路线已停用）** | ComfyUI 模板路线被证伪（§5.1）；官方库 + MPS 实测：2 s/8 步热推理 0.69 s，首次加载 4.7 s，进程峰值约 5.2 GB |
| 2 | SA3 输出采样率 | **已查证** | 实测 44.1 kHz、双声道（模型 config `sample_rate=44100`、`audio_channels=2`） |
| 3 | Small-SFX 推荐 steps / cfg | **部分查证** | 官方 `generate()` 默认即 8 步 / cfg 1.0；实测 cfg 3.0 开始削波（37 个样本），cfg 5.0 崩坏（4469 个削波、质心飙到 7 kHz）。ComfyUI 模板的 50 步 / cfg 7 不适用于 small-sfx |
| 4 | 是否支持 loop / seam 处理 | **仍未查证** | 官方 `generate()` 无 loop 参数；seam 未做拼接实验 |
| 5 | MOSS-SoundEffect v2.0 在 Apple Silicon/MPS 的支持与速度 | **仍未查证** | 未下载、未验证（按用户约束不下载 11.2 GB 权重） |
| 6 | MOSS-SoundEffect v2.0 权重精确字节数 | **仍未查证** | 同上 |
| 7 | ThinkSound / Woosh / MOSS 的 ComfyUI 节点是否存在 | **仍未查证** | 未再检索；「未检索到」不等于不存在 |
| 8 | TangoFlux 权重的确切许可标识 | **仍未查证** | 未做许可链复核 |
| 9 | FoleyCrafter 权重许可 | **仍未查证** | 未做许可链复核 |
| 10 | AudioGen / AudioLDM 2 最大生成长度 | **仍未查证** | 未逐项核对 model card |
| 11 | `SaveAudioAdvanced` 的 `format` 是否直接支持 wav | **已查证** | 上一轮 ComfyUI 模板实测可产出 flac，转 wav 可用（该路线现已停用，结论仅作记录） |

### 5.1 实测记录（2026-11-12，M4 Pro / MPS / 官方 stable-audio-3 + torch 2.7.1）

环境：官方仓库 `Stability-AI/stable-audio-3`（commit 于当日克隆）装在 `/Volumes/other/ndw_venvs/stable-audio-3/.venv`（Python 3.11，torch 2.7.1，transformers 5.8.0，未装 flash-attn）；
权重 `/Volumes/other/models/ndw/sa3_small_sfx/`：`model.safetensors`（2,270,384,940 B，与 Comfy-Org 公开源同哈希）+ `model_config.json` + `t5gemma-b-b-ul2/`（1,183,022,944 B，公开镜像的 HF 格式）。

**ComfyUI 路线失败证据（已停用）**：同一权重、同一提示词，用 medium 的 50 步 / cfg 7 得到全零输出（-120 dBFS）；用 8 步 / cfg 1.0 得到「一声咔哒」（峰值 +3 dBFS、<50 Hz 占比 43%、高频仅 2%、包络剧烈抖动）。文件本身完好（44.1 kHz 立体声、时长匹配、无重复块），问题在采样参数与模板接线。

**参数快扫（fixed seed=1234，提示词 `heavy wooden door slams shut, deep thud` / 2 s）**

| 步数 | cfg | 热推理 | 峰值 | RMS | 静音帧 | 质心 | 削波样本 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 4 | 1.0 | — | 0.658 | -29.2 dBFS | 75% | 2513 Hz | 0 |
| 8 | 1.0 | 0.69 s | 0.875 | -25.3 dBFS | 68% | 1976 Hz | 0 |
| 16 | 1.0 | 1.33 s | 0.894 | -24.3 dBFS | 74% | 2759 Hz | 0 |
| 8 | 3.0 | 2.29 s | 1.000 | -20.9 dBFS | 62% | 1557 Hz | 37 |
| 8 | 5.0 | 1.01 s | 1.000 | -9.4 dBFS | 41% | 6988 Hz | 4469 |

结论：**cfg 固定 1.0**，步数默认 8（4 步偏轻、16 步提升有限但耗时翻倍）。cfg > 1 会同时放大音量与失真，5.0 已明显崩坏。

**最短时长（8 步 / cfg 1.0，多 seed）**

| 时长 | 结果 |
| --- | --- |
| 0.5 s | 不可用：12827 个削波样本、RMS -1.5 dBFS、质心 9 kHz，退化成噪声 |
| 1.0 s | 可用但偏轻：峰值 0.14~0.89、RMS -21.7~-44.4 dBFS，静音帧 56~63% |
| 1.5 s | 正常，但 thud 类会削波（50~1589 个样本） |
| 2.0 s | 正常（冲击类样例即 2 s） |

**长音频**：15 s / 8 步热推理 1.69 s，RMS -34.3 dBFS、静音帧 3.1%，包络连续。

**中文提示词适配层实测**：词典式中→英改写 + `Length: N seconds` 对「沉重的木门被猛地关上」可用；
对「雨点密集地打在铁皮屋顶上」改写过于单薄（`raindrops, densely, hitting, tin, roof`），
生成稀疏滴水（RMS -35.9 dBFS、包络每秒一次脉冲）；补全英文（`continuous heavy rain drumming on a tin roof...`）后得到连续雨声（RMS -28.6 dBFS、静音帧 7%）。
结论：**词典适配层只能兜底，氛围类长音效建议直接用 `--prompt-en`**。

**进程口径**：常驻 worker 首次加载 3.7~4.7 s，进程峰值 RSS 约 5.2 GB；CLI 单次调用（含加载）总耗时 5.1~5.5 s；同进程连续生成时每条只计热推理时间。

---

## 6. 若要做小样本验证：代价与建议顺序

> **2026-11-12 状态**：A1、A2 已执行完毕（见 §5.1），并已落地为本项目 `sfx` provider；B1 未启动。

| 步骤 | 下载量 | 风险/耗时 | 说明 |
| --- | --- | --- | --- |
| A1：SA3 Small-SFX 最小验证 | ≈3.46 GB（2.27 GB ckpt + 1.19 GB 文本编码器） | MPS 单次生成耗时未知，预计秒级到分钟级；一次性占用 GPU | 用官方 `medium_base` 模板改 ckpt 名 + 自建平面模板，跑 2 条（一条 3 s 冲击、一条 15 s 环境）即可判定「能不能用」 |
| A2：参数快扫 | 0 | 3–5 次生成 | 固定 seed，比 8 步 / 20 步 / 50 步与 cfg 1.0 / 7.0 的可听差异 |
| B1：MOSS 可行性 | ≈11.2 GB + Qwen3 文本编码器 | 需独立 venv（torch 版本与 CUDA 假设），MPS 可能退回 CPU，耗时会显著拉长 | 仅在 A 路线因许可或质量不达标时才做 |

建议只做 A1 + A2；B1 在许可确有需求前不启动。验证产物请落在 `/Volumes/other/...` 临时目录，验证后清理。

---

## 7. 风险登记（按影响排序）

| 风险 | 影响 | 处置建议 |
| --- | --- | --- |
| 年收入跨过 100 万美元门槛 | 已发布的音效需重新取得 Enterprise 许可 | 在 `models.json` 的 `license_gate` 字段显式标注，页面上做许可提示；长期方案走路线 B |
| Gemma Terms of Use 叠加 | 需要额外合规评审 | 接入前单独评审；如不可接受，改路线 B 或用 SA Open 1.0 + 自有 t5-base（t5-base 为 Apache-2.0，但仍然受模型本体许可约束） |
| 中文提示词质量 | 生成结果与剧情不符 | 复用现有 LLM 通道做中→英改写，并在模板里追加 `Length: N seconds` |
| 短音效被 1 s 下限放大 | impact 类听感拖沓 | 适配层生成后按 `duration_ms` 精确裁切，并做 10–30 ms 淡出 |
| subgraph 模板不可直接用 | 接入返工 | 先按官方内部节点组合自建平面模板，再用 `--dry-run` 校验 bindings |
| MPS 不可用/过慢 | 路线 A 失效 | 预留 `providers.sfx = offline` 回退，保持现有 ffmpeg 路径不删 |

---

## 8. 参考链接

- Stable Audio 3 Small-SFX 模型卡：https://huggingface.co/stabilityai/stable-audio-3-small-sfx
- Stable Audio 3 官方公告（含输出归属表述）：https://stability.ai/news-updates/meet-stable-audio-3-the-model-family-built-for-artistic-experimentation-with-open-weight-models
- Stability AI 许可与 FAQ：https://stability.ai/license
- ComfyUI Stable Audio 3 支持（Day-0）：https://blog.comfy.org/p/stable-audio-3-day-0-support
- ComfyUI Stable Audio 3 工作流文档：https://docs.comfy.org/tutorials/audio/stable-audio/stable-audio-3
- ComfyUI 官方音频模板清单：https://github.com/Comfy-Org/workflow_templates/tree/main/templates
- Stable Audio Open 1.0：https://huggingface.co/stabilityai/stable-audio-open-1.0
- Stable Audio Open Small：https://huggingface.co/stabilityai/stable-audio-open-small
- MOSS-SoundEffect v2.0：https://huggingface.co/OpenMOSS-Team/MOSS-SoundEffect-v2.0
- MMAudio（权重 CC-BY-NC 4.0）：https://huggingface.co/hkchengrex/MMAudio
- ThinkSound（README 许可段落）：https://github.com/FunAudioLLM/ThinkSound
- AudioCraft / AudioGen（权重 CC-BY-NC 4.0）：https://facebookresearch.github.io/audiocraft/
- AudioLDM 2（CC-BY-NC-SA 4.0）：https://huggingface.co/cvssp/audioldm2
- Woosh（非商用公开权重）：https://ai.sony/blog/introducing-woosh-sony-ais-sound-effect-foundation-model
- FoleyCrafter：https://github.com/open-mmlab/FoleyCrafter
- ACE-Step 1.5：https://huggingface.co/ACE-Step/Ace-Step1.5
