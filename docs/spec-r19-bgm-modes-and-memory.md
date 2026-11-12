# R19 配乐两档生成 + ComfyUI 内存释放

## 1. 目标

- ACE-Step 生成配乐按用途分两档落地，不再只留一个默认开关：
  - `narrative`（叙事）：`generate_audio_codes=true`，保留层次与推进；可选裁首尾近静音 + 结尾 0.4s 淡出，再做 loudnorm；
  - `bed`（铺底）：`generate_audio_codes=false`，补后处理链（裁开头静音 → 淡入 0.8s → loudnorm -16 LUFS / -1.5 dBTP）。
- 档位可由条目、页面/CLI 覆盖，缺省按场景特征推断。
- 每次生成记录实际档位与「生成 / 后处理」两段耗时，便于复盘。
- 修复 ComfyUI 生成结束模型与内存不释放的问题。

## 2. 档位优先级与推断

优先级：条目显式 `mode` > 页面/CLI `mode` > 场景推断。

推断规则（`media/music.py::resolve_mode`，规则以中文注释写在代码内）：

1. 该段有台词 → `bed`（对白垫底，不抢语音频段）；
2. 无台词但时长 ≥ `LONG_BED_SECONDS=60` 秒的长镜头 → `bed`；
3. 其余无台词情绪段 → `narrative`。

## 3. 铺底后处理参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| 静音阈值 | -50 dBFS | 低于该电平视为静音 |
| 起点保留 | 0.25s | 裁掉静音后至少保留的起点 |
| 淡入 | 0.8s | 避免突然进入 |
| 响度 | -16 LUFS | 两遍 loudnorm，真峰值 -1.5 dBTP；线性增益受峰值限制时最多再做两遍动态 loudnorm 逼近目标 |

叙事档响度归一目标 -16 LUFS / -1.5 dBTP，**不加淡入**；归一开关优先级：
CLI `--normalize/--no-normalize` > 条目 `normalize` > 全局 `bgm.normalize_narrative`（默认 true）。铺底档始终归一。

### 叙事档首尾处理

- 处理：`-50 dBFS` 以下判近静音，裁开头保留 0.15s 自然起音/混响、裁结尾保留 0.2s 余韵，最后做 0.4s 淡出。
- 结尾选择短淡出而非环形交叉淡化：交叉淡化会叠加开头素材、改变长度与听感，只适合确定循环的片段；短淡出对循环与普通衔接都安全，且不改中段节奏。
- 开关优先级：CLI `--trim-edges/--no-trim-edges` > 条目 `trim_edges` > 全局 `bgm.trim_narrative_edges`（默认 true）；铺底档不走这套处理。
- 记录：`trim_edges` 与 `edge_trim`（阈值、保留时长、淡出时长、处理前后首尾静音时长与有效时长）。

## 4. 落盘与记录

- 音频：`assets/audio/bgm/bgm_<chapter>_<mode>_s<seed>.wav`（48kHz / 24bit / 立体声）。
- cue：新增可空字段 `mode`（`narrative` / `bed`）、`normalize`、`trim_edges`（布尔，可空），有计划时更新原 cue，否则追加手工 cue。
- 生成记录：`bgm/<chapter>.records.json`，含 `mode`、`mode_source`、`mode_reason`、`generation_s`、`postprocess_s`、`normalize`、`normalize_target`、`trim_edges`、`edge_trim`、`pre_lufs` / `pre_true_peak`、归一后 `lufs` / `true_peak`、seed、prompt_id，保留最近 200 条。

## 5. 入口

- CLI：`ndw bgm generate --chapter X --prompt "..." [--duration 40] [--mode auto|narrative|bed] [--plan bgp_...] [--normalize|--no-normalize] [--trim-edges|--no-trim-edges]`。
- Web：媒体页新增「配乐（BGM）」卡片，档位下拉固定为「自动推断 / 叙事 / 铺底」。
- 接口：`GET /api/ui/bgm`、`POST /api/ui/bgm/generate`。

## 6. ComfyUI 内存释放

- 现状：全工程未调用官方 `POST /free`；生成后 ACE-Step 权重与内存持续驻留。
- 实测（ComfyUI 主进程 `top` MEM）：生成前 816M → 生成后 11G → 空闲 10s 仍 11G → 手动 `/free` 后 1099M。
- 修复：`LocalComfyGateway.free_memory()` 调用 `POST /free`（`unload_models=true, free_memory=true`）；
  配乐生成、图片/视频批次结束、设定图单张生成结束后调用，释放动作放在 GPU 锁内。
- 配置：`comfyui.free_after_job`，默认 `true`；连续批量生成可关掉以换取速度。
- 修复后复测：生成结束即回落到 1101~1153M。

## 7. 叙事档归一复测（同描述 / seed 20261112 / 30 秒）

| 版本 | LUFS | 真峰值 | LRA | 后处理耗时 |
| --- | --- | --- | --- | --- |
| 归一前 | -22.58 | -1.92 dBTP | 13.4 | 约 0.1s（仅转 wav） |
| 归一后 | -16.11 | -1.50 dBTP | 9.9 | 5.3s（含两遍线性 + 两遍动态 loudnorm） |

前 5 秒 RMS 与归一前一致，确认未裁静音、未加淡入。`--no-normalize` 实测后处理 0.4s、-23.35 LUFS，记录与 cue 的 `normalize=false` 同步落盘。

## 8. 叙事档首尾处理复测

真实生成 10 秒叙事档（GPU 队列空闲时执行）：

- 处理前：总长 10.0s，开头近静音 0s，结尾近静音 2.85s，有效 7.15s；
- 处理后：总长 7.25s，开头 0s，结尾 0.16s，有效 7.09s；后处理 2.2s。
- 额外用「2 秒纯静音 + 成品」拼接验证开头裁剪：开头近静音 2.03s → 0.10s，总长 9.25s → 7.22s；最后 50ms 峰值 0.00024，确认淡出生效。
- 该 10 秒样本动态较大，归一到 -17.02 LUFS / -1.71 dBTP（仍受动态与峰值约束），归一链路本身不变。

## 9. BGM 时间槽填充方式确认

现有链路是**循环填充**，不是补静音：`media/mixer.py` 对 `loop=true` 的 cue 使用 `-stream_loop -1`，再用 `atrim` 截到 `end_ms`（未指定 `loop_until_ms` 时取章节时长）。

真实例子：7.253s 的叙事档成品放入 30s 时间槽，music stem 输出 30.0s，原素材完整重复 4 次；循环接缝实测：

- 接缝采样跳变 0.00024，同轨内部跳变 99.9 分位 0.00870、最大 0.01711，接缝比内部常态小约 36 倍，无爆音；
- 接缝前 100ms RMS -100dB（素材尾部已淡出）、接缝后 -45.6dB（素材头部近静音），无音量跳变；
- 各 7.25s 片段 RMS 为 -38.6 / -37.5 / -37.5 / -37.6 dB，第一段略低是 cue 默认 1.5s 淡入导致。

结论：行为正常，未修改混音逻辑；`tests/smoke_test.py` 增加断言防止退化为补静音。
