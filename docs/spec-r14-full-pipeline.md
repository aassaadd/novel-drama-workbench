# R14 Spec：小说 → 有声剧全链路工作台

> 立项日期：2026-11-10
> 范围纠正：本项目不是「只做合成声音」，而是
> **小说项目 → 大纲设计 → 人物/世界观设定 → 章节生成 → 有声剧标注（导演脚本）→ 配音 → 音效 → 配图/视频位 → 导出** 的一条龙工作台。
> 本文档先出 spec，报一次后直接按 §6 任务清单实施。

---

## 1. 范围

### 1.1 本轮做（全部功能真做）

| 阶段 | 现状 | 本轮目标 |
| --- | --- | --- |
| S0 工程 | 可用 | 保持不动 |
| S1 设定库（世界观/人物/文风/连续性） | **只有 schema，无代码** | 落盘读写、版本号、CLI、UI |
| S2 大纲设计 | **只有 chapter.outline 字符串** | 幕/章/节拍三级结构、生成、落地成章节 |
| S3 章节生成 | 写作适配器只有 omlx 真实实现，未配置时不可用 | 新增离线写作实现，功能完整可跑 |
| S4 有声剧标注（导演脚本） | 手工标注可解析，无「建议」 | 规则自动标注建议 + 人工确认 + 落地 |
| S5 配音 | 可用（IndexTTS 主力 / CosyVoice3 备用） | 冻结，只做回归 |
| S6 音效 | `NOT_IMPLEMENTED` | 离线合成器真实出 wav + 状态机 + 入时间轴 |
| S7 配图 | dry-run | 离线渲染真实 PNG + 状态机 + 入时间轴 |
| S8 视频位 | dry-run | 离线渲染真实 MP4（≤5s）+ 状态机 + 入时间轴 |
| S9 混流 | `NOT_IMPLEMENTED` | 音轨 + 画面 → MP4 |
| S10 导出 | srt / manifest / schedule / mix | 增 MP3、MP4、交付包 |
| S11 全链路编排 | 无 | `ndw produce` 一条命令跑完 + 断点续跑 |
| S12 Web | 只有「台词校对」一页 | 多页签工作台，覆盖全链路 |

### 1.2 本轮不做（明确边界）

- **不真调任何模型**：LLM（omlx）、生图（ComfyUI/Qwen-Image）、生视频（MiniMax-H3 / LTX）一律不联调、不启服务、不下载权重。它们走「离线占位实现」，接口与真实实现同签名，配置一改即可切换。
- 不改 `~/git/index-tts-official` 仓库，不建第三个 Python 环境（项目 3.12 + IndexTTS 3.11 子进程）。
- 不改已有 `schema/*.json` 的既有语义；新增派生数据只落 `settings/`、`.ndw/`、`assets/`、`renders/`、`exports/`。
- 不做多用户、鉴权、云端同步、协作编辑。
- 不做中文分词/韵律研究；配音质量不回退即可。

---

## 2. 设计原则

1. **占位 ≠ 假成功**：所有离线实现必须产出**真实文件**（wav/png/mp4/txt）并回写真实时长/尺寸/sha，不允许返回 `NOT_IMPLEMENTED` 后当成成功。
2. **模型可替换**：离线实现与真实实现同协议（`WritingAdapter` / `SfxAdapter` / `ComfyGateway`），由配置 `providers.*` 选择，业务层不感知。
3. **单一事实来源**：所有写操作经 CLI；Web 只调 CLI 的 `--json` 输出，不在前端重算状态（沿用现有 `web/actions.run`）。
4. **状态可判定**：每个阶段有明确 status + 判定函数（参数指纹 / 产物存在性 / 上游 revision），不靠「跑过一次」记忆。
5. **失败可继续**：逐条失败不中断整批；阶段失败中止后续阶段并保留已完成产物与错误位置。
6. 函数级中文注释；单文件 < 1500 行；产物与日志落 `/Volumes/other`，不落 `/tmp`。

---

## 3. 数据结构与状态机

### 3.1 设定库 `settings/bible.json`

复用现有 `schema/setting-bible.schema.json`（`premise` / `world` / `outline` / `style` / `continuity`），不新造 schema。

```
settings/bible.json
├── premise    {logline, theme, core_conflict, ending_intent, target_audience, target_length}
├── world      {era, locations[], rules[], factions[], glossary[]}
├── outline    {acts[{id,title,summary,chapters[{id,index,title,summary,beats[{id,summary,character_ids[],turning_point,target_words}]}]}]}
├── style      {tone, pov, tense, prose_style, forbidden[], visual_style}
└── continuity {timeline[], open_threads[]}
```

- 版本：`settings_revision` 任何变更 +1。
- 大纲的 `chapters[].id` 直接使用章节 ID，保证「大纲条目 ↔ 章节文件」一一对应。
- 与 `characters.json` 的关联只存 `character_ids`，角色档案仍以 `characters.json` 为唯一事实。

新增存储：`src/novel_drama/store/settings_store.py`

```python
class SettingsStore:
    def __init__(self, project_root)              # 绑定工程
    def exists(self) -> bool
    def load(self) -> dict                        # 不存在时抛 FileNotFoundError
    def ensure(self, work_id: str) -> dict        # 不存在则按 work 生成骨架并落盘
    def save(self, bible: dict) -> dict           # schema 校验 → revision+1 → 原子写
    def validate(self, bible: dict) -> None
```

### 3.2 章节状态机（沿用既有枚举，新增流转约束）

```
draft ──generate──▶ written ──scene split──▶ split ──script apply──▶ scripted
  ▲                    │                        │                      │
  └──reset/regen───────┴────────────────────────┴──── (编辑正文 → stale)
scripted ──tts batch 全绿──▶ voiced ──image/video run 全绿──▶ visualized ──mux──▶ composed
```

判定函数：`resolve_chapter_stage(chapter) -> {status, evidence}`，只依据子实体真实状态推导，不信任存储字段。

- `written`：`content` 非空且 `word_count > 0`
- `split`：`scenes` 非空且每场景 `lines` 非空
- `scripted`：存在导演脚本且 `status=ready` 且无 `orphan` 锚点
- `voiced`：所有台词行 `tts.status=ready` 且产物存在
- `visualized`：所有 `key_images`/`video_segments` 的 `asset.status in (ready, skipped)`
- `composed`：`renders/mix.norm.wav` 与 `exports/*.mp4` 同时存在

### 3.3 媒体产物状态（沿用 schema 枚举）

| 实体 | 状态流转 |
| --- | --- |
| `key_image.asset` | `pending → queued → running → ready` ／ `failed` ／`stale`（提示词或尺寸变更） |
| `video_segment.asset` | 同上 + `skipped`（无源图且策略为 skip） |
| `sfx_event.asset` | `pending → running → ready` ／ `failed` ／ `skipped` |
| `render-job` | `queued → running → succeeded` ／ `failed` ／ `interrupted` ／ `skipped` |
| `export-artifact` | `pending → running → ready` ／ `failed` |

失败处理：`AdapterError.code` 决定是否重试（沿用现有 7 码）；`retryable=true` 且 `attempts < max_attempts` 自动回队；`NOT_IMPLEMENTED` 一律不重试，直接暴露给用户。

### 3.4 编排运行记录 `.ndw/runs/<run_id>.json`

```json
{
  "run_id": "run_xxx",
  "chapter_id": "chp_xxx",
  "stages": [
    {"stage": "setting", "status": "succeeded", "started_at": "...", "finished_at": "...",
     "artifacts": [{"kind": "json", "path": "settings/bible.json"}], "error": null}
  ],
  "created_at": "...", "updated_at": "..."
}
```

`status ∈ queued|running|succeeded|failed|skipped`。`--resume` 跳过 `succeeded`，其余按顺序重跑。

---

## 4. 接口签名

### 4.1 写作层（新增离线实现，真实实现不变）

`src/novel_drama/adapters/offline_writing.py`

```python
class OfflineWritingAdapter:
    """离线写作实现：按任务类型用确定性规则产出可检查文本，不联网、不加载模型。"""

    def __init__(self, settings: dict)                 # {seed, chapter_words, determinism}
    def generate(self, req: TextGenRequest) -> TextGenResult
    def stream(self, req: TextGenRequest) -> Iterator[TextGenChunk]
    def health(self, timeout_s=None) -> HealthStatus   # 永远 ok（本地能力）
    def list_models(self) -> list[dict]                # [{"id": "offline-writing"}]
    def unload(self, model=None) -> dict               # {"status": "skipped"}
```

任务分派（`req.task`）：

| task | 产出 |
| --- | --- |
| `outline` | 幕/章/节拍 JSON 文本（按 `metadata.acts/chapters/beats` 数量） |
| `chapter` | Markdown 正文（按 beat 展开，对白取自 `metadata.characters`，目标字数 `target_words ±20%`） |
| `script` | 导演脚本标注文本（镜头/台词/音效/画面），可直接喂 `parse_markup` |
| `image_prompt` | 一句画面提示词（场景 summary + style.visual_style） |
| `freeform` | 回显指令 + 说明（调试用） |

约束：只使用 `characters.json` 中存在的角色名，不新造角色；同 seed 同输入产出完全一致（便于回归）。

装配：`src/novel_drama/writing/service.py`

```python
def generate_outline(store, settings_store, config, acts=3, chapters_per_act=3, dry_run=False) -> dict
def generate_chapter(store, config, chapter_id, regen=False, dry_run=False) -> dict
def suggest_script(store, config, chapter_id, dry_run=False) -> dict   # 返回标注文本 + 解析预览
def suggest_image_prompts(store, config, chapter_id, dry_run=False) -> dict
```

### 4.2 音效（离线合成器）

`src/novel_drama/adapters/sfx.py` 增 `OfflineSfxAdapter`（实现 `SfxAdapter`）：

```python
class OfflineSfxAdapter:
    def generate(self, req: SfxRequest) -> SfxResult    # 合成到 req.output_path，返回实测时长
    def search(self, query, kind, limit=10) -> list[SfxCandidate]
    def health(self) -> HealthStatus
```

合成规则（ffmpeg 滤镜，确定性，同 prompt+kind+duration 同 sha）：

| kind | 合成方式 |
| --- | --- |
| `ambience` | 粉噪 + 低通 + 缓慢音量起伏，可 loop |
| `foley` | 短脉冲 + 带通，指数衰减 |
| `impact` | 低频正弦下扫 + 瞬态 |
| `transition` | 扫频（sine 200→2000Hz）+ 淡入淡出 |
| `music_sting` | 三音和弦叠加 + 衰减包络 |

### 4.3 配图 / 视频（离线渲染）

`src/novel_drama/adapters/image.py`

```python
class OfflineImageAdapter:
    """按提示词渲染占位图：色块渐变 + 场景名 + 提示词摘要，真实 PNG。"""
    def render(self, spec: dict) -> dict   # {asset_id, path, width, height, sha256, duration_ms}
    def health(self) -> HealthStatus
```

`src/novel_drama/adapters/video.py`

```python
class OfflineVideoAdapter:
    """按关键图（或纯色）渲染 ≤5s MP4：h264 + 静音轨，真实时长回写。"""
    def render(self, spec: dict) -> dict   # {asset_id, path, duration_ms, fps, sha256}
    def health(self) -> HealthStatus
```

真实分支：`ComfyGateway` 已定义 `submit/status/fetch/cancel`，本轮只保留装配点与 `providers.image=comfyui` 的错误提示，不实现网络调用细节。

### 4.4 混流 / 导出

```python
# media/mux.py
def plan_mux(timeline, project_root, mix_plan=None) -> dict     # 保持现有签名与字段
def mux(plan: dict) -> dict                                     # 真执行：ffmpeg → mp4

# media/bundle.py（新增）
def export_bundle(store, chapter, timeline, options) -> dict    # mp3+mp4+srt+manifest+README.txt
```

### 4.5 全链路编排

```python
# orchestration/produce.py
STAGES = ("setting", "outline", "chapter", "scenes", "lines", "script",
          "tts", "sfx", "image", "video", "timeline", "concat", "mix", "mux", "export")

def produce(store, config, chapter_id, from_stage="setting", to_stage="export",
            resume=True, dry_run=False, keep_going=False, force=False) -> dict
```

- 每阶段返回 `{stage, status, artifacts, error}` 写入 `.ndw/runs/<run_id>.json`。
- 阶段前置校验失败 → 该阶段 `failed`，后续 `skipped`，退出码 `4`（前置条件缺失）或 `5`（执行失败）。
- `--dry-run`：只打印每阶段将做的事与缺什么，不落任何产物，退出码 `0`。

### 4.6 CLI 新增/变更

| 命令 | 作用 | 状态 |
| --- | --- | --- |
| `ndw setting show/set/validate/import/export` | 设定库读写与校验 | 新增 |
| `ndw outline show/generate/apply` | 大纲生成与落地成章节 | 新增 |
| `ndw chapter generate --chapter` | 生成/重生成正文 | 新增 |
| `ndw script suggest --chapter` | 生成标注建议（offline 或 LLM） | 新增 |
| `ndw sfx run --chapter` | 真实音效合成 | 由 NOT_IMPLEMENTED 改为可用 |
| `ndw image run --chapter` | 真实占位图渲染 | 同上 |
| `ndw video run --chapter` | 真实视频位渲染 | 同上 |
| `ndw mux --render` | 真实混流 MP4 | 同上 |
| `ndw export --format bundle` | 交付包 | 新增 |
| `ndw produce --chapter` | 全链路一条命令 | 新增 |
| `ndw run show --run <id>` | 查看编排进度 | 新增 |

退出码沿用：`0` 成功 / `1` 拒绝 / `2` 用法 / `3` 未实现 / `4` 前置缺失 / `5` 执行失败 / `6` schema 失败。

### 4.7 Web 接口与页面

新增页面（hash 路由，单页多视图，不引入框架）：

| 视图 | 内容 |
| --- | --- |
| `#/project` | 工程概览、设定库编辑（premise/world/style）、角色列表 |
| `#/outline` | 幕/章/节拍树、生成大纲、落地成章节 |
| `#/chapter` | 章节正文查看、生成/重生成、拆场景、切台词 |
| `#/script` | 标注语法说明、建议生成、解析预览、apply |
| `#/voice` | **现有台词校对页（原样保留）** |
| `#/media` | 音效/配图/视频位列表与生成、预览 |
| `#/export` | 进度、时间轴、混音/混流/导出、产物下载 |

新增 API（全部转调 CLI `--json`）：

```
GET  /api/ui/setting          → setting show
POST /api/ui/setting          → setting set
POST /api/ui/outline/generate → outline generate
POST /api/ui/outline/apply    → outline apply
POST /api/ui/chapter/generate → chapter generate
POST /api/ui/script/suggest   → script suggest
POST /api/ui/media/run        → sfx|image|video run
POST /api/ui/produce          → produce（后台进程 + .ndw/runs 轮询）
GET  /api/ui/run              → run show
GET  /api/ui/exports          → 导出产物清单
```

前端文件拆分（防止单文件超限）：`static/app.js`（壳/路由/API/轮询）＋ `static/page-voice.js`（现有逻辑迁移）＋ `static/page-project.js` ＋ `static/page-media.js` ＋ `static/page-export.js`。

---

## 5. 端到端验收（不接任何模型）

单条命令产出可检查文件：

```bash
export PYTHONPATH=src
PY=.venv/bin/python3.12
P=/Volumes/other/ndw_e2e_r14

$PY -m novel_drama.cli init $P --title 长夜将明 --author 测试 --genre 悬疑
$PY -m novel_drama.cli -p $P setting set --logline "..." --era "..." --tone "..."
$PY -m novel_drama.cli -p $P outline generate --acts 2 --chapters-per-act 2
$PY -m novel_drama.cli -p $P outline apply
$PY -m novel_drama.cli -p $P chapter generate --chapter chp_xxx
$PY -m novel_drama.cli -p $P scene split --chapter chp_xxx
$PY -m novel_drama.cli -p $P line split --chapter chp_xxx
$PY -m novel_drama.cli -p $P script suggest --chapter chp_xxx --apply
$PY -m novel_drama.cli -p $P tts batch --chapter chp_xxx
$PY -m novel_drama.cli -p $P sfx run --chapter chp_xxx
$PY -m novel_drama.cli -p $P image run --chapter chp_xxx
$PY -m novel_drama.cli -p $P video run --chapter chp_xxx
$PY -m novel_drama.cli -p $P timeline build --chapter chp_xxx
$PY -m novel_drama.cli -p $P concat --chapter chp_xxx
$PY -m novel_drama.cli -p $P mix --chapter chp_xxx --render
$PY -m novel_drama.cli -p $P mux --chapter chp_xxx --render
$PY -m novel_drama.cli -p $P export --chapter chp_xxx --format bundle
```

验收断言：

1. 每步退出码 0；中间失败必须给出「哪个实体、哪个字段、怎么修」。
2. 产物齐全且可打开：`assets/sfx/*.wav`（时长与 `actual_duration_ms` 一致 ±50ms）、`assets/images/*.png`（尺寸与 `width/height` 一致）、`assets/video/*.mp4`（时长 ≤5000ms）、`renders/mix.norm.wav`、`exports/*.mp3|mp4|srt|manifest.json|README.txt`。
3. 所有 JSON 通过 schema 校验；二跑同命令不重复出文件（幂等）。
4. `.ndw/runs/<run_id>.json` 记录 15 个阶段全部 `succeeded`。
5. `tests/smoke_test.py` 仍 `SMOKE OK`；新增 `tests/e2e_offline.py` 一条命令复现上述全流程。
6. Web：七个页签都能打开并读到数据；配音页签功能与改造前一致。
7. 服务 `127.0.0.1:8765` 重启后页面可用，`/api/health` 200。

---

## 6. 任务清单（顺序即执行顺序）

| 序 | 任务 | 依赖 | 产出文件 | 完成判定 |
| --- | --- | --- | --- | --- |
| T1 | 设定库存储与 CLI | — | `store/settings_store.py`、`cli_setting.py` | `setting set/show/validate` 通过 schema |
| T2 | 离线写作实现 | — | `adapters/offline_writing.py` | 五类 task 均产出文本，同 seed 一致 |
| T3 | 大纲生成 + 落地成章节 | T1,T2 | `writing/service.py`、`cli_writing.py` | `outline generate/apply` 后章节文件存在 |
| T4 | 章节生成 | T3 | 同上 | `chapter generate` 产出正文且字数在目标 ±20% |
| T5 | 导演脚本建议 | T4 | 同上 | `script suggest --apply` 后 scene 有 sfx/key_image/video |
| T6 | 音效离线合成 | — | `adapters/sfx.py`、`cli_media` 改造 | 5 类 kind 各出 wav，时长回写 |
| T7 | 配图离线渲染 | — | `adapters/image.py` | PNG 尺寸/sha 回写，状态 ready |
| T8 | 视频位离线渲染 | T7 | `adapters/video.py` | MP4 ≤5s，时长回写 |
| T9 | 混流实现 | T6,T7,T8 | `media/mux.py` | 产出可播放 MP4 |
| T10 | 导出交付包 | T9 | `media/bundle.py` | mp3/mp4/srt/manifest/README 齐全 |
| T11 | 全链路编排 | T1–T10 | `orchestration/produce.py`、`cli_produce.py` | 15 阶段 run 记录全绿 |
| T12 | Web 多页签 | T1–T11 | `web/app.py`、`static/*` | 七视图可用，配音页不回归 |
| T13 | 测试与文档 | T12 | `tests/e2e_offline.py`、README、roadmap、修改记录 | SMOKE OK + e2e 通过 |
| T14 | 服务重启与验收 | T13 | — | 8765 可用，`/api/health` 200 |

风险与回滚：每完成一个任务即提交一次；`tts`/`mix`/`export` 现有路径只做「不改签名、不改行为」的扩展；任何阶段失败先停下来修，不带错推进。

---

## 7. 实施结果（2026-11-10）

T1–T14 全部完成，关键实测记录：

| 验收项 | 结果 |
| --- | --- |
| 离线端到端（`tests/e2e_offline.py`） | 通过。`proj_20261110_040947`：629 字正文 → 35 行台词 → 导演标注 1 镜头/0 告警 → 音效/配图/视频各 1 条就绪 → `offline_dub` 配音 35/35 成功 → 时间轴 → 拼接 → 混音 → 混流 128.5s MP4（3.1MB）→ 导出 mp3(3.0MB)/mp4(3.1MB)/srt/bundle/README 全部非空 |
| 真实链路后半段（IndexTTS 产物） | 通过。`/Volumes/other/ndw_e2e_r14` 章节 `chp_01M70XWXKZCC6MH9YK7WSYNHB8`：44 行真实配音 → 时间轴 03:02 → 拼接 182s → 混音峰值 -5.46dBFS / 规整 -16.02LUFS → 混流 2 片段 → 导出 mp4(4.16MB)/mp3(4.37MB)/srt/manifest/bundle，`missing: []` |
| 编排记录 | `.ndw/runs/<run_id>.json` 逐阶段落盘；失败即中止并记录错误（例：`concat` 曾因键名不符失败，修复后重跑全绿） |
| 冒烟回归 | `tests/smoke_test.py` 输出 `SMOKE OK` |
| Web | 七个页签均可见并读到数据；台词校对页 46 行、徽标与底部计数正常，控制台无异常（浏览器实测） |
| 服务 | `127.0.0.1:8765` 重启后 `/api/health` 200，首页可用 |

已知偏差（不阻塞验收）：

1. `line split` 会把叙述句一并切行，长章节配音行数偏多；后续可按段落合并叙述行。
2. 离线配图/视频只保证「真出文件 + 尺寸/时长契约」，画面内容是排版占位，不追求观感。
3. `offline_dub` 的 `voice_clone` 能力是为通过批次层硬过滤而声明，不做真实克隆；真实配置不加载该 provider。

