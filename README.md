# 本地有声剧工作台（novel-drama-workbench）

把「本地模型写小说 → 分角色配音 + 音效 → 有声剧 → 关键配图 / 个别 5 秒短视频」串成一套本机工作台。

- 写作 LLM 固定使用本机 `omlx`（协议待确认，先按接口抽象）。
- 图像与视频**不自带模型**，一律调用本机 ComfyUI（Qwen-Image 2.1 + Krea2 三阶段出图、MiniMax-H3 GGUF 5 秒带音轨视频、LTX 2.3 i2v）。
- 单卡串行：写作生成、配音、音效、出图、出视频全部进同一队列，共用 `/Volumes/other/gpu.lock`，避免与 ComfyUI 抢卡。

> 当前状态：**工程骨架（M1 数据链路）**。文本处理、场景/台词切分、时间轴、队列与锁为真实实现；所有模型调用（omlx / TTS / 音效 / ComfyUI / 混音导出）均为显式 `NotImplemented` 占位，按约定返回 `NOT_IMPLEMENTED`，不会假成功。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/PRD.md](docs/PRD.md) | 目标用户、场景、核心流程、用户故事、MVP 边界、开放问题 |
| [docs/data-model.md](docs/data-model.md) | 实体、字段含义、必填/可选、状态机、幂等与增量 |
| [docs/architecture.md](docs/architecture.md) | 五层架构、适配层接口、ComfyUI 网关、串行调度、长文本策略 |
| [docs/ui-plan.md](docs/ui-plan.md) | 页面/面板清单、CLI 命令清单、退出码、无界面全流程示例 |
| [docs/roadmap-remaining.md](docs/roadmap-remaining.md) | 剩余里程碑路线图、资源占用、依赖顺序与验收标准 |
| [schema/](schema/) | 16 个 JSON Schema（draft 2020-12），落盘唯一契约 |
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
│   ├── adapters/             # writing(omlx) / tts / sfx / comfyui + 统一错误码
│   ├── orchestration/        # GPU 锁、串行队列、幂等任务、流水线步骤
│   ├── media/                # 时间轴构建、ffmpeg 调用、混音计划
│   └── web/                  # FastAPI 骨架（可选依赖）
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
| `adapters` | `writing` | 固定 `omlx`；`tts`/`sfx` 在对接前保持 `placeholder` |
| `omlx` | `protocol` / `endpoint` / `cli_path` | **待回填**：确认为 `http`、`openai_compatible` 或 `cli` 后再填 |
| `comfyui` | `base_url` | 本机 ComfyUI 地址，默认 `http://127.0.0.1:8188` |
| `comfyui` | `templates_dir` / `templates` | API 格式工作流模板目录与三类模板 ID |
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

# 3. 角色与音色
$PY -m novel_drama.cli -p ~/novels/changan character add --name 沈砚 --role protagonist --alias 沈队
$PY -m novel_drama.cli -p ~/novels/changan voice add --name 沈砚 --preset Serena --provider cosyvoice3_mlx
$PY -m novel_drama.cli -p ~/novels/changan voice variant-add --voice 沈砚 --variant-name 真人参考 \
  --source cloned --audio ./ref.wav --ref-text "参考音频对应文本" --consent --provider cosyvoice3_mlx
$PY -m novel_drama.cli -p ~/novels/changan voice set-default --voice 沈砚 --variant 真人参考
$PY -m novel_drama.cli -p ~/novels/changan voice bind --character 沈砚 --voice 沈砚 --adapter cosyvoice3_mlx
$PY -m novel_drama.cli -p ~/novels/changan voice preview --voice 沈砚 --variant 真人参考 --text "试听一句"

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

# 6. 时间轴（自动读取脚本静音事件）→ 可核对时间表 → 拼接 → 导出
$PY -m novel_drama.cli -p ~/novels/changan timeline build --chapter chp_xxx
$PY -m novel_drama.cli -p ~/novels/changan timeline table --chapter chp_xxx --out ./schedule.tsv
$PY -m novel_drama.cli -p ~/novels/changan concat --chapter chp_xxx
$PY -m novel_drama.cli -p ~/novels/changan export --chapter chp_xxx --format srt

# 7. 图像/视频/混流：本轮只出 dry-run 计划并返回退出码 3（NOT_IMPLEMENTED）
$PY -m novel_drama.cli -p ~/novels/changan image run --chapter chp_xxx --dry-run
$PY -m novel_drama.cli -p ~/novels/changan video run --chapter chp_xxx --dry-run
$PY -m novel_drama.cli -p ~/novels/changan mux --chapter chp_xxx

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

## 适配层对接约定（写作/音效/ComfyUI 仍占位，TTS 已落地）

所有实现都继承 `src/novel_drama/adapters/base.py` 中的接口，错误统一为 `AdapterError(code, message, retryable)`：

| 错误码 | 含义 | 是否自动重试 |
| --- | --- | --- |
| `NOT_IMPLEMENTED` | 本轮占位，尚未对接 | 否 |
| `UNAVAILABLE` | 服务未启动 / 端口不通 | 是 |
| `TIMEOUT` | 调用或排队超时 | 是 |
| `RATE_LIMITED` | 上游限流 | 是 |
| `INVALID_INPUT` | 参数不合法（如缺音色） | 否 |
| `UPSTREAM_ERROR` | 上游返回错误 | 视 `retryable` |
| `CONTENT_BLOCKED` | 内容被拒绝 | 否 |

待对接的四个接口：

1. `WritingAdapter`（omlx）：文本进、文本出、支持流式，可设置上下文预算与 stop；章节续写、台词切分、情绪标注、提示词生成共用同一个适配器。协议确认后实现 `OmlxHttpAdapter` 或 `OmlxCliAdapter`。
2. `TtsAdapter`：输入文本 + 音色 + 情绪/语速/停顿，输出音频文件与**实测时长**；时长回写台词行，时间轴以实测为准。**已落地**：`tts/providers/` 下 `indextts_25`（子进程）与 `cosyvoice3_mlx`（mlx_audio）两个真实实现，见 `docs/spec-r13-indextts25.md`。
3. `SfxAdapter`：按类型/描述生成或检索音效，返回音频与时长；可选搜索实现。
4. `ComfyGateway`：`health` / `submit` / `status` / `fetch` / `cancel` / `interrupt` / `queue_snapshot`；提交 API 格式工作流 JSON，轮询历史，取产物到 `assets/`。

串行与幂等：

- 所有 GPU 任务（含 omlx 文本生成）进入 `.ndw/queue.json`，单进程消费，持 `/Volumes/other/gpu.lock` 时才执行。
- 锁文件用 `O_CREAT|O_EXCL` 创建并写入 pid；持有进程消失时自动接管。
- 幂等键 `sha256(kind | target | input_revision | adapter | params_hash)`，重复入队复用既有任务；产物按 `<实体>_r<rev>.ext` 命名，输入未变则跳过。
- 失败按 `retryable` 与最大尝试次数自动回队；进程退出时 `running` 任务标记为 `interrupted`，下次启动续跑。

## 当前完成度

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 工程数据层 | 可用 | 建工程、章节/角色读写、原子落盘、schema 校验 |
| 设定库 | 可用 | `settings/bible.json`：premise / world / outline / style / continuity，`setting show|set|world|validate|import|export`；每次写入 `settings_revision +1` |
| 大纲设计 | 可用 | `outline generate`（模型调用，缺省离线实现）→ `outline apply` 落地成章节 |
| 章节生成 | 可用 | `chapter generate` 按设定库 + 节拍产出正文，字数可配，重复执行需 `--regen` |
| 文本切分 | 可用 | 规则版场景切分（空行/转场词/字数上限）、台词切分（引号/旁白/内心独白）、说话人别名匹配 |
| 导演标注 | 可用 | `script suggest [--apply]` 产出镜头/旁白/情绪/音效/画面/视频位标注，并物化成场景内媒体条目 |
| 时间轴 | 可用 | 语音/音效/图/视频轨排布，未合成时用估算时长并告警 |
| 串行队列与锁 | 可用 | 持久化队列、跨进程 GPU 锁、幂等、失败重试、断点续跑 |
| CLI | 可用 | 全链路可跑；`produce` 一次串起 15 个阶段并落 `.ndw/runs/<run_id>.json` |
| Web 工作台 | 可用 | 7 个页签（项目/大纲/章节/导演标注/台词校对/音效·配图·视频/导出），写操作全部转调 CLI |
| 写作适配 | 可用（模型为占位） | 离线实现确定性产文；`omlx` 协议未回填 |
| TTS provider 层 | 可用 | 主力 IndexTTS 2.5（子进程调用独立 3.11 环境，8 维情绪向量 / emo_text / emo_audio_prompt 三通路互斥）；备用 CosyVoice3-0.5B(MLX)。均已实测出音；`offline_dub` 仅供无模型验收 |
| 音效 | 可用（模型为占位） | 离线实现用 ffmpeg lavfi 合成 5 类环境/拟音/冲击/转场/和弦，真出 WAV |
| 配图 | 可用（模型为占位） | 离线实现用 Pillow 渲染渐变 + 标题 + 提示词，真出 PNG（长边 ≤1536） |
| 视频位 | 可用（模型为占位） | 离线实现用 ffmpeg 把静帧渲成 500–5000ms 片段，真出 MP4 |
| 混音 / 混流 / 导出 | 可用 | `mix --render` 分轨混音 + 响度规整；`mux --render` 逐段渲片 + concat + AAC 合流；`export --format bundle` 产出 MP4/MP3/SRT/manifest/bundle/README |

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

## 本轮已知限制

- 写作、音效、配图、视频位四类**模型调用**仍是离线实现：接口、数据、状态、UI、失败处理都已按真实契约落地，换成真模型只需新增 adapter 并改 `providers.*`，上层不用动。
- `omlx` 写作协议未回填，`OmlxWritingAdapter` 仍抛 `NOT_IMPLEMENTED`；`offline_dub` 只是验收用占位配音 provider，真实链路默认仍是 IndexTTS 2.5。
- 配音是逐行串行合成（IndexTTS 单进程），46 行约需 10 分钟；`produce` 支持断点续跑，重跑只补未完成阶段。
- `line split` 会把叙述句也切成行，长章节的配音行数偏多（后续可按段落合并叙述行）。
- 产品层面的开放问题（音色来源、音效生成方式、导出规格等）见 [docs/PRD.md](docs/PRD.md) 的开放问题清单，未替用户做决定。
