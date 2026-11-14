# 常驻配音 worker 设计（R15）

> 状态：已实现（2026-11-11）
> 日期：2026-11-11
> 关联：`docs/spec-r13-indextts25.md`、`docs/tts-line-state-and-seams.md`、`src/novel_drama/tts/providers/indextts25*.py`
> 目标：把「每行（甚至每块）起一个 Python 3.11 子进程、每次重新加载 3.26GB 模型」改为「整批共用一个常驻 worker、模型只加载一次」。
> 约束：只改进程生命周期与通信方式，不改合成参数、切分规则、产物路径、侧车格式与缓存键。

---

## 0. 一句话结论

实测每行固定开销 **15.96s**（进程启动 0.56s + import 2.21s + 模型加载 13.19s），真正推理只有 5.18s。
5 行台词：逐行起进程 **121.35s**，单进程连续合成 **45.40s**（2.67×），**每行省 15.19s**；
从第 2 行起每行 **24.27s → 5.18s**（4.69×）。

---

## 1. 现状链路（谁 spawn 谁）

### 1.1 调用链

```
Web「开始生成」POST /api/ui/generate
  └─ web/app.py:986-1001  Popen 1 个 CLI 进程（start_new_session=True）
       python -m novel_drama.cli --project <P> tts run --chapter <C> --wait
       └─ cli.py:405-409  cmd_tts：把 N 行台词各自入队成 N 个 job
       └─ cli.py:408-409  --wait → _drain(queue)（cli.py:112-133）
            └─ GpuQueue.run_once（gpu_queue.py:197-221）
                 ├─ GpuLock.hold(job_id)   ← 每个 job 抢一次锁
                 └─ execute_job → _execute_tts_job（jobs.py:137-185）
                      └─ run_batch(rows=[这一行])（batch.py:294-613）
                           └─ service.synthesize（service.py:481）
                                └─ _synthesize_chunks（service.py:333-479）
                                     └─ 按 24 字切块，逐块 provider.synthesize（service.py:379-389）
                                          └─ IndexTts25Provider.synthesize（indextts25.py:84-163）
                                               └─ self._run_worker → subprocess.run（indextts25.py:182-191）
                                                    └─ python3.11 indextts25_worker.py（worker.py:42-104）
                                                         ├─ IndexTTS2(...)  ← 每次重新加载模型
                                                         ├─ tts.infer(...)  ← 一次
                                                         └─ 进程退出
```

### 1.2 判定

| 问题 | 结论 | 依据 |
| --- | --- | --- |
| 批量配音是一行一进程吗？ | **不是**。批量是一个 3.12 CLI 进程内循环多行 | `cli.py:405-409` 入队后 `_drain` 循环；`jobs.py` 一行一 job |
| 每次都重新加载模型吗？ | **是**。模型在 3.11 worker 里，worker 一次只跑一个请求就退出 | `worker.py:42-104` 读完 stdin 构造模型、infer 一次、`return 0` |
| 行内还会多次加载吗？ | **会**。按 24 字切块，**每块一次子进程**，长台词 N 块 = N 次模型加载 | `service.py:367` 切块 + `service.py:389` 逐块调 provider |
| 有没有现成常驻机制没启用？ | **没有**。provider 自述「每次合成需重新加载模型，热启约 30s」（`indextts25.py:79-81`），能力矩阵 `clone_prompt_cache: false` | 实测 + 配置 |
| GPU 锁是否放大开销？ | **当前不是瓶颈**。锁按 job 抢放，配音串行时无等待；测量时锁文件为僵死锁（pid 46398 已不存在），接管一次后每行只做创建/删除 | `gpu_queue.py:125-146`；实测锁内容与 `kill -0` |

> 注：官方 `infer` 内部**本身有参考音频缓存**（`infer_v2_5.py:620-663`，按 `spk_audio_prompt` 字符串判等），但只在同一进程内有效——逐行起进程让这个缓存永远用不上。

---

## 2. 实测耗时拆解

### 2.1 测量条件

- 机器：Mac（Apple Silicon，MPS），与生产同一套权重与解释器
- 解释器：`/Users/zhaohaochen/git/index-tts-official/.venv/bin/python`（3.11.13）
- 权重：`/Volumes/other/models/ndw/indextts_25`（`gpt.pth` 3,259,599,833 B）
- 参考音频：`/Volumes/other/models/voice_refs/cosyvoice_zeroshot_human.wav`（3.48s 真人样本）
- 5 条台词（均 ≤ 24 字，即都只切 1 块）：

  1. 你终于来了，我等了你整整三年。
  2. 这件事没有你想的那么简单。
  3. 走吧，再晚就来不及了。
  4. 我记得那天下了很大的雨。
  5. 如果重来一次，你会怎么选？

- 测量脚本为临时文件（`/tmp/ndw_measure_one.py`、`/tmp/ndw_measure_loop.py`、`/tmp/ndw_measure_run.py`），**未改动生产代码**。
- 条件限制：模型文件已在系统页缓存内（冷盘首次加载会更慢，本次未测）；无 ComfyUI/图片任务争用；未持 GPU 锁。

### 2.2 逐行起进程（复刻生产链路）

| 行 | 解释器+site+读 stdin | import torch/indextts | IndexTTS2 构造（模型加载） | 单行 infer | 写盘/sf.info | 墙钟合计 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.60s | 2.28s | 13.27s | 9.55s | <0.01s | 25.70s |
| 2 | 0.56s | 2.26s | 13.24s | 8.20s | <0.01s | 24.27s |
| 3 | 0.56s | 2.27s | 13.28s | 7.68s | <0.01s | 23.78s |
| 4 | 0.54s | 2.14s | 12.84s | 7.94s | <0.01s | 23.45s |
| 5 | 0.55s | 2.10s | 13.38s | 8.12s | <0.01s | 24.15s |
| **均值** | **0.56s** | **2.21s** | **13.19s** | **8.30s** | **<0.01s** | **24.27s** |

- 单行 infer 8.30s 里含「首次参考音频编码」；见 2.3 的差值。
- 写盘：官方 `infer` 内部直接 `save_pcm_wav`，本次额外测量只有 `sf.info`，确认外部落盘开销可忽略（<10ms）。
- 5 行墙钟合计 **121.35s**。

### 2.3 单进程连续合成（同一批 5 行，模型只加载一次）

| 阶段 | 耗时 |
| --- | --- |
| 解释器启动 + import torch/indextts | 2.11s（另加进程启动约 0.56s） |
| IndexTTS2 构造（模型加载一次） | 12.95s |
| 第 1 行 infer | 9.02s |
| 第 2~5 行 infer 均值 | **5.18s**（5.06 / 5.09 / 5.41 / 5.15） |
| 5 行墙钟合计 | **45.40s** |

**参考音频编码 ≈ 3.84s**（第 1 行 9.02s − 后续均值 5.18s，实测差值）。
归因依据：官方源码只在 `spk_audio_prompt` 变化/首次时做 speaker 编码与 style 提取，之后直接复用缓存（`infer_v2_5.py:618-663`）。因此该 3.84s 只在「每进程第一句」出现一次——逐行起进程等于每行白付一次。

### 2.4 对比与节省

| 指标 | 逐行起进程 | 单进程连续 | 差异 |
| --- | --- | --- | --- |
| 5 行总墙钟 | 121.35s | 45.40s | **−75.95s（2.67×）** |
| 每行平均 | 24.27s | 9.08s（含首行 ref 编码） | **−15.19s/行** |
| 第 2 行起每行 | 24.27s | 5.18s | **−19.09s/行（4.69×）** |
| 每行固定开销 | 15.96s（0.56+2.21+13.19） | ≈0（仅一次） | 全部消除 |

> 以上均为**实测**。第 2 行起的 5.18s 已包含参考音频缓存复用；若批内多角色频繁交替参考音频，每切换一次会有约 3.84s 重编码，分组后按音色聚簇可把编码次数降到「每个不同参考音频一次」（见 §8.5）。

---

## 3. 官方源码依据（不是推断）

| 事实 | 原文/位置 |
| --- | --- |
| 不能就地加载进工作台 3.12 环境 | `pyproject.toml:25` `requires-python = ">=3.10,<3.12"`；`:57-58` `torch==2.8.*` / `torchaudio==2.8.*` |
| 参考音频按「路径字符串」缓存 | `infer_v2_5.py:620` `if self.cache_spk_cond is None or self.cache_spk_audio_prompt != spk_audio_prompt:`；`:654-658` 写回 5 个 cache 字段 |
| 首次/换参考才做 w2v-BERT + Campplus + length_regulator | `infer_v2_5.py:627-653` |
| `use_emo_text` 必须在构造时打开 qwen 情感模型 | `infer_v2_5.py:588-600`（构造时 `use_qwen_emo=False` 会 RuntimeError） |
| MPS 分支自行关闭 bf16 | `infer_v2_5.py:107-110`（worker 不覆盖，`worker.py:53-61`） |
| 三条情绪通路互斥 | `infer_v2_5.py:583-586`（工作台已在参数层提前拒绝，`indextts25.py:102-118`） |

---

## 4. 设计目标与非目标

### 4.1 目标

1. 模型在**一整批配音内只加载一次**（整章/整次「开始生成」= 一批）。
2. 每行只付推理成本；长台词多块不再重复加载模型。
3. 停止、失败、Web 退出都不留孤儿进程，不产生半截 wav。
4. 产物路径、命名、侧车字段、缓存键、指纹算法**完全不变**。
5. 保留一键回退到现有逐行链路的能力（配置开关）。
6. 批内按复用键**分组重排执行顺序**：换音色只重编码参考音频、不重启；只有换模型或切换 qwen 文字情绪模式才重启，且重启次数与行数无关（见 §8）。
7. 重排只影响执行顺序：文件名、revision、章节内行序、时间轴、台词表、导出顺序一律按原顺序写回，不得错位。

### 4.2 非目标（本轮不做）

- 常驻配音进程与图片/视频任务的 GPU 调度、排队、优先级——按「配音独占 GPU、用户不会同时操作」的前提，不做跨任务调度。
- 多 worker 并发 / 批量并行推理（MPS 单卡串行，收益未验证）。
- 常驻系统服务、跨工程共享 worker、HTTP 化对外服务。
- 流式返回、边合成边播放。
- 改动切分规则、接缝处理、对齐策略。

---

## 5. 常驻 worker 形态

### 5.1 选型

| 方案 | 说明 | 结论 |
| --- | --- | --- |
| A. 3.11 独立子进程 + stdin/stdout NDJSON | provider 持有子进程；一行请求一行响应；父进程死→管道 EOF→worker 退出 | **采用** |
| B. 工作台 3.12 进程内加载模型 | 依赖冲突（torch 2.8 vs 2.15、Python <3.12） | 否决（硬约束） |
| C. 本地 HTTP / Unix Socket 常驻服务 | 需端口/套接字管理、鉴权、独立生命周期与孤儿治理 | 本轮否决（复杂度不匹配） |
| D. 复用 Web 后端进程 | Web 是 uvicorn 3.12 进程，同 B 冲突；且 Web 重启会带走 | 否决 |

选 A 的关键理由：管道**自带生命周期耦合**——CLI 进程退出/被杀，worker 的 stdin 立即 EOF，无需额外守护即可避免孤儿；也不需要端口与鉴权。

### 5.2 归属与复用范围

- worker 实例挂在 `IndexTts25Provider` 上，provider 由 `build_adapters` 构建一次，`GpuQueue` 全程复用（`jobs.py:167` 从 `adapters.tts.service` 取同一 service）。
- 因此「一个 CLI `tts run --wait` 进程 = 一个 worker = 整批复用」。
- 批内执行顺序按复用键分组重排（见 §8）：分组只决定「谁先谁后」，产物与章节顺序仍按原顺序写回。
- 不建议跨批次常驻（Web 服务持有），原因：增加 Web 退出清理与显存长期占用风险，而跨批次复用收益只是省一次 15.96s（一整章占比很小）。

### 5.3 进程拓扑

```
Web (3.12, uvicorn)
 └─ 配音批次 CLI 进程 (3.12, start_new_session=True, 独立进程组)
     └─ 常驻 worker (3.11 + torch 2.8, 同进程组)
          └─ MPS / 模型权重常驻
```

- worker 不脱离进程组：现有「停止生成」`os.killpg(os.getpgid(pid), SIGTERM)`（`web/app.py:1026-1029`）会同时打到 CLI 与 worker。
- worker 不自己抢 GPU 锁（锁仍由 CLI 的 `GpuQueue` 按 job 持有），避免两把锁叠加。

---

## 6. 通信协议（NDJSON）

### 6.1 请求

```json
{"id":"line_xxx#0","cmd":"synth","model_dir":"/Volumes/other/models/ndw/indextts_25","text":"你终于来了。","out_path":"/…/line_xxx_r3.wav.tmp-41234.wav","ref_audio":"/…/voice_refs/xxx.wav","ref_fingerprint":"abspath|mtime_ns|size","lang":"ZH","emo_vector":null,"emo_alpha":null,"emo_text":null,"emo_audio_prompt":null,"duration_factor":null}
```

- `id`：`<line_id>#<chunk_index>`，用于乱序/超时对账，也用于日志排查。
- 控制命令：`{"cmd":"ping"}`、`{"cmd":"shutdown"}`。
- 兼容：worker 保留现有「一次性读 stdin → `NDW_RESULT`」入口（`worker.py:42-104`），常驻模式改用 `--serve`，避免影响 `tts say` / `tts compare` / 排障脚本。

### 6.2 响应

```json
{"id":"line_xxx#0","ok":true,"out_path":"…","duration_s":3.27,"sample_rate":24000,"infer_s":5.18}
{"id":"line_xxx#0","ok":false,"error_type":"RuntimeError","error":"…","traceback":"…"}
```

- **stdout 只放协议行**；官方库日志（进度条、`>> gpt_gen_time` 等）统一重定向到 stderr。现有解析是「倒序找前缀行」，常驻模式必须改为「逐行读、按 id 匹配」，否则会误吞日志。

### 6.3 原子落盘（防半截 wav）

- 请求里给的是**临时路径**：`<最终名>.tmp-<worker_pid>.wav`。
- worker 合成成功后返回；上层（3.12 进程）在收到成功响应后 `os.replace(tmp, final)`。
- 失败/超时/取消：上层删除临时文件；worker 被 SIGTERM 时也删自己的临时文件。
- 这样即使进程在半途被杀，最终路径也不会出现疑似完整的半截文件（现有 `service._synthesize_chunks` 只读最终路径拼接，天然不受影响）。

---

## 7. 生命周期

### 7.1 状态机

```
未启动 --首次 synth--> 加载中(约16s) --成功--> 空闲/复用
空闲 --idle_timeout_s 无请求--> 退出（下次请求再懒启动）
复用 --批结束(close)--> 优雅退出
任意 --SIGTERM--> 清理临时文件 → 退出
任意 --父进程消失(stdin EOF)--> 推出
```

### 7.2 各时点行为

| 时点 | 行为 | 依据/落点 |
| --- | --- | --- |
| 首次请求 | 懒启动：spawn + 握手 `ping`，期间状态显示「加载模型中」 | 建议日志打印 `tts worker: loading model…` |
| 后续行 | 直接复用，无握手开销 | 实测每行 5.18s |
| 空闲超时 | worker 内部读超时（`idle_timeout_s`）→ 自行退出；下次请求重新懒启动 | 待拍板默认值，建议 300s |
| 整批完成 | CLI 在 `_drain` 返回后 `finally: service.close()` → `shutdown` → 等 5s → `terminate` → 再 2s → `kill` | `cli.py:408-409` 改造点 |
| 用户「停止生成」 | 现有 `killpg(SIGTERM)` 打整组；worker 收信号：不接新请求、结束当前 infer、删临时文件、退出 | `web/app.py:1007-1051`，无需改行为 |
| Web 服务退出 | uvicorn shutdown 钩子复用 `ui_stop` 逻辑，避免留下无主 CLI 批次 | 新增 lifespan/on_shutdown |
| CLI 被杀（kill -9） | worker stdin EOF → 5s 内退出（若正在 infer，等当前句结束，最长约 30s） | 管道 EOF 兜底 |
| 重复点击生成 | 沿用 queue active 判断（`web/app.py:971-976`）+ pid 文件；补充「pid 存活则直接返回 started:false」 | 见 §9.2 |

### 7.3 模型与音色切换

worker 复用键（任一变化即优雅重启，仅付一次约 16s）：

```
(protocol_version, provider_id, model_dir, use_qwen_emo)
```

- `model_dir` 变化 = 换模型 → 重启。
- `use_qwen_emo` 变化 = 批次里从「向量情绪」切到「文字情绪」→ 重启；建议默认按首批请求推断，遇到切换时重启并在日志提示（不建议一律 `True`：会额外加载 qwen 情感模型，内存与启动成本未实测）。
- **参考音频切换**：官方只按路径字符串判等（`infer_v2_5.py:620`），同名文件被覆盖会复用旧音色。worker 侧维护 `(abspath, mtime_ns, size)` 指纹；指纹变化时显式清空官方 5 个 `cache_*` 字段再合成。
  - 风险：依赖官方内部字段名，升级需复核；备选做法是复制参考音频到带指纹的新文件名（不改官方代码，但会产生临时文件）。
- **换音色不重启**：`ref_audio` / `emo_audio_prompt` 变化只触发一次参考音频重编码（约 3.84s，实测差值），进程继续复用。批内按二级键聚簇后，每个不同参考音频只编码一次，而不是每次切换都编码（见 §8.2、§8.5）。
- **重启只发生在组边界**：`model_dir` 或 `use_qwen_emo` 变化才重启；分组把这些行各自聚成一组，重启次数 = `组数 − 1`，与行数无关（见 §8.4）。
- 情绪强度 `emo_alpha`、向量数值变化不触发重启。

---

## 8. 分组调度（执行顺序重排）

### 8.1 结论与目标

换音色只需重编码参考音频（3.84s/次），换模型或切换 qwen 文字情绪模式才需要重启（约 16s）。
因此把「同一个 worker 能连续处理的行」排在一起执行，可把重启次数与重编码次数压到与行数无关的量级；
**分组只重排执行顺序，不改变任何产物内容与顺序**。

### 8.2 分组键

| 层级 | 键 | 变化后果 |
| --- | --- | --- |
| 一级（重载分界） | `(provider_id, model_dir, use_qwen_emo)` | 变化 → 优雅重启 worker（约 16s） |
| 二级（组内排序） | `(ref_audio 绝对路径, emo_audio_prompt 绝对路径)` | 变化 → 只重新编码参考音频（约 3.84s） |

- `use_qwen_emo`：批内只要有一行使用 `emo_text`（文字情绪），该组就需要 qwen。纯 `emo_vector` 或无情绪的行在 qwen 组里也能正常合成，只是不调用 qwen。
- **不进键、不触发重启**的参数：情绪强度 `emo_alpha`、8 维向量数值、`duration_factor`、文本长度、情绪名。
  即「同一情绪强度变化不重启」，「情绪文本模式切换才重启」。
- 音色不同也不重启：同一 worker 内切换 `spk_audio_prompt` 只让官方重新编码一次（`infer_v2_5.py:620-663`）。

### 8.3 分组位置与回填方式

- **分组在入队阶段完成**（`cli.py` 的 `cmd_tts`）：对 `build_tts_jobs` 的结果做一次纯 Python 预解析（`service.decide` + `_line_voice`），算出每行的一级/二级键，按「一级键 + 二级键」做**稳定排序**后依次入队。
  - 稳定排序：同键内保持原章节顺序，不改变同键行的相对次序。
  - 预解析只用于排序，不参与产物生成；执行时 `_execute_tts_job` 仍按行重新解析音色与路由（现有行为），因此即使批内决策发生变化，也只是分组不够优，**不会写错产物**。
- **不新增队列字段**：分组键不落盘（`schema/render-job.schema.json` 为 `additionalProperties: false`，避免动 schema）；队列顺序即执行顺序，日志里打印 `group=<一级键>` 便于排障。
- **回填**：`run_batch` 仍逐行就地修改 chapter 中对应的 line 对象并 `save_chapter`；`scenes[].lines[]` 数组顺序从不重排。因此：
  - 产物文件名 `assets/voice/lines/<line_id>_r<revision>.wav` 与执行顺序无关；
  - revision 由行自身状态机递增，与执行顺序无关；
  - 台词表、时间轴、拼接、导出、SRT 都读章节原顺序，**不会因重排错位**；
  - 批次报告 `.ndw/tts_batches/<bid>.json` 的 `rows` 按执行顺序记录，仅用于排障（实现时在报告里注明 `exec_order`，不改 schema）。
- **进度与预计剩余**：`stats.succeeded/failed` 按实际完成条数累加；前端「完成 X/Y」读的是队列中行状态，总数不变；ETA 仍用最近 8 条成功耗时滑动平均。分组后同参考音频连续执行，样本更同质，估算通常更稳（每个音色簇的首行会多约 3.84s，滑动平均可吸收）。

### 8.4 最坏情况：重启次数上界

- 设批内不同一级键的数量为 `G`，则重启次数 = `max(0, G − 1)`。
- `G ≤ |providers| × |model_dirs| × 2`（qwen 开 / 关）。
- 常规配置（provider 与模型固定）下 `G ≤ 2`：整章同时存在「向量情绪」与「文字情绪」时各成一组，**最多 1 次重启**。
- 单次重启实测约 16s（0.56 + 2.11 + 12.95）；即使 `G = 4`（2 provider × 2 种 qwen 模式）也只有 3 次重启 ≈ 48s。
- **关键结论：重启次数与行数 N 无关**。整章情绪每行交替，也只是分成 2 组，不会每行重启。
- 多章 pipeline 场景：worker 在同一个 CLI 进程内**跨章复用**（`close()` 只在进程结束时调用），章与章之间不额外重启；但分组只在本章入队时进行，**不跨章全局重排**，以保住「章完成顺序」与进度语义。此时 `G` 取整个 CLI 进程内出现的不同一级键数量，上界不变。
- 示例（推断）：60 行、向量/文字情绪各半 → 2 组、1 次重启 ≈ 16s，整章约 331 + 16 ≈ 347s，仍约 4.2×。

### 8.5 重编码次数

- 组内按 `(ref_audio, emo_audio_prompt)` 聚簇后，每个不同参考音频只编码一次，成本 ≈ `R × 3.84s`（R = 组内不同参考音频数）。
- 不做组内聚簇（按原台词顺序）最坏为「每行都切换参考音频」，成本 ≈ `N × 3.84s`。
- 因此组内按二级键排序是分组调度的组成部分，不是可选优化。

### 8.6 对「停止生成」的影响

- 停止仍走 `killpg(SIGTERM)`，立即生效；worker 结束当前句后退出并清理临时文件。
- 停止后已完成的行在章节里呈「非连续」分布（靠后的行可能先完成），这是分组调度的直接结果；行状态、进度条条数、预计剩余都按实际完成条数统计，不因重排而失真。
- 恢复生成仍由 `line_state` 逐行判定补齐，不依赖执行顺序。

---

## 9. 并发与保护（从简）

配音独占 GPU，本设计**不做**跨任务调度。只保留三条必要保护：

1. **单 worker 串行**：一个 provider 一个 worker，天然串行，不引入并发参数；分组只重排顺序，不产生并行。
2. **同一工程同一时刻只允许一个配音批次**：沿用 `web/app.py:971-976` 的 active 判断；`generate` 增加「pid 文件中的进程仍存活 → 返回 `started:false`」。
3. **GPU 锁沿用现状**：仍按 job 抢放（`gpu_queue.py:213`），不改成整批持锁，避免行为变化；实测无等待（锁文件僵死锁已被接管）。

---

## 10. 失败与恢复

| 场景 | 检测 | 处理 | 用户可见 |
| --- | --- | --- | --- |
| 单行推理卡住 | 请求超时（建议 300s，待拍板） | 杀 worker，记 `PROVIDER_TIMEOUT`（可重试），删临时 wav | 该行失败并标记，批继续 |
| worker 崩溃/无响应 | 读到 EOF、非 JSON、响应 id 不匹配 | 当前行记 `PROVIDER_UNREACHABLE`；标记 worker 失效；下一行懒重启 | 该行失败，批继续 |
| 连续崩溃 ≥ `max_restarts`（建议 2） | 计数 | **自动降级为逐行起进程**（旧链路仍在），并打一条告警；不无限重启 | 批继续，但速度回落到旧水平 |
| 用户取消 | SIGTERM | 不再接新请求；当前 infer 结束后退出（MPS 无法在句中断）；删临时文件 | 「已停止」 |
| 半截 wav | 只认成功响应 | 临时文件 + `os.replace`；失败删除 | 不产生坏产物 |
| 批次中断 | 进程退出 | 已落盘行保留（现有行为，`batch.py` 逐行保存） | 恢复后只补未完成行 |

---

## 11. 预期收益

### 11.1 实测（5 行，单块）

| 项目 | 数值 |
| --- | --- |
| 逐行起进程总耗时 | 121.35s |
| 常驻总耗时 | 45.40s |
| 加速比 | 2.67× |
| 每行节省 | 15.19s（首行口径）/ 19.09s（第 2 行起口径） |

### 11.2 整章外推（**推断，未验证**）

公式：`常驻 ≈ 0.56 + 2.11 + 12.95 + 9.02 + (N−1) × 5.18`；`逐行 ≈ N × 24.27`（假设每行 1 块）

| 行数 N | 现状 | 常驻后 | 加速比 |
| --- | --- | --- | --- |
| 20 | 485s | 135s | 3.6× |
| 60 | 1456s | 331s | 4.4× |
| 100 | 2427s | 545s | 4.5× |
| 300 | 7281s | 1570s | 4.6× |

### 11.3 长台词（多块）额外收益（**推断，未验证**）

现状每多 1 块 = 多一次 15.96s 固定开销 + 一次推理；常驻后每多 1 块只多约 5.18s。
24 字以上的台词在当前 24 字切分下很常见，这部分收益会叠加在上表之上。

---

## 12. 改动范围

| 文件 | 改动 | 风险 |
| --- | --- | --- |
| `tts/providers/indextts25_worker.py` | 新增 `--serve` 常驻循环（读 NDJSON、按 id 回包、idle 超时、信号处理、临时文件清理）；保留一次性入口 | 中：协议与日志分流要一次做对 |
| `tts/providers/indextts25.py` | 新增常驻客户端（spawn/握手/请求/超时/重启/关闭）；`synthesize` 优先常驻，异常按配置回落 | 中：并发读写的锁与超时 |
| `tts/base.py` | `TtsProvider`/`BaseProvider` 增 `close()`，默认 no-op | 低 |
| `tts/service.py` | 新增 `close()`，遍历 provider 调用；不改合成逻辑 | 低 |
| `tts/batch.py` | 不改（复用 `run_batch`），只需保证 provider 实例跨 job 复用 | 低 |
| `cli.py` | `cmd_tts` 的 `--wait` 分支 `try/finally: service.close()`；确认 `_tts_service` 与 `_make_queue` 用的是同一个 service 实例 | **中：当前 `_tts_service` 与 `_make_queue` 会各建一个 `TtsService`，必须统一，否则 close 关不到队列里的 worker** |
| `cli.py`（分组） | `cmd_tts` 入队前按一级/二级键做纯 Python 预解析与稳定排序；分组键不落盘、不改 schema；预解析失败时退回原顺序入队（只影响速度，不影响产物） | 低：预解析与执行期决策可能不一致，仅导致分组不最优 |
| `tts/batch.py`（报告） | 不改逐行保存逻辑；批次报告按执行顺序记录 `rows`，并补 `exec_order` 便于排障（报告无严格 schema） | 低 |
| `web/app.py` | `ui_generate` 增加存活 pid 复用判断；新增 shutdown 钩子复用 `ui_stop` 清理 | 中：停止与队列写入的竞态（现有等待循环已缓解） |
| `config/config.yaml`、`config.py` | 新增 `tts.providers[].runtime`: `worker_mode`（resident/per_call，默认 resident）、`idle_timeout_s`（默认 300）、`request_timeout_s`（默认 300）、`max_restarts`（默认 2） | 低 |
| `schema/*` | **不改** | — |
| 数据/旧工程 | **无需迁移**：产物路径、侧车、缓存键、指纹均不变；关开关即回旧链路（**未上线阶段暂不要求**：无需为旧工程保留回旧链路与兼容分支） | 低 |

### 12.1 风险清单

1. **MPS 显存/RSS 常驻**：本次未测常驻内存峰值，需在实现阶段补一次（现状单行 RSS 约 3GB 量级，仅作参考）。
2. **依赖官方内部字段**做参考音频缓存失效；官方升级需复核，备选方案见 §7.3。
3. **停止时的竞态**：`killpg` 与队列写并发，现有 2s 等待 + 取消标记可覆盖，但要补测试。
4. **日志形态变化**：从「每行一个进程日志」变成「一个进程多行」，必须给每行打 `request_id`，否则无法定位。
5. **回退链路要保活**：`per_call` 分支不能被删除，且崩溃降级会用到它。
6. **分组预解析成本**：按行做一次 `decide + _line_voice` 是纯 Python，开销毫秒级；但若预解析抛错必须整体退回原顺序，否则会少排/漏排行。
7. **停止后行完成顺序错位**：分组后靠后的行可能先完成，章节里已完成的 `tts_state` 呈非连续分布；进度与预计剩余按实际条数统计，但用户视觉上会看到「跳着完成」。

---

## 13. 待拍板选项

| # | 选项 | 建议 | 备选 |
| --- | --- | --- | --- |
| 1 | 空闲退出时长 `idle_timeout_s` | **300s** | 120s（更省内存）/ 600s / 不退出 |
| 2 | 默认模式 `worker_mode` | **resident**（保留 `per_call` 开关） | 先默认 per_call，灰度验证后再切 |
| 3 | 复用范围 | **仅单批次内**（批结束即关） | Web 服务常驻、跨批次复用 |
| 4 | 单行请求超时 | **300s** | 保留 1800s |
| 5 | worker 连续崩溃 | **自动降级 per_call**（保住可用性） | 直接失败该批 |
| 6 | qwen 文字情绪分组策略 | **auto**：向量组 / 文字组各成一组，最多 1 次重启 | force：整批统一加载 qwen，0 次重启但常驻内存更高（未测） |
| 7 | 组内聚簇维度 | **`ref_audio` + `emo_audio_prompt`**（编码次数最少） | 仅 `ref_audio`（实现更简单） |
| 8 | 分组范围 | **章内分组、跨章不重排**（保住章完成顺序与进度语义） | 整个 CLI 进程全局重排（多章收益略高，进度语义变差） |
| 9 | 停止后完成顺序错位 | **接受**（进度/预计剩余按实际条数，不按顺序） | 不分组、严格按章节顺序（放弃分组收益） |

---

## 附录 A：测量方法与复现

```bash
# 单行（复刻生产 worker：起进程 → 加载 → 推理 → 退出）
printf '%s' '{"model_dir":"/Volumes/other/models/ndw/indextts_25","text":"你终于来了，我等了你整整三年。","out_path":"/tmp/ndw_measure_out/smoke.wav","ref_audio":"/Volumes/other/models/voice_refs/cosyvoice_zeroshot_human.wav"}' \
  | env PYTHONPATH=/Users/zhaohaochen/git/index-tts-official HF_HOME=/Volumes/other/hf-cache \
    /Users/zhaohaochen/git/index-tts-official/.venv/bin/python /tmp/ndw_measure_one.py

# 全量对比（3 次解释器基线 + 2 次 torch import 基线 + 5 行逐进程 + 5 行单进程）
/Users/zhaohaochen/git/novel-drama-workbench/.venv/bin/python /tmp/ndw_measure_run.py
```

- 临时脚本只存在于 `/tmp`，不属于工程代码，本次**未修改任何生产代码**。
- 原始数据：`/tmp/ndw_measure_summary.json`。

---

## 14. 实现落地与自测说明

### 14.1 代码落点

| 需求 | 落点 |
| --- | --- |
| 常驻进程 | `tts/providers/indextts25_worker.py` 新增 `--serve`（NDJSON、ping/synth/shutdown、空闲退出、SIGTERM 清理临时文件）；一次性入口保留 |
| 常驻客户端 | `tts/providers/indextts25.py`：懒启动 + 握手、请求 id 对账、单行超时、连续崩溃降级、`close()` |
| 原子落盘 | 常驻与逐行两条路径都先写 `<最终名>.tmp-<pid>.wav`，成功后 `os.replace`，失败删除临时文件 |
| 统一实例 | CLI `tts run` 的分组预解析、队列执行、收尾 `close()` 都用 `GpuQueue` 内部的同一个 `TtsService` |
| 分组调度 | `cli.py:_sort_jobs_by_group`：一级键 `(provider_id, model_dir, use_qwen_emo)`，二级键 `(ref_audio, emo_audio_prompt)`；稳定排序失败时退回原顺序 |
| 停止回收 | Web 仍 `killpg(SIGTERM)`；worker 收到信号先删临时文件再退出；Web 退出钩子复用停止逻辑 |
| 降级 | `worker_mode=per_call` 或常驻连续失败超过 `max_restarts` 后自动走旧链路 |

### 14.2 配置项与默认值

`config/config.yaml` → `tts.providers[]` 下对应 provider 的 `runtime`：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `worker_mode` | `resident` | `resident` 常驻；`per_call` 逐行起进程（回退开关） |
| `idle_timeout_s` | `300` | 空闲多久 worker 自动退出 |
| `request_timeout_s` | `300` | 单行请求超时 |
| `max_restarts` | `2` | 连续崩溃超过该次数后自动降级为 `per_call` |

页面没有独立开关：Web「开始生成」会起一个 CLI 批次，行为由该工程的 `config.yaml` 决定；改完配置需重启 Web 进程。CLI 同理（`--config` 指向不同配置即可切换模式）。

### 14.3 建议自测步骤

1. 选 3~5 行：`tts run --chapter <C> --force --line <id> ... --wait`，看每行日志间隔是否明显缩短；首行会多约 16s 模型加载。
2. 看 worker 是否复用：批次日志里 `[ndw-worker] model loaded` 只应出现一次（切换 qwen 文字情绪时最多多一次）。
3. 确认退出：批次结束后 `pgrep -fl indextts25_worker` 应为空；`find <P> -name '*.tmp-*'` 应为空。
4. 确认停止：Web 点「停止生成」或对 CLI 发 SIGTERM 后，再执行上一步两条命令，均应无残留。
5. 回退验证：把 `worker_mode` 改成 `per_call` 跑 1 行，确认仍能正常出音频。

> 历史基线（上一次只读测量，**不是本次验证结果**）：逐行起进程 24.27s/行，单进程第 2 行起 5.18s/行。
