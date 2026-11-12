# 本地有声剧工作台（novel-drama-workbench）

把「本地模型写小说 → 分角色配音 + 音效 → 有声剧 → 关键配图 / 个别 5 秒短视频」串成一套本机工作台。

- 写作 LLM 固定使用本机 `omlx`（OpenAI 兼容 HTTP，已实测）。
- 图像与视频**不自带模型**，一律调用本机 ComfyUI（内置模板：Qwen-Image Q4 + Lightning 4 步出图、MiniMax-H3 GGUF 5 秒带音轨视频、ACE-Step 1.5 配乐）。
- 单卡串行：写作生成、配音、音效、出图、出视频全部进同一队列，共用 `/Volumes/other/gpu.lock`，避免与 ComfyUI 抢卡。

> 当前状态：**本机端到端可用的工作台**。写作（omlx，OpenAI 兼容 HTTP）、IndexTTS 2.5 常驻 worker / CosyVoice3 配音、音色库与音色编排、音效、ComfyUI 配图/短视频、ACE-Step 配乐、混音/混流/导出（含剪映草稿）均已落地；离线实现保留为无模型验收路径。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/README.md](docs/README.md) | **文档索引**：基线设计 / 迭代 spec / 调研，逐篇标注实现与验证状态 |
| [docs/PRD.md](docs/PRD.md) | 目标用户、场景、核心流程、用户故事、MVP 边界、开放问题 |
| [docs/data-model.md](docs/data-model.md) | 实体、字段含义、必填/可选、状态机、幂等与增量 |
| [docs/architecture.md](docs/architecture.md) | 五层架构、适配层接口、ComfyUI 网关、串行调度、长文本策略 |
| [docs/ui-plan.md](docs/ui-plan.md) | 页面/面板清单、CLI 命令清单、退出码、无界面全流程示例 |
| [docs/spec-r15~r19](docs/README.md) | 常驻配音 worker、剪映导出、音色设计、音色/BGM 前置、大纲与正文续写、配乐两档 |
| [schema/](schema/) | JSON Schema（draft 2020-12），落盘唯一契约 |
| [项目修改记录文档.md](项目修改记录文档.md) | 全部改动登记 |

## 目录结构

```text
novel-drama-workbench/
├── config/
│   └── config.example.yaml   # 复制为 config/config.yaml 后修改（已被 .gitignore 忽略）
├── docs/                     # PRD / 数据模型 / 架构 / 页面与 CLI
├── schema/                   # JSON Schema：project/chapter/scene/character/line/...
├── src/novel_drama/
│   ├── cli.py                # CLI 入口（argparse，无额外依赖）
│   ├── config.py             # 配置加载（YAML，缺省回退内置默认值）
│   ├── models/               # 实体构造器（dict + 工厂函数）、ID、时长估算
│   ├── store/                # 工程读写、队列持久化、schema 校验
│   ├── adapters/             # writing(omlx/offline) / tts / sfx / comfyui + 统一错误码
│   ├── tts/                  # provider 层、常驻 worker、音色/音色设计、行状态与缓存
│   ├── writing/              # 章节生成、续写、润色、反向总结、世界条目
│   ├── director/             # 导演脚本解析与物化
│   ├── orchestration/        # GPU 锁、串行队列、幂等任务、流水线步骤
│   ├── media/                # 时间轴、ffmpeg、混音、配乐、剪映草稿
│   └── web/                  # 本地 Web 工作台（FastAPI + 静态页）
├── tests/smoke_test.py       # 端到端冒烟测试（无需任何模型）
└── pyproject.toml
```

工程数据目录（`ndw init` 生成）：

```text
<工程>/
├── project.json              # 作品与工程设置
├── characters.json           # 角色表（含旁白 chr_narrator）
├── chapters/chp_xxx.json     # 章节 → 场景 → 台词行 / 音效 / 关键图 / 视频段
├── timelines/chp_xxx.json    # 时间轴
├── .ndw/queue.json           # 串行任务队列
├── assets/                   # 语音、音效、图片、视频产物
├── renders/                  # 混音中间产物
└── exports/                  # 导出成片与清单
```

## 安装

要求：macOS、Python 3.10+、ffmpeg（M4 混音需要）。

```bash
cd /Users/zhaohaochen/git/novel-drama-workbench

# 方式一：uv（推荐，本机已用 uv 管理 Python 3.12）
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python PyYAML jsonschema
# 如需 Web 骨架：uv pip install --python .venv/bin/python fastapi uvicorn

# 方式二：已有 Python 3.10+ 虚拟环境
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

安装为命令（可选，之后可直接用 `ndw`）：

```bash
# uv 环境
uv pip install --python .venv/bin/python -e .
# 或 pip 环境
.venv/bin/pip install -e .
```

未安装为命令时，用模块方式运行（本文示例均用这种方式）：

```bash
export PYTHONPATH=src
.venv/bin/python -m novel_drama.cli --help
```

## 配置

```bash
cp config/config.example.yaml config/config.yaml
```

需要重点确认的字段：

| 段 | 字段 | 说明 |
| --- | --- | --- |
| `adapters` | `writing` / `tts` / `sfx` / `comfyui` | 装配层；实际能力用 `providers.writing`（`offline`/`omlx`）、`providers.image/video`（`offline`/`comfyui`）逐项切换 |
| `omlx` | `protocol` / `endpoint` / `api_key` / `model` | 已实测为 OpenAI 兼容 HTTP（默认 `http://127.0.0.1:9000`）；模型下拉从 `GET /v1/models` 动态获取，并显示输入/输出上限 |
| `tts` | `providers[].runtime.worker_mode` | `resident`（默认，整批复用同一进程，模型只加载一次）/ `per_call`（逐行起进程）；另有 `idle_timeout_s`、`max_restarts` |
| `comfyui` | `base_url` | 本机 ComfyUI 地址，默认 `http://127.0.0.1:8188` |
| `comfyui` | `templates_dir` / `templates` | 模板目录（默认 `config/comfy`）与四类模板 ID：`qwen_image_accel` / `minimax_h3_accel` / `minimax_h3_accel_t2v` / `ace_step_1_5` |
| `comfyui` | `models_dir` | ComfyUI 的 models 目录；留空时按 ComfyUI 启动日志的 `Adding extra search path` 与常见安装路径自动探测 |
| `comfyui` | `free_after_job` | 生成结束调用官方 `POST /free` 卸载模型与内存（默认 `true`）；连续批量生成可关掉换速度 |
| `bgm` | `normalize_narrative` | 叙事档是否做 loudnorm（默认 `true`）；铺底档始终归一 |
| `voice` | `denoise` / `design` | 参考音降噪（ffmpeg afftdn，默认开启）与音色设计（本地 VoxCPM2 独立 venv） |
| `models` | `download_mirrors` | 模型下载镜像链，默认 `https://hf-mirror.com` → `https://huggingface.co`（国内优先、国外降级） |
| `gpu` | `lock_path` | 与 ComfyUI 共用的锁文件，默认 `/Volumes/other/gpu.lock` |

配置也可用 `NDW_PROJECT` 环境变量指定默认工程目录；命令行 `--project` 优先级最高。

## 快速开始（不接模型也能跑）

```bash
export PYTHONPATH=src
PY=.venv/bin/python

# 1. 建工程
$PY -m novel_drama.cli init ~/novels/changan --title 长夜将明 --author 我 --genre 悬疑

# 2. 加章节（直接从文件导入正文）
$PY -m novel_drama.cli -p ~/novels/changan chapter add --title 第一章 --content-file ./ch1.md
# 整本导入：重复执行不会造重复章节（默认 skip，命中即退出码 1 并说明已存在哪一章）
$PY -m novel_drama.cli -p ~/novels/changan chapter import ./book.md
$PY -m novel_drama.cli -p ~/novels/changan chapter import ./book.md --on-duplicate replace            # 覆盖正文
$PY -m novel_drama.cli -p ~/novels/changan chapter import ./book.md --on-duplicate replace --force-replace  # 已有台词行/音频也覆盖

# 2.1 正文：生成 / 手工保存 / 按提示词改写 / 按正文重总结大纲
$PY -m novel_drama.cli -p ~/novels/changan chapter generate --chapter chp_xxx
$PY -m novel_drama.cli -p ~/novels/changan chapter save --chapter chp_xxx --file ./ch1.md
$PY -m novel_drama.cli -p ~/novels/changan chapter edit --chapter chp_xxx --instruction "把对峙写得更克制，删掉解释性旁白"   # 默认预览
$PY -m novel_drama.cli -p ~/novels/changan chapter edit --chapter chp_xxx --instruction "压缩成两句" --range 120:260 --apply
$PY -m novel_drama.cli -p ~/novels/changan chapter summarize --chapter chp_xxx          # 预览：按正文重总结标题/摘要/大纲
$PY -m novel_drama.cli -p ~/novels/changan chapter summarize --chapter chp_xxx --apply  # 落盘并同步设定库节拍

# 2.2 续写：默认只出预览草稿，加 --apply 才落盘；--retry 覆盖重抽
$PY -m novel_drama.cli -p ~/novels/changan outline continue --chapters 3 --brief "进入第二卷，节奏加快"
$PY -m novel_drama.cli -p ~/novels/changan outline continue --chapters 3 --apply
$PY -m novel_drama.cli -p ~/novels/changan chapter continue --chapter chp_xxx --words 800
$PY -m novel_drama.cli -p ~/novels/changan chapter continue --chapter chp_xxx --words 800 --intent "把对峙推到摊牌" --apply

# 3. 角色与音色
$PY -m novel_drama.cli -p ~/novels/changan character add --name 沈砚 --role protagonist --alias 沈队
$PY -m novel_drama.cli -p ~/novels/changan voice add --name 沈砚 --preset Serena --provider cosyvoice3_mlx
$PY -m novel_drama.cli -p ~/novels/changan voice variant-add --voice 沈砚 --variant-name 真人参考 \
  --source cloned --audio ./ref.wav --ref-text "参考音频对应文本" --consent --provider cosyvoice3_mlx
$PY -m novel_drama.cli -p ~/novels/changan voice set-default --voice 沈砚 --variant 真人参考
$PY -m novel_drama.cli -p ~/novels/changan voice bind --character 沈砚 --voice 沈砚 --adapter cosyvoice3_mlx
$PY -m novel_drama.cli -p ~/novels/changan voice preview --voice 沈砚 --variant 真人参考 --text "试听一句"

# 3.1 内置音色：首次按需下载到外置盘（默认 /Volumes/other/models/voice_refs/builtin）
$PY -m novel_drama.cli voice builtin list                 # 看清单与下载状态
$PY -m novel_drama.cli voice builtin install              # 下载全部（约 2MB，24k 单声道）
$PY -m novel_drama.cli -p ~/novels/changan voice builtin import   # 导入当前工程，可重复执行
$PY -m novel_drama.cli -p ~/novels/changan voice bind --character 沈砚 --voice "内置男声 01"

# 3.2 Web：人物设计 → 「音色库」子标签，可浏览器录音 / 上传 / 试听 / 绑定角色（支持 afftdn 降噪）

# 3.3 全剧音色编排：校验跨章音色、旁白区间、龙套音色池；页面在人物页「音色编排」入口
$PY -m novel_drama.cli -p ~/novels/changan cast check
$PY -m novel_drama.cli -p ~/novels/changan cast backfill --dry-run     # 补缺失音色描述，默认 dry-run

# 3.4 按人物设定抽音色候选（本地 VoxCPM2，需独立 venv，见 config voice.design）
$PY -m novel_drama.cli -p ~/novels/changan voice design --character 沈砚 --count 4

# 4. 拆场景 → 切台词
$PY -m novel_drama.cli -p ~/novels/changan scene split --chapter chp_xxx
$PY -m novel_drama.cli -p ~/novels/changan line split --chapter chp_xxx
$PY -m novel_drama.cli -p ~/novels/changan line list --chapter chp_xxx --status unknown

# 4.1 返工查询：一条命令看「哪些句子要重做」（计数与 tts plan 同源，不会对不上）
$PY -m novel_drama.cli -p ~/novels/changan line list --chapter chp_xxx --status pending,stale,failed
$PY -m novel_drama.cli -p ~/novels/changan line diff --chapter chp_xxx --line lin_xxx    # 改了哪些参数
$PY -m novel_drama.cli -p ~/novels/changan line revert --chapter chp_xxx --line lin_xxx  # 只回参数，不回文本

# 4.2 原文对照：导入时已把清洗前原文存进 .ndw/imports/
$PY -m novel_drama.cli -p ~/novels/changan chapter source --chapter chp_xxx            # 快照信息
$PY -m novel_drama.cli -p ~/novels/changan chapter source --chapter chp_xxx --diff     # 哪几段被我删了/改了
$PY -m novel_drama.cli -p ~/novels/changan chapter source --chapter chp_xxx --paragraph 3  # 这段原文去哪了

# 4.3 导演脚本（人工标注，规则解析，不调用 LLM）
$PY -m novel_drama.cli script syntax
$PY -m novel_drama.cli -p ~/novels/changan script parse --chapter chp_xxx --file ./ch1.ndw
$PY -m novel_drama.cli -p ~/novels/changan script apply --chapter chp_xxx

# 4.4 虚拟分组：按条件批量改行参数（存 .ndw/groups/，不动章节 JSON）
$PY -m novel_drama.cli -p ~/novels/changan group list  --chapter chp_xxx                  # 顺带报未分组/重复分组
$PY -m novel_drama.cli -p ~/novels/changan group diff  --chapter chp_xxx --group grp_a1b2  # 哪些行、旧值→新值
$PY -m novel_drama.cli -p ~/novels/changan group apply --chapter chp_xxx --group grp_a1b2  # 物化写入，命中行变 stale

# 5. 配音：行级批量（单并发、失败不中断、逐行落 sidecar）
$PY -m novel_drama.cli -p ~/novels/changan tts batch --chapter chp_xxx --batch-size 5 --limit 5
$PY -m novel_drama.cli -p ~/novels/changan tts batch --chapter chp_xxx --dry-run
$PY -m novel_drama.cli -p ~/novels/changan tts plan --chapter chp_xxx --scope selected --line lin_xxx --line lin_yyy
$PY -m novel_drama.cli -p ~/novels/changan tts batch --chapter chp_xxx --status pending,stale --batch-size 5
# 同一句多 provider 对比试听（默认产物落 <工程>/var/tts/compare，随工程迁移）
$PY -m novel_drama.cli -p ~/novels/changan tts compare --text "你终于来了。"

# 6. 时间轴（自动读取脚本静音事件）→ 可核对时间表 → 拼接 → 导出
$PY -m novel_drama.cli -p ~/novels/changan timeline build --chapter chp_xxx
$PY -m novel_drama.cli -p ~/novels/changan timeline table --chapter chp_xxx --out ./schedule.tsv
$PY -m novel_drama.cli -p ~/novels/changan concat --chapter chp_xxx
$PY -m novel_drama.cli -p ~/novels/changan export --chapter chp_xxx --format srt
$PY -m novel_drama.cli -p ~/novels/changan export --chapter chp_xxx --format jianying   # 剪映草稿 zip，素材自包含

# 7. 图像/视频/混流：providers.image/video=comfyui 时真实出图/出片；--dry-run 只列模板、画幅与宽高
$PY -m novel_drama.cli -p ~/novels/changan image run --chapter chp_xxx --dry-run
$PY -m novel_drama.cli -p ~/novels/changan video run --chapter chp_xxx --dry-run
$PY -m novel_drama.cli -p ~/novels/changan mux --chapter chp_xxx

# 7.0 配乐：ACE-Step 两档（auto 按场景推断 / narrative 叙事 / bed 铺底）
#    英文提示词来自标注阶段（!bgm ... en=...）；--prompt 仅用于记录，调试可加 --rewrite-prompt
$PY -m novel_drama.cli -p ~/novels/changan bgm generate --chapter chp_xxx --plan bgp_xxx --duration 40 --mode auto
$PY -m novel_drama.cli -p ~/novels/changan bgm generate --chapter chp_xxx --prompt-en "tense low strings, sparse piano, slow ticks" --duration 40 --mode auto
$PY -m novel_drama.cli -p ~/novels/changan bgm sync --chapter chp_xxx   # 按导演脚本 BGM 描述重建 cue

# 7.1 ComfyUI 模板与模型（不需要工程）
$PY -m novel_drama.cli comfy list                 # 模板、必需占位符、固定参数、依赖模型
$PY -m novel_drama.cli comfy health               # 连通性 + 模板校验 + 依赖模型
$PY -m novel_drama.cli comfy scan                 # 扫描 models 目录并写回 config/comfy/models.json
$PY -m novel_drama.cli comfy resolve              # 反查线上地址（加 --refresh 忽略缓存）
$PY -m novel_drama.cli comfy models list
$PY -m novel_drama.cli comfy models verify
$PY -m novel_drama.cli comfy models install --key vae/qwen_image_vae.safetensors
$PY -m novel_drama.cli comfy models install --key vae/qwen_image_vae.safetensors --dry-run
$PY -m novel_drama.cli comfy models install --key vae/qwen_image_vae.safetensors --mirror https://hf-mirror.com
$PY -m novel_drama.cli comfy import --id my_flow --kind image --workflow ./my_workflow.json
$PY -m novel_drama.cli --json comfy scan          # --json 是全局参数，必须写在子命令前
$PY -m novel_drama.cli models install --key cosyvoice3/8bit --mirror https://hf-mirror.com

# 8. 自检与队列
$PY -m novel_drama.cli -p ~/novels/changan doctor
$PY -m novel_drama.cli -p ~/novels/changan queue list

# 9. Web 骨架（需安装可选依赖）
$PY -m novel_drama.cli -p ~/novels/changan serve --port 8765
```

冒烟测试：

```bash
PYTHONPATH=src .venv/bin/python tests/smoke_test.py   # 期望输出 SMOKE OK
```

## 适配层对接约定

所有实现都继承 `src/novel_drama/adapters/base.py` 中的接口，错误统一为 `AdapterError(code, message, retryable)`：

| 错误码 | 含义 | 是否自动重试 |
| --- | --- | --- |
| `NOT_IMPLEMENTED` | 该分支尚未对接（如关闭的模型路径） | 否 |
| `UNAVAILABLE` | 服务未启动 / 端口不通 | 是 |
| `TIMEOUT` | 调用或排队超时 | 是 |
| `RATE_LIMITED` | 上游限流 | 是 |
| `INVALID_INPUT` | 参数不合法（如缺音色） | 否 |
| `UPSTREAM_ERROR` | 上游返回错误 | 视 `retryable` |
| `CONTENT_BLOCKED` | 内容被拒绝 | 否 |

四个接口的现状：

1. `WritingAdapter`：`OmlxWritingAdapter` 已落地（OpenAI 兼容 HTTP，支持流式、上下文预算与 stop）；章节生成、续写、润色、导演标注共用同一适配器，结构化输出统一走工具调用。`offline` 实现保留为无模型验收路径。
2. `TtsAdapter`：输入文本 + 音色 + 情绪/语速/停顿，输出音频文件与**实测时长**，时长回写台词行。已落地 `indextts_25`（常驻 worker，模型整批只加载一次）与 `cosyvoice3_mlx` 两个真实 provider，含音色变体、内置音色、参考音降噪与音色设计。
3. `SfxAdapter`：离线实现用 ffmpeg lavfi 合成 5 类音效，真出 WAV；按描述检索/生成音效的模型路径尚未接入。
4. `ComfyGateway`：本机 HTTP 网关已落地（`health` / `submit` / `status` / `fetch` / `cancel` / `interrupt` / `queue_snapshot`，支持任务结束 `POST /free` 释放显存）；提交 API 格式工作流，轮询历史，取产物到工程目录。

串行与幂等：

- 所有 GPU 任务（含 omlx 文本生成）进入 `.ndw/queue.json`，单进程消费，持 `/Volumes/other/gpu.lock` 时才执行。
- 锁文件用 `O_CREAT|O_EXCL` 创建并写入 pid；持有进程消失时自动接管。
- 幂等键 `sha256(kind | target | input_revision | adapter | params_hash)`，重复入队复用既有任务；产物按 `<实体>_r<rev>.ext` 命名，输入未变则跳过。
- 失败按 `retryable` 与最大尝试次数自动回队；进程退出时 `running` 任务标记为 `interrupted`，下次启动续跑。

## 图像与视频（ComfyUI 模板）

图像与视频仍由本机 ComfyUI 出片，但**工作流不再要求用户自己准备**：本机调好的工作流转成了工程内置模板，放在 `config/comfy/`，由 `manifest.json` 登记。

| 模板 ID | 用途 | 固定模型 | 固定参数 | 画幅桶（宽×高） |
| --- | --- | --- | --- | --- |
| `qwen_image_accel` | 关键图 | `qwen-image-Q4_K_S.gguf` + `Qwen-Image-Lightning-4steps-V2.0-bf16.safetensors`（强度 1.0） | 4 步 / CFG 1.0 / euler / simple / shift 3.1 | 1:1 1328×1328、9:16 928×1664、16:9 1664×928、3:4 1140×1472、4:3 1472×1140 |
| `minimax_h3_accel` | 短视频（图生视频） | `MiniMax-H3-FL2VA-Pruned-Q4_K_M.gguf` + `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | 4 步 / res_multistep / simple / CFG 1.0 / 124 帧 / 24fps / mp4 h264 | 9:16 512×896、16:9 896×512、1:1 768×768 |
| `minimax_h3_accel_t2v` | 短视频（文生视频） | 同上 | 同上 | 同上 |

几条硬约定：

- **模板来源**：`config/comfy/source/` 保留最初从 ComfyUI 导出的 UI 工作流（只作来源追溯）；实际使用的是转换后的 API 模板 `config/comfy/<id>.json`，两者都由 `manifest.json` 关联。
- **参数不可调**：除 `prompt / negative_prompt / width / height / seed / filename_prefix / input_image` 外，步数、CFG、采样器、调度器、模型、LoRA、帧率、帧数全部固定在模板与 manifest 的 `fixed` 里，CLI 与页面都不提供修改入口。
- **只随画幅调宽高**：宽高不是自由输入，由工程 `settings.output.aspect_ratio`（`1:1/3:4/4:3/9:16/16:9`，默认 `1:1`）或条目自身的 `aspect_ratio` 经模板 `size_map` 映射得到；视频只支持 `9:16/16:9/1:1`，其它画幅取最接近的一档并在结果里注明。
- **占位符必须全部解析**：转换后仍有 `{{...}}` 残留时直接报 `INVALID_INPUT`，不会发请求。
- **失败不回退占位图**：`providers.image/video=comfyui` 时走真实生成，失败只把条目写成 `asset.status=failed` 并保留中文错误，不会偷偷换成离线渲染图；i2v 缺首帧才降级为 t2v，并在结果里记录原因。
- **串行占卡**：提交 → 轮询 → 取回整段在 GPU 锁内执行，与配音/写作共用同一把锁；产物按 `<条目ID>_r<revision>.<ext>` 落 `assets/`。
- **模型清单**：`config/comfy/models.json` 记录 9 个依赖模型的体积、许可与被哪些模板引用；`config/comfy/models.local.json` 是本机反查到的线上地址缓存，**已在 `.gitignore` 中，不入库**。
- **下载镜像链**：默认 `https://hf-mirror.com` → `https://huggingface.co`，可用 `models.download_mirrors` 改；下载先落暂存，体积校验通过后再原子改名就位。

本机冒烟结论（ComfyUI 0.44.1）：关键图 `928×1664` 成功；i2v 与 t2v 均为 `512×896` / 124 帧 / 5.166s。

## 当前完成度

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 工程数据层 | 可用 | 建工程、章节/角色读写、原子落盘、schema 校验 |
| 设定库 | 可用 | `settings/bible.json`：premise / world / outline / style / continuity，`setting show\|set\|world\|validate\|import\|export`；每次写入 `settings_revision +1` |
| 大纲设计 | 可用 | `outline generate` → `outline apply` 落地成章节；`outline continue` 在已有大纲后追加新章（默认预览，`--apply` 才写设定库） |
| 章节生成 | 可用 | `chapter generate` 按设定库 + 节拍产出正文，字数可配，重复执行需 `--regen`；上下文含故事圣经、绑定世界、角色卡、前 3 章摘要、前文摘录，按预算裁剪 |
| 章节编辑、续写与反向总结 | 可用 | `chapter save` 手工保存正文；`chapter edit` 按提示词改写；`chapter continue` 在正文末尾续写（默认预览草稿，`--apply` 追加，`--retry` 重抽，`--intent` 传自由意图）；`chapter summarize` 从正文反向总结并同步设定库 |
| 文本切分 | 可用 | 规则版场景切分（空行/转场词/字数上限）、台词切分（引号/旁白/内心独白）、说话人别名匹配 |
| 导演标注 | 可用 | `script suggest [--apply]` 产出镜头/旁白/情绪/音效/画面/视频位/BGM 描述标注，并物化成场景内媒体条目 |
| 时间轴 | 可用 | 语音/音效/图/视频轨排布，未合成时用估算时长并告警 |
| 串行队列与锁 | 可用 | 持久化队列、跨进程 GPU 锁、幂等、失败重试、断点续跑 |
| CLI | 可用 | 全链路可跑；`produce` 一次串起全流程并落 `.ndw/runs/<run_id>.json` |
| Web 工作台 | 可用 | 三栏布局：左「书→幕→章→场景」树（带流水线状态灯）+ 中区工作区 + 右「上下文」Inspector，底部为当前章节时间线与试听；7 个 hash 路由（`#/project` `#/outline` `#/chapter` `#/script` `#/voice` `#/media` `#/export`），写操作全部转调后端，媒体与产物经只读 `/api/ui/file` 就地预览 |
| 写作适配 | 可用 | `providers.writing=omlx` 接本机 OpenAI 兼容 HTTP，模型下拉动态获取；`offline` 为确定性离线实现 |
| TTS provider 层 | 可用 | 主力 IndexTTS 2.5（常驻 worker 复用同一进程，8 维情绪向量 / emo_text / emo_audio_prompt 三通路互斥）；备用 CosyVoice3-0.5B(MLX)；`offline_dub` 仅供无模型验收 |
| 音色库与音色编排 | 可用 | 多变体音色库、内置音色导入、参考音降噪、本地 VoxCPM2 音色设计；`cast check` 校验跨章音色、旁白区间与龙套音色池，页面在人物页可编辑音色编排 |
| 音效 | 可用（模型为占位） | 离线实现用 ffmpeg lavfi 合成 5 类环境/拟音/冲击/转场/和弦，真出 WAV |
| 配图 | 可用 | 离线实现用 Pillow 渲染渐变 + 标题 + 提示词，真出 PNG；`providers.image=comfyui` 时走内置模板真实出图，失败不回退占位图 |
| 视频位 | 可用 | 离线实现用 ffmpeg 把静帧渲成 500–5000ms 片段；`providers.video=comfyui` 时走内置模板出片（i2v 缺首帧自动降级 t2v） |
| 配乐（BGM） | 可用 | ACE-Step 1.5 分「叙事 / 铺底」两档；`bgm sync` 按导演脚本 BGM 描述重建 cue；记录档位与生成/后处理耗时 |
| 混音 / 混流 / 导出 | 可用 | `mix --render` 分轨混音 + 响度规整；`mux --render` 逐段渲片 + concat + AAC 合流；`export` 支持 `srt` / `bundle`（MP4+MP3+SRT+manifest）/ `jianying`（剪映草稿 zip） |

## 离线端到端验收（不接任何模型）

```bash
.venv/bin/python3.12 tests/e2e_offline.py
```

默认在外部卷的 `ndw_runs/e2e_offline/proj_<时间戳>` 建工程（可用 `NDW_E2E_DIR` 改根目录；测试工程与运行时配置都落在仓库外，仓库里只有 fixture），
用 `tests/fixtures/e2e_offline.yaml`（写作/音效/配图/视频全离线，配音走 `offline_dub`），
从建项目一路跑到导出交付包，末尾逐项校验 `renders/*.mp4`、`exports/*.{mp3,mp4,srt,bundle.json}` 与 `README.txt`。

## 里程碑与完成判定

| 里程碑 | 范围 | 怎么算完成 |
| --- | --- | --- |
| M1 文本与工程管理 | 工程、章节、场景、角色、台词切分与校对 | 从空目录建工程 → 导入章节 → 拆场景 → 切台词 → 改说话人/情绪 → 全部 JSON 通过 schema 校验；不依赖任何模型；`tests/smoke_test.py` 通过 |
| M2 配音与时间轴 | TTS 适配器、音色绑定、逐行合成、实测时长、时间轴 | 绑定音色后 `tts run --wait` 全绿；台词行 `tts.status=ready` 且 `duration_ms` 为实测值；时间轴无「估算」告警；重复执行不重复合成 |
| M3 音效与配图 | 音效适配器、关键图提示词、ComfyUI 出图 | 每场至少 1 条音效可播放并落位正确；关键图按锚点行生成并进入时间轴；ComfyUI 不可达时给出明确错误与重试；队列与 ComfyUI 不并发占卡 |
| M4 短视频与成片导出 | 5 秒视频段、混音、字幕、导出 | i2v/t2v 段时长 ≤ 5000ms 且带音轨；`mix` 产出可用音轨；`export` 产出 MP4/MP3/SRT/manifest 并可完整播放；manifest 与时间轴 revision 对应 |

## 已知限制

- 音效的**模型生成/检索**路径尚未接入；当前只有 ffmpeg 离线合成，够用于验收与草稿。
- 常驻配音 worker 已实现（`worker_mode=resident`），但只做过语法与静态检查，未做整章真机跑测；需要时把 `worker_mode` 改回 `per_call` 可回退旧的逐行行为。
- 剪映导出只产出草稿 zip，**未做剪映专业版侧的人工导入实测**；素材自包含、30fps、分镜级，导入后仍需人工核对轨道。
- 大纲/正文续写：CLI 与 Web 入口均已接入，正文/大纲页做过浏览器实测；未覆盖所有异常分支的页面回归。
- 音色设计（VoxCPM2）已用真实 worker 跑通单条候选与入库，未做 4 条候选的完整页面验收；依赖独立 venv，`config/config.example.yaml` 里 `voice.design.python` 是本机绝对路径，换机需改。
- ACE-Step 叙事档不裁首尾静音，素材首尾可能有近静音区间，直接循环会有断感；铺底档已裁开头静音并加 0.8s 淡入。
- ACE-Step 单次生成耗时主要集中在音频代码 LM 阶段（60 秒素材实测约 87 秒，其中去噪仅约 2 秒），批量生成偏慢。
- ComfyUI 的 `input/`、`output/` 是全局共享命名空间，目前只靠 ULID 文件名避免重名，长期运行会累积文件，需要手工清理或后续加分目录策略。
- `line split` 会把叙述句也切成行，长章节的配音行数偏多（后续可按段落合并叙述行）。
- 产品层面的开放问题（音色来源、导出规格等）见 [docs/PRD.md](docs/PRD.md) 的开放问题清单，未替用户做决定。
