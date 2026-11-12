# R21 配乐本地化：项目内 Stable Audio Open 1.0

目标：把配乐生成从 ComfyUI（ACE-Step 1.5）扩展到项目内 provider，按音效（`sfx/`）同构实现；
ACE-Step 保留为长曲回落路径。默认 `local`，配置可切回 `comfyui`，两条路径共用后处理与记录格式。

## 1. 结论与边界

| 项 | 事实 |
| --- | --- |
| 模型 | Stable Audio Open 1.0（1B，44.1kHz 立体声，`stable-audio-tools` 推理） |
| 权重 | `model.safetensors` 4,853,889,016 B；sha256 `7b20458a…6b57e` 与官方 LFS 一致 |
| 文本编码器 | t5-base（encoder 438,525,864 B），随模型目录离线加载 |
| 许可 | Stability AI Community License：年收入 < 100 万美元可商用；权重目录保留 `LICENSE.md` |
| 时长 | 训练窗口 2,097,152 采样 ≈ 47.55s，实现上限取 47s；可用下限 5s（实测 5s 正常出音） |
| 卸载/空闲 | 常驻 worker 空闲 300s 自动退出；CLI 单次结束 `close()` 收尾 |
| 实测（M 系 Mac / MPS / 100 步 / cfg 7.0） | 10s 推理 24s；47s 推理 108~111s；进程峰值约 10.3~10.6GB RAM |

## 2. 目录与组成

```
src/novel_drama/bgm/
  types.py                      BgmSpec / BgmRequest / BgmResult / BgmHealth / BgmError
  base.py                       BgmProvider 协议
  prompting.py                  中文→英文配乐描述（LLM 优先、词典兜底；不含时长后缀）
  registry.py                   配置 → provider 实例；weights_path 只允许落在 models_root
  providers/stable_audio_open.py         客户端：常驻 NDJSON worker、懒加载、超时/重试/降级
  providers/stable_audio_open_worker.py  独立环境内推理，直接写 44.1kHz PCM24 wav
```

- 独立环境：`/Volumes/other/ndw_venvs/stable-audio-open/.venv`（Python 3.11，torch 2.7.1、
  stable-audio-tools 0.0.20、numpy 1.26.4、soundfile；另装 pytorch_lightning 补齐其 LoRA 导入）。
- 权重：`/Volumes/other/models/ndw/stable_audio_open_10/`（model.safetensors、model_config.json、
  LICENSE.md、t5-base/）。`model_config.json` 的 git blob sha1 为 `3e74821…`，与官方一致。

## 3. 行为与配置

- `bgm.provider: local | comfyui`（默认 local）；CLI `--provider` 可临时覆盖。
- `bgm.providers[]`：`weights_path`、`defaults.{steps,cfg,device,max_seconds,min_seconds}`、
  `runtime.{python,timeout_s,idle_timeout_s,request_timeout_s,max_restarts}`。
- 两档语义不变：
  - 叙事：生成 → 48kHz/24bit → 可选裁首尾近静音（-50 dBFS，保留 0.15s 起音）+ 0.4s 结尾淡出
    → 可选 loudnorm（-16 LUFS / -1.5 dBTP）；
  - 铺底：生成 → 裁开头静音（保留 0.25s）→ 0.8s 淡入 → 两遍 loudnorm。
- 循环填充仍由 `media/mixer.py` 的 `-stream_loop -1` + `atrim` 负责，本次未改。
- 落盘：`assets/audio/bgm/<item>_s<seed>.wav` + 同名 `.json` 侧车（提示词、参数、耗时、内存、响度）；
  生成记录继续写 `<chapter>.records.json`，字段保留 `mode / normalize / trim_edges / edge_trim`，
  新增 `provider / model / steps / cfg / latency_ms / load_s / peak_mib / prompt_en`。
- 超时长：请求 > 47s 时直接返回中文提示，并说明可 `--provider comfyui`（ACE-Step，10~180s）。

## 4. 模型登记与设置页

`tts/model_store.py` 的 CATALOG 新增 `stable_audio_open_10`：

- 体积 5,295,669,195 B（整包），`required_files` 覆盖主权重、配置、许可与 t5-base；
- 许可：Stability AI Community License，`license_gate: none`；
- 下载：新增 `direct_files` 机制（HF 镜像对冷门仓库的小文件不稳定），按清单逐文件直链下载，
  支持断点续传、3 次重试、`model.safetensors` sha256 校验；主源 ModelScope，HF 镜像作降级；
- 设置页音频分组直接复用现有渲染：显示体积 / 许可 / 来源、下载进度百分比与失败重试。

## 5. 实测样例

| 档位 | 请求 | 成品时长 | 响度 | 真峰值 | 生成耗时 | 后处理 | 进程峰值 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 叙事 | 47s | 43.42s（裁掉头 0.33s / 尾 3.27s） | -16.09 LUFS | -1.50 dBTP | 111.1s | 4.65s | 10,555 MiB |
| 铺底 | 40s | 39.98s | -16.00 LUFS | -4.24 dBTP | 92.7s | 2.71s | 10,555 MiB |

试听保留在 `/Volumes/other/ndw_bgm_samples/`（两条 wav + 同名侧车）。

## 6. 页面接入（R21 续）

媒体页配乐面板（`web/static/page-media.js`，由脚本注入，不改 index.html）：

- 后端选择：本地 Stable Audio Open（5~47 秒，默认）/ ComfyUI ACE-Step（10~180 秒）；
  时长超过本地上限时自动切 ACE-Step，并在控件下方说明两者上限差异。
- 两档用途（自动推断 / 叙事 / 铺底）、响度归一与首尾裁剪三个开关；
  开关显示当前生效值与来源（默认值 / 条目设置 / 页面选择），选中有 cue 的计划时带出条目值。
- 生成中只在本面板显示进度条与「已用 / 预计」秒数（本地按 6s 加载 + 2.35s/秒粗估），
  不弹全屏蒙版，生成期间可切到其它页面继续操作。
- 完成后自动刷新列表：新增「后端」列，成品可直接试听；
  最近一次记录显示档位、后端、生成/后处理耗时与 LUFS。
- 失败在面板内显示中文原因；本地不可用（`UNAVAILABLE / MODEL_MISSING / PROVIDER_DISABLED /
  PROVIDER_UNREACHABLE`）时附「去设置页下载 / 检查模型」链接（沿用现有设置页，不新开导航）。
- 后端 `/api/ui/bgm` 新增 `providers`（含可用性、时长上下限）与 `defaults`（provider、
  两个开关默认值）；`/api/ui/bgm/generate` 接收 `provider`，超过本地上限时自动改走 ACE-Step，
  错误 detail 改为 `{message, code, provider}`，页面据此区分是否给设置页入口。

验证：页面默认本地后端，10 秒叙事配乐实测生成 33.1s、-15.93 LUFS，新行可播放；
显式 ACE-Step 与「本地 + 60 秒」都正确路由到 ComfyUI（当前服务未启动，返回中文 503，
code=UNAVAILABLE），实际 ACE-Step 出音回归待 ComfyUI 启动后补。

## 7. 已知限制

- 本地路径最长 47s；60s 以上仍需 ACE-Step。
- MPS 不支持 float64，APG 投影会报错，worker 固定 `apg_scale=0.0`（vanilla CFG）。
- 词典中→英只作兜底，复杂配器描述建议 `--prompt-en`。
- 每次 CLI 调用都会重载模型（约 4.3s）；Web 端尚未接入 provider 选择与独立 worker 复用。
- 5s 极短片段 loudnorm 可能到不了 -16 LUFS（受真峰值与门限影响），属已知行为。
