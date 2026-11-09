# R13 Spec：IndexTTS 2.5 真接入 + 剩余功能收口

> 时间：2026-11-10
> 前置：工作台 venv 已回退 Python 3.12.9 / torch 2.15.0 / 176 包与快照零差异。
> 本 spec 覆盖「需求 → 设计 → 任务清单」三段；批准后直接按清单实施。

## 1. 需求

### 1.1 范围（本轮做）

| 编号 | 需求 | 说明 |
| --- | --- | --- |
| S1 | IndexTTS 2.5 真出音 | 工作台进程经子进程调用 `~/git/index-tts-official/.venv`（3.11 + torch 2.8.0），权重 `/Volumes/other/models/ndw/indextts_25`，不下载任何东西 |
| S2 | 情绪 8 维通路 | 顺序固定 `[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]`；项目 10 情绪枚举必须映射到该 8 维 |
| S3 | 三条情绪通路显式互斥 | `emo_vector` / `emo_text` / `emo_audio_prompt` 三者任取其一；给两条以上直接报错，不依赖官方静默清空 |
| S4 | 文字情绪前置构造 | 走 `emo_text` 时 worker 必须以 `use_qwen_emo=True` 构造模型 |
| S5 | MPS 精度跟随官方 | `use_bf16=False`，不自选精度 |
| S6 | 默认 provider 切换 | IndexTTS 2.5 出音验收通过后，`default_provider_id` 切到 `indextts_25`；切换前必须确认 CosyVoice3 仍能真出音 |
| S7 | 剩余功能收口 | 除 LLM 联调 / 生图 / 视频外，把配音→时间轴→拼接→导出→混音这条链路在一章上真实跑通，不留 `NOT_IMPLEMENTED` 阻断 |
| S8 | 文档同步 | README 完成度表、路线图状态、项目修改记录文档同步 |

### 1.2 明确不做

- 不做 LLM 联调（`adapters/writing.py` 保持现状）。
- 不做生图、不做短视频、不做 MP4 混流（`mux` 保持 `NOT_IMPLEMENTED`）。
- 不接入 SFX 生成：本机 `models_root` 下只有 `cosyvoice3/` 与 `indextts_25/`，没有音效模型权重；`sfx` 保持显式占位报错，不在本轮造模型。
- 不修改 `~/git/index-tts-official` 任何代码。
- 不新建第三个 Python 环境。

### 1.3 事实基线（本轮实测）

| 项 | 实测值 | 与任务描述的差异 |
| --- | --- | --- |
| IndexTTS 解释器 | Python **3.11.16** | 任务描述写 3.11.13，实测为 3.11.16，按实测记录 |
| torch / torchaudio | 2.8.0 / 2.8.0 | 一致 |
| MPS | 可用 | — |
| soundfile | 0.13.1 | 一致（worker 依赖） |
| `indextts` 包 | 可 import（`indextts/infer_v2_5.py`） | 一致 |
| 权重 | `config.yaml` / `gpt.pth` / `codec.pth` / `s2mel.pth` / `hf_cache/` 齐 | 一致 |

### 1.4 验收标准

1. `ndw tts say --provider indextts_25` 产出可解码 wav，`duration_ms` 为实测值，产物落 `/Volumes/other` 下工程目录。
2. 三种情绪通路各跑一条：`emo_vector`（happy）、`emo_text`、`emo_audio_prompt`；任意两条同时给 → 退出码非 0 且错误文案指明冲突字段。
3. `ndw tts providers` 中 `indextts_25` 与 `cosyvoice3_mlx` 均 `ok`。
4. 同一章在 IndexTTS 下走完 `tts batch → timeline build → concat → export --format srt → mix --render`，无 `NOT_IMPLEMENTED`，退出码 0。
5. `default_provider_id` 为 `indextts_25` 后，不显式指定 provider 的批量合成实际命中 IndexTTS。
6. `tests/smoke_test.py` 仍输出 `SMOKE OK`。
7. 日志与产物不落 `/tmp`；不新增文件超过 1500 行。

## 2. 设计

### 2.1 模块与改动点

| 模块 | 文件 | 改动 |
| --- | --- | --- |
| 配置 | `config/config.yaml`、`config/config.example.yaml` | `tts.providers[indextts_25]` 增 `runtime{python,repo,timeout_s}`、`enabled: true`；capabilities 改为 8 维情绪真实能力 |
| 注册表 | `tts/registry.py` | 无需改结构（未知键自动进 `spec.extra`），仅确认 `runtime` 已透传 |
| provider | `tts/providers/indextts25.py` | 三通路 XOR 强校验；情绪映射改走新模块；错误文案带 `runtime` 缺失提示 |
| worker | `tts/providers/indextts25_worker.py` | 已满足 S4/S5；补 `interval_silence`、`max_text_tokens_per_segment` 透传（可选） |
| 情绪映射 | `tts/emotion_map.py`（新增，约 90 行） | 项目 10 情绪 → IndexTTS 8 维 + `emo_alpha` 推导 + 反向表 |
| 服务 | `tts/service.py` | 无结构改动；确认 `requirement.emotion` 传入 provider 前不被改写 |
| CLI | `cli.py` / `cli_voice.py` | `tts say` 增 `--emo-text` / `--emo-audio-prompt`；`tts providers` 展示三通路能力 |
| 文档 | `README.md`、`docs/roadmap-remaining.md`、`项目修改记录文档.md` | 状态与实测回填 |

### 2.2 数据契约

配置（唯一新增契约）：

```yaml
- provider_id: indextts_25
  enabled: true
  runtime:
    python: /Users/zhaohaochen/git/index-tts-official/.venv/bin/python
    repo: /Users/zhaohaochen/git/index-tts-official
    timeout_s: 1800
  capabilities:
    emotion_control: emo_vector_8d
    emotion_instruction: true          # emo_text 可用
    emotion_reference_variants: true   # emo_audio_prompt 可用
```

情绪映射（单向，确定性，不做猜测式加权）：

| 项目情绪 | 8 维向量 | 说明 |
| --- | --- | --- |
| neutral | 全 0 | 不做情绪控制 |
| joy | happy=1 | |
| sad | sad=1 | |
| angry | angry=1 | |
| fear | afraid=1 | |
| surprise | surprised=1 | |
| tender | calm=1 | 官方无「温柔」，用 calm 最接近 |
| excited | happy=1 | `emo_alpha` 提到上限 |
| whisper | calm=1 | 官方无气声控制，靠 `delivery_note` → `emo_text` 兜底 |
| serious | calm=1 | |

`emo_alpha = 0.5 + 0.5 * intensity`（intensity 取 0~1，缺省 0.5）；`neutral` 恒为全 0 且不传 alpha。

### 2.3 接口

- provider 层：`synthesize(SynthesisRequest) -> SynthesisResult`（不变）。
- worker 层：stdin JSON → stdout 末行 `NDW_RESULT {...}`（不变）。
- CLI：`tts say --provider <id> --text ... [--emotion <name>] [--emotion-intensity 0~1] [--emo-text "..."] [--emo-audio-prompt <wav>]`。

### 2.4 失败处理

| 场景 | 行为 |
| --- | --- |
| 两条以上情绪通路 | `UNSUPPORTED_EMOTION`，文案列出冲突的两项，不静默丢弃 |
| `runtime.python` / `repo` 缺失 | `PROVIDER_DISABLED`，提示配置键路径 |
| 权重缺 `config.yaml`/`gpt.pth` | `MODEL_MISSING`，提示 `ndw models list/verify` |
| 参考音频缺失 | `VOICE_MISSING` |
| 子进程超时 | `PROVIDER_TIMEOUT`，超时值来自 `runtime.timeout_s` |
| worker 非零退出 | `PROVIDER_UNREACHABLE`，回传末 6 行 stderr |
| MPS OOM | worker 异常原文回传，provider 归为 `PROVIDER_OOM`（若可识别）否则 `PROVIDER_UNREACHABLE` |

原则：任何失败都不回退到 CosyVoice3 静默顶替；切换只用配置，不在运行期偷换。

## 3. 任务清单

| 顺序 | 任务 | 依赖 | 验收 |
| --- | --- | --- | --- |
| T1 | 配置落 `runtime` + `enabled` + 真实 capabilities（`config.yaml`、`config.example.yaml`） | — | `ndw doctor` 相关项由「未启用」变为「环境就绪」 |
| T2 | 新增 `tts/emotion_map.py`，接进 `indextts25.py` | T1 | 10 情绪全部能转出合法 8 维向量，单测式脚本逐项打印 |
| T3 | 三通路 XOR 强校验 + CLI 增 `--emo-text`/`--emo-audio-prompt` | T2 | 冲突组合报错；三条单通路各成功一次 |
| T4 | IndexTTS 单句真出音（`tts say`） | T3 | wav 可解码、时长实测、日志/产物在 `/Volumes/other` |
| T5 | CosyVoice3 单句回归 | — | wav 可解码，`tts providers` 显示 ok |
| T6 | 切 `default_provider_id: indextts_25` | T4、T5 | 不指定 provider 的 `tts batch` 命中 IndexTTS |
| T7 | 整章端到端：`tts batch → timeline build → concat → export srt → mix --render` | T6 | 全链路退出码 0，产出音频/SRT/混音文件 |
| T8 | 文档同步 + 回归 | T7 | README/路线图/修改记录更新；`SMOKE OK` |

串行原则：T4 与 T5 各自独占 TTS 模型，不并行；T7 的批量合成同样单并发。

## 4. 落地结果（2026-11-10）

| 任务 | 结果 | 关键证据 |
| --- | --- | --- |
| T1 | 完成 | `tts.providers` 中 `indextts_25` 为「可用 / 已启用」，`runtime` 指向 3.11 环境 |
| T2 | 完成 | `src/novel_drama/tts/emotion_map.py`；10 情绪 + 中文别名全部能转出合法 8 维 |
| T3 | 完成 | `--emotion angry --emo-text ...` 直接报 `UNSUPPORTED_EMOTION`，不再回落；三通路各自出音成功 |
| T4 | 完成 | `indextts_angry.wav` 2391 ms / 22050 Hz；`indextts_emotext.wav`、`indextts_emoaudio.wav` 各自独立时长 |
| T5 | 完成 | `cosy_angry.wav` 5.6 s / 24000 Hz（走 instruct 通路） |
| T6 | 完成 | `default_provider_id: indextts_25`；整章批量中未锁音色的行命中 IndexTTS |
| T7 | 完成 | `/Volumes/other/ndw_demo`：batch → timeline → concat → export srt → mix --render 全部退出码 0 |
| T8 | 完成 | `SMOKE OK`；README / 路线图 / 修改记录已同步 |

### 4.1 与原设计的偏差（均已验证后采用）

1. **IndexTTS 解释器是 3.11.16**，不是 3.11.13；不影响接入，按实测记录。
2. **`tts.default_provider_id` 原先不参与路由**（只有音色库 CLI 读它），自动选择完全由分数决定，切默认值不会生效。已让 `route()` 接受 `preferred_provider_id`，主力通过能力/可用性检查后优先。
3. **`instruct` 不再由情绪自动改写**（原 `_tts_instruct`、`_instruct_for` 都会把 emotion 改写成「用X的语气说这句话」）。否则 IndexTTS 会同时收到向量与文字两条通路而必然冲突。改写下沉到 CosyVoice3 provider，行为等价。
4. **参数类错误与行级锁定不再回落**：之前 `--provider indextts_25` 遇到情绪冲突会静默改用 CosyVoice3 出音，看起来成功、实际参数被丢弃。
5. **`tests/smoke_test.py` 显式覆盖 `adapters.tts=placeholder`**：本机 `adapters.tts` 是 `auto`，不覆盖就会用真模型出音，使「占位必须失败」断言失去意义。

### 4.2 已知边界

- 音色变体（`voices.json`）自带 `default_provider_id`，属于人工锁定，优先级高于全局默认；想让某角色改用 IndexTTS 需在音色库里改绑定，而不是只改全局配置。
- `sfx` 与 `mux` 仍为显式占位：本机没有音效权重，MP4 属视频范围。
