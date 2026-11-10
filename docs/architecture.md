# 架构设计

## 1. 设计原则

1. **文本是唯一真相**：音频、图、视频、时间轴、成片都是从工程数据派生的可重建产物。
2. **接口先于实现**：写作 LLM、TTS、音效、ComfyUI 四类外部能力全部收口到「适配层」，上层只依赖统一接口。本轮所有实现为 `NotImplemented` 占位 + 明确的对接约定。
3. **单卡串行**：文本生成（omlx）、TTS、音效、图像、视频全部排队，同一时刻只有一个 GPU 任务，并与 ComfyUI 共用同一把锁。
4. **增量与幂等**：以 `revision + 参数哈希` 生成幂等键，输入不变绝不重跑。
5. **失败可降级**：单行/单图/单音效失败不阻塞整章，按明确降级路径出片（视频 → 静态图，音效 → 静音，TTS → 报错阻塞）。
6. **不做模型管理**：不下载、不管理权重、不维护模型清单。写作交给用户本机 omlx，图像视频交给用户本机 ComfyUI。

## 2. 分层与依赖方向

```
┌──────────────────────────────────────────────────────────────┐
│ 展示层 presentation   web(FastAPI) + 静态前端 + CLI(ndw)      │
├──────────────────────────────────────────────────────────────┤
│ 任务编排层 orchestration  串行 GPU 队列 / 流水线 / 重试 / 断点 │
├──────────────────────────────────────────────────────────────┤
│ 模型适配层 adapters   writing(omlx) / tts / sfx / comfyui     │
├──────────────────────────────────────────────────────────────┤
│ 媒体合成层 media  时间轴构建 / 混音 / ffmpeg 封装 / 字幕      │
├──────────────────────────────────────────────────────────────┤
│ 工程数据层 models + store  实体定义 / JSON 读写 / schema 校验 │
└──────────────────────────────────────────────────────────────┘
```

依赖只允许**自上而下**：展示层 → 编排层 → 适配层 → 媒体层 → 数据层。反向依赖通过回调/事件（如任务状态变更）传递，禁止数据层 import 适配层。

| 层 | 目录 | 职责 | 禁止 |
| --- | --- | --- | --- |
| 工程数据层 | `src/novel_drama/models/`、`store/`、`schema/` | 实体构造器（dict + 工厂函数）、JSON 读写、校验、ID 生成、版本与迁移 | 不发网络请求、不调 ffmpeg |
| 媒体合成层 | `src/novel_drama/media/` | 时间轴计算、混音/合成命令生成与执行、字幕生成 | 不直接调模型 |
| 适配层 | `src/novel_drama/adapters/` | 统一接口与各实现；错误码归一 | 不写工程文件（只返回产物路径）、不做编排 |
| 编排层 | `src/novel_drama/orchestration/` | 队列、任务生命周期、幂等、重试、断点续跑、流水线步骤 | 不拼 ffmpeg 命令、不解析模型响应细节 |
| 展示层 | `src/novel_drama/web/`、`cli.py` | HTTP API 与 CLI，调用编排层与数据层 | 不含业务规则（规则下沉到编排/数据层） |

## 3. 模型适配层接口

### 3.1 通用错误契约

所有适配器只抛 `AdapterError`，由编排层统一映射为任务状态与用户提示。

```python
class ErrorCode(str, Enum):
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"   # 本轮占位，未对接
    UNAVAILABLE     = "UNAVAILABLE"       # 服务未启动/连不上
    TIMEOUT         = "TIMEOUT"           # 单次调用或整体任务超时
    RATE_LIMITED    = "RATE_LIMITED"      # 上游限流
    INVALID_INPUT   = "INVALID_INPUT"     # 参数非法（如视频 > 5 秒、首帧缺失）
    UPSTREAM_ERROR  = "UPSTREAM_ERROR"    # 上游返回错误（含 ComfyUI 节点报错）
    CONTENT_BLOCKED = "CONTENT_BLOCKED"   # 内容被上游拒绝

class AdapterError(Exception):
    code: ErrorCode
    message: str          # 面向用户的中文提示，必须可操作
    retryable: bool       # 由适配器判定；编排层据此决定重试
    detail: dict          # 原始响应/日志片段，落日志不直出界面
```

**约定**：

- `retryable=True` 仅用于 `UNAVAILABLE` / `TIMEOUT` / `RATE_LIMITED` / 部分 `UPSTREAM_ERROR`；`INVALID_INPUT` 与 `NOT_IMPLEMENTED` 永不自动重试。
- 适配器不吞异常、不做静默降级；降级策略属于编排层。
- 适配器不修改工程数据，只返回 `AssetRef`（相对路径 + 元数据），由编排层写回实体。

### 3.2 写作模型适配（omlx）

**选型已定**：写作 LLM 使用用户本机已有的 **omlx**，不做模型选型、不做权重下载与本地模型管理。omlx 的对外调用形态（命令行 / 本地 HTTP / OpenAI 兼容）由另一条线确认，适配器按「可切协议」设计，确认后只补一个协议实现，不动上层。

```python
@dataclass
class TextGenRequest:
    task: str                  # outline|chapter|scene|dialogue_split|emotion_tag|image_prompt|freeform
    system: str                # 角色设定 + 文风约束
    messages: list[Message]    # [{role: system|user|assistant, content: str}]
    max_tokens: int = 2048
    temperature: float = 0.8
    stop: list[str] = field(default_factory=list)
    stream: bool = True
    context_budget_tokens: int = 32768   # 由 ContextAssembler 保证不超
    metadata: dict = field(default_factory=dict)

@dataclass
class TextGenChunk:
    delta: str
    done: bool
    usage: Usage | None = None           # prompt_tokens/completion_tokens

@dataclass
class TextGenResult:
    text: str
    finish_reason: str                   # stop|length|error
    usage: Usage | None
    model: str
    raw: dict | None

class WritingAdapter(Protocol):
    def generate(self, req: TextGenRequest) -> TextGenResult: ...
    def stream(self, req: TextGenRequest) -> Iterator[TextGenChunk]: ...
    def health(self) -> HealthStatus: ...
```

**统一入口规则**：章节续写、大纲生成、场景拆分、台词切分、情绪标注、图像/视频提示词生成等**所有文本生成步骤都走同一个 `WritingAdapter`**，仅通过 `task` 与 `system` 区分，不按任务接不同模型。

**协议实现（待确认后回填）**：

| 实现 | 触发条件 | 说明 |
| --- | --- | --- |
| `OmlxHttpAdapter` | 配置 `endpoint` 非空 | 本地 HTTP；若为 OpenAI 兼容则直接复用 `/v1/chat/completions`，否则按确认的私有协议实现 |
| `OmlxCliAdapter` | 配置 `cli_path` 非空 | 以子进程方式调用，stdin 传 prompt / 参数传文件，stdout 流式读文本 |

两个实现共用 `TextGenRequest/Result` 与错误映射；切换只改 `settings.writing`。缺失协议时抛 `NOT_IMPLEMENTED` 并提示「omlx 协议待确认」。

**流式与停止**：`stream()` 逐块产出，CLI 与页面可直接边生成边显示；`stop` 由适配器透传给底层；`finish_reason=length` 时上层必须提示「被截断，建议分段」。

### 3.3 TTS 适配

```python
@dataclass
class TtsRequest:
    text: str
    voice_id: str
    emotion: str = "neutral"
    emotion_intensity: float = 0.5
    speed: float = 1.0
    pause_before_ms: int = 0
    pause_after_ms: int = 0
    sample_rate: int = 48000
    format: str = "wav"
    reference_audio: str | None = None   # 声音克隆用；不支持的引擎忽略并告警
    seed: int | None = None
    delivery_note: str | None = None
    output_path: str | None = None

@dataclass
class TtsResult:
    audio_path: str
    duration_ms: int          # 必须为实测时长（读 wav 头/解码得到）
    sample_rate: int
    channels: int
    voice_id: str
    model: str
    raw: dict | None = None

class TtsAdapter(Protocol):
    def list_voices(self) -> list[Voice]: ...          # 供角色音色库选择
    def synthesize(self, req: TtsRequest) -> TtsResult: ...
    def health(self) -> HealthStatus: ...
```

**约定**：必须以**单行**为单位合成（不做整章拼接后切分）；`duration_ms` 必须实测，禁止返回估算值；不支持情绪时按 `emotion_map` 映射为空操作但必须保留字段；产物写入 `assets/voice/<lineId>_r<rev>.wav` 由编排层决定路径，适配器按传入 `output_path` 写。

### 3.4 音效适配

音效有两种来源，接口统一为「描述进、音频出」，来源差异由实现隐藏：

```python
@dataclass
class SfxRequest:
    prompt: str
    kind: str                    # ambience|foley|impact|transition|music_sting
    duration_ms: int
    loop: bool = False
    seed: int | None = None
    output_path: str | None = None

@dataclass
class SfxResult:
    audio_path: str
    duration_ms: int
    sample_rate: int
    channels: int
    provider: str
    cache_key: str | None = None   # 素材库命中的稳定键，便于复用

class SfxAdapter(Protocol):
    def generate(self, req: SfxRequest) -> SfxResult: ...
    def search(self, query: str, kind: str, limit: int = 10) -> list[SfxCandidate]: ...  # 不支持则抛 NOT_IMPLEMENTED
    def health(self) -> HealthStatus: ...
```

**约定**：返回时长可短于请求（由时间轴决定循环或补静音）；`cache_key` 相同的请求允许直接复用既有产物，不重复生成。

### 3.5 ComfyUI 网关

图像与视频**全部**通过本机 ComfyUI 的 HTTP 接口完成，工作台不自带图像/视频模型。网关只做「填参 → 提交 → 轮询 → 取产物」，不理解节点语义。

```python
@dataclass
class ComfySubmitResult:
    prompt_id: str
    client_id: str
    number: int | None            # ComfyUI 队列序号

@dataclass
class ComfyJobStatus:
    state: str                    # pending|running|success|error|canceled
    progress: float | None        # 0~1
    current_node: str | None
    message: str | None
    error: dict | None

@dataclass
class ComfyArtifact:
    kind: str                     # image|video|audio|other
    filename: str
    subfolder: str
    server_path: str
    local_path: str | None

class ComfyGateway(Protocol):
    def health(self) -> HealthStatus: ...                       # GET /system_stats
    def list_templates(self) -> list[WorkflowTemplate]: ...      # 读本地模板目录
    def submit(self, template_id: str, params: dict, client_id: str) -> ComfySubmitResult: ...
    def status(self, prompt_id: str) -> ComfyJobStatus: ...      # GET /history + /queue
    def fetch(self, prompt_id: str, dest_dir: str) -> list[ComfyArtifact]: ...  # GET /view
    def cancel(self, prompt_id: str) -> None: ...                # POST /queue {"delete":[...]}
    def interrupt(self) -> None: ...                             # POST /interrupt
    def queue_snapshot(self) -> dict: ...                        # GET /queue
```

**对接约定**：

- 工作流模板以 **API 格式 JSON** 放在 `config/comfy/<template_id>.json`，并在 `config/comfy/manifest.json` 登记；工程已内置三个加速模板，契约见 §10。
- 模板内用 `{{param_name}}` 占位，只允许 `prompt / negative_prompt / width / height / seed / filename_prefix / input_image` 七个名字（见 §10.3），替换失败即 `INVALID_INPUT`，禁止猜测节点 ID。
- 提交前必须 `health()`；连不上时报 `UNAVAILABLE` 并提示「检查 ComfyUI 是否启动、地址是否正确」。
- 轮询间隔与超时来自 `settings.comfy`；状态查询必须用短超时（≤10s），避免 ComfyUI 无响应时长时间阻塞。
- 产物取回后写入工程 `assets/`，文件名由编排层生成；ComfyUI 的 `output/` 目录不作为工程引用源。
- 视频类模板的时长上限由上层校验（`duration_ms ≤ 5000`），网关不重复校验但必须原样传递。

### 3.6 适配器选型与装配

```yaml
# config/config.yaml（示例，实际以 config/config.example.yaml 为准）
adapters:
  writing: omlx
  tts: placeholder
  sfx: placeholder
  comfyui: local
```

装配在 `adapters/__init__.py` 的 `build_adapters(config)` 完成；未知名称直接报错，**不做静默回退**。任何适配器缺失时，依赖它的流水线步骤在「执行」阶段报 `NOT_IMPLEMENTED`，但「规划/预览」阶段仍可用（`--dry-run`）。

## 4. 串行调度：队列、断点、重试、幂等

### 4.1 为什么必须串行

- 单卡内存有限，TTS、图像、视频同时跑会 OOM；
- ComfyUI 自身有队列，工作台的视频/图像任务必须与之互斥；
- **omlx 文本生成同样占用 GPU**，因此写作类任务与图像/视频/TTS 任务共用同一条队列，不能并行。

### 4.2 队列模型

- 持久化：`.ndw/queue.json`（快照）+ `.ndw/logs/<jobId>.log`（日志）。
- 排序：`priority` 升序 → `created_at` 升序；同幂等键只保留一条。
- 状态：`queued → running → succeeded | failed | canceled | interrupted`。
- 单 worker：`orchestration/gpu_queue.py` 内的 `GpuQueue.run_once()` 取一条任务，抢锁后执行，结束释放。
- 优先级约定：`writing`/`tts` = 100，`sfx` = 120，`image` = 140，`video` = 160，`mix`/`export` = 200（CPU 任务，仍走队列以便统一状态与重试）。

### 4.3 跨进程锁（与 ComfyUI 互斥）

```
acquire: open(lock_path, O_CREAT|O_EXCL) → 写 {pid, job_id, started_at} → 成功
        已存在则读取 pid 判断是否存活；存活 → 等待；僵死 → 记录并接管
release: 删除锁文件（仅当内容 pid 与自己一致）
```

- 默认锁路径 `/Volumes/other/gpu.lock`，可配置。
- 抢锁范围：任务整个执行期（含 ComfyUI 提交→终态轮询），确保不与外部 ComfyUI 任务抢卡。
- 启动前先查询 ComfyUI `/queue`；有运行中任务时等待而非中断。

### 4.4 断点续跑

- 每个任务执行前记录 `started_at` 与 `attempts`；进程退出时仍为 `running` 的任务，下次启动统一标记 `interrupted`。
- `interrupted` 任务重跑时先检查产物是否存在（按幂等键 + revision 命名）：
  - 产物完整 → 直接 `succeeded`（不烧 GPU）；
  - 产物缺失/不完整（大小 0 或校验失败）→ 删除残留后重跑。
- ComfyUI 任务额外记录 `prompt_id`：重启后可用 `/history/{prompt_id}` 查询，若已完成则直接取产物，避免重复生成。

### 4.5 失败重试

| 错误码 | 自动重试 | 策略 |
| --- | --- | --- |
| `UNAVAILABLE` | 是 | 5s、20s、60s 三次退避 |
| `TIMEOUT` | 是 | 同上；超过 `max_attempts` 转人工 |
| `RATE_LIMITED` | 是 | 尊重上游 `retry_after`，否则 30s |
| `UPSTREAM_ERROR` | 视 `retryable` | ComfyUI 节点错误默认不自动重试（同参数重跑结果一致） |
| `INVALID_INPUT` | 否 | 直接失败并提示改参数 |
| `NOT_IMPLEMENTED` | 否 | 直接失败并提示「适配器未对接」 |
| `CONTENT_BLOCKED` | 否 | 直接失败，提示修改提示词 |

重试不重置 `attempts` 以外的数据；超过上限的任务保持 `failed` 并保留最后一次错误。

### 4.6 幂等

- 幂等键：`sha256(kind | target_ids | input_revision | adapter | params_hash)`。
- 入队时若存在同键任务：`queued/running` → 复用（不新增）；`succeeded` → 直接返回既有产物；`failed` → 允许重新入队（沿用或重置 attempts 由调用方决定）。
- 产物路径含 revision，天然避免「同路径不同内容」的覆盖问题。

## 5. 长文本处理

### 5.1 分层切分策略

```
作品 → 章节（按标题/字数上限）→ 场景（按叙事单元）→ 台词行（按说话人/旁白边界）
```

| 层级 | 切分依据 | 产出 | 是否用模型 |
| --- | --- | --- | --- |
| 章节 | 用户已有分章，或按标题正则/字数上限（默认 5000 字）硬切 | `chapter[]` | 否（导入时可选模型校验） |
| 场景 | 规则优先：空行、地点/时间标注、对话块连续长度、「数日后/与此同时」等转场词；超长再交模型细分 | `scene[]` | 混合 |
| 台词行 | 引号内为对白，引号外叙述为旁白；`他说/她问` 归旁白并用于反推说话人；内心独白按 `（）` 或「心想」识别 | `line[]` | 混合 |

**规则优先的原因**：切分必须可复现、可解释；模型只处理规则无法覆盖的歧义段落（如无引号的对白、多人抢话），且只替换该段，不重排全文。

### 5.2 上下文装配（`text/context.py`）

每次文本生成调用按固定顺序装配，受 `context_budget_tokens`（默认 32768，中文 1 字 ≈ 1 token 粗算）约束。装配结果写入 `TextGenRequest.system`（设定层）与 `messages`（任务层），真实 LLM 适配器会完整发送；`metadata` 继续供离线适配器使用，保证其确定性。

```
system:
[1] 故事圣经（作品设定 + 文风 + 主要角色摘要）              —— 永不裁剪
[2] 世界设定（本章绑定世界：时代/概述/规则/势力/地点/术语；未绑定回退设定库摘要）
[3] 角色卡（POV 与节拍涉及角色：性格/背景/说话方式/关系备注；排除旁白）

user:
[4] 前情摘要（前 N 章，默认 3，按 index 从旧到新；无 synopsis 回退正文末尾 800 字）
[5] 本章大纲与节拍（摘要、逐条节拍含目标字数与转折点、视角与基调）
[6] 前文摘录（上一章结尾 / 本章已有正文结尾，重写场景）
[7] 本次任务指令（目标字数、格式约束）                      —— 永不裁剪
```

- 裁剪顺序：[6] 前文摘录 → [4] 前情摘要（删最旧）→ [2] 世界细节（先地点/术语，再规则/势力）→ [3] 角色卡细节（先背景，再性格/说话方式）；[1] 与 [7] 永不裁剪，仍超限时记入 `ContextAssembler.warnings`。
- 摘要链：前情优先使用 `chapter.synopsis`；缺失时回退该章正文末尾 800 字。
- 一致性：角色名、称呼、性格、说话方式只从 `characters.json` 取，不接受模型临时编造；切分结果里的新角色必须进入「待确认角色」列表，不自动落库。
- 正文写完后由 `writing/summarize.py` 反向总结 `chapter.title/synopsis/outline` 并同步设定库节拍，保证目录、大纲与正文一致。

### 5.3 会话与重放

- 每次调用记录 `task`、装配后的 prompt 哈希、模型标识、token 用量到任务日志，便于复现与排查。
- 不保存对话历史用于跨任务拼接（避免上下文漂移）；跨任务一致性只依赖故事圣经 + 摘要链。

## 6. 媒体合成层

| 模块 | 职责 |
| --- | --- |
| `timeline_builder.py` | 由台词行（实测/估算时长 + 停顿）、音效位置、图/视频锚点计算各轨 `start_ms/end_ms`，输出 `Timeline` |
| `mixer.py` | 生成混音计划：语音轨拼接、音效增益/淡入淡出、视频音轨策略、响度归一化；输出 ffmpeg 参数计划 |
| `ffmpeg.py` | 封装 `ffmpeg` 调用（音频拼接、混音、图片序列转视频、字幕烧录/外挂），统一错误与进度 |
| `subtitles.py`（M4） | 由时间轴的台词行生成 SRT |

时间轴规则：

- 语音轨按行顺序累加：`start = prev_end + pause_before`，`end = start + duration`（实测优先，否则估算），行后加 `pause_after`。
- 音效按 `position` 解析为绝对时间；`loop=true` 时铺满锚定行范围。
- 图像按锚点行的 `start_ms` 出现，持续到下一个图像或场景结束（默认 3~5 秒，可配置）。
- 视频按 `start_offset_ms` 插入，替换该时间窗内的图像轨；失败则用 `fallback_image_id`。
- 视频自带音轨按 `audio_policy` 处理（`mute` 时丢弃，`duck` 时在配音期间降低 12dB）。

## 7. 目录与模块映射

```
src/novel_drama/
├── cli.py                 # CLI 入口（argparse，零额外依赖）
├── config.py              # 配置加载与校验
├── models/                # 实体构造器（dict + 工厂函数）+ 枚举 + 时长估算
├── store/                 # 工程读写、队列持久化、schema 校验
├── adapters/              # base(接口+错误) / writing(omlx) / tts / sfx / comfyui
├── orchestration/         # gpu_queue(锁+队列) / jobs(幂等+重试) / pipeline(流水线)
├── media/                 # timeline_builder / mixer / ffmpeg
├── text/                  # splitter(切分) / context(上下文装配)
└── web/                   # FastAPI 应用与路由
```

单文件上限 1500 行；超过即按实体/职责拆分。

## 8. 配置与安全

- 配置分三层：`config/config.yaml`（本机路径、适配器选择）→ 工程 `project.json`（作品级参数）→ 任务参数（单次覆盖）。
- 敏感信息（如云端 TTS 的 API Key）只放 `config/config.yaml` 或环境变量，**不写入工程、不写日志、不进 git**。
- 默认零外网：除显式启用的云端适配器外，所有请求目标为 `127.0.0.1`。
- 不执行模型/工作流文件内的任意代码；ComfyUI 模板只做字符串参数替换。

## 9. 扩展点

| 扩展 | 做法 |
| --- | --- |
| 新写作用协议（omlx 确认后） | 新增 `OmlxHttpAdapter` 或 `OmlxCliAdapter`，实现 `WritingAdapter` |
| 换 TTS 引擎 | 新增实现 + 改 `adapters.tts` 配置；数据与时间轴不变 |
| 换音效来源 | 新增实现（生成式/素材库），实现 `generate` 与可选 `search` |
| 新 ComfyUI 工作流 | 放模板 JSON + 在 `settings.comfy.templates` 注册 ID，无需改代码 |
| 新画面类型 | 扩展 `video_segment.mode` 与模板映射，时间轴按同一套锚点规则处理 |

## 10. ComfyUI 模板契约

### 10.1 来源与存放

内置模板来自本机 ComfyUI 里已经调通的 UI 工作流，经 `ndw comfy import` 转成 API 格式后入库，不再依赖用户现场导出。

| 路径 | 作用 | 是否入库 |
| --- | --- | --- |
| `config/comfy/manifest.json` | 模板登记表（3 个模板） | 是 |
| `config/comfy/<template_id>.json` | 转换后的 API 格式工作流 | 是 |
| `config/comfy/source/` | 最初从 ComfyUI 导出的 UI 工作流，仅作来源追溯 | 是 |
| `config/comfy/models.json` | 模板依赖模型清单（体积、许可、引用关系） | 是 |
| `config/comfy/models.local.json` | 本机反查到的线上地址缓存 | 否（`.gitignore`） |

三个内置模板：`qwen_image_accel`（关键图）、`minimax_h3_accel`（i2v）、`minimax_h3_accel_t2v`（t2v，由 i2v 移除 `LoadImage` 与 `first_frame` 连线派生）。`media/comfy_media.py` 对模板与清单**只读**，任何时候都不改写 `config/comfy/`。

### 10.2 UI → API 转换

ComfyUI 保存的是画布格式（`nodes` + `links` + 坐标 + `widgets_values`），`/prompt` 要的是 `{"<节点ID>": {"class_type", "inputs"}}`。`media/comfy_workflow.py` 做一次性转换：

1. 向运行中的 ComfyUI 请求 `/object_info/<class_type>`，按 `required` + `optional` 的声明顺序筛出控件型输入（`INT/FLOAT/STRING/BOOLEAN/COMBO/MULTISELECT`），建立「`widgets_values` 顺序 → 输入名」映射；
2. UI 节点自带的 `inputs[].widget.name` 更接近前端实际渲染顺序，存在时优先；
3. `control_after_generate`、`upload` 等前端附加值不进入 API；
4. `links` 解析为 `[上游节点ID, 输出槽位]`；
5. 跳过静音（`mode=2`）/ 旁路（`mode=4`）节点与 `MarkdownNote / Note / Reroute / PrimitiveNode` 等不可执行节点，只保留从输出节点可达的子图；
6. 转换失败抛 `ComfyWorkflowError`；`ndw comfy import` 失败时不覆盖已有模板（显式 `--force` 才覆盖）。

### 10.3 占位符白名单

| 模板 | 允许的占位符 | 必需 |
| --- | --- | --- |
| `qwen_image_accel` | `prompt` / `negative_prompt` / `width` / `height` / `seed` / `filename_prefix` | 除 `negative_prompt` 外全必需 |
| `minimax_h3_accel` | `prompt` / `width` / `height` / `seed` / `filename_prefix` / `input_image` | 全部必需 |
| `minimax_h3_accel_t2v` | `prompt` / `width` / `height` / `seed` / `filename_prefix` | 全部必需 |

- 白名单之外的名字一律拒绝；数值型占位符保持 `int`。
- 填参后仍有 `{{...}}` 残留时直接 `INVALID_INPUT`，**不发出任何请求**。
- 步数、CFG、采样器、调度器、模型、LoRA、帧率、帧数等全部固定在模板与 manifest 的 `fixed` 中，CLI 与页面都不提供修改入口。

### 10.4 manifest.json 字段

| 字段 | 说明 |
| --- | --- |
| `id` / `kind` | 模板 ID 与用途（`image` / `video`） |
| `file` | 对应的 API 模板文件名 |
| `source` / `derived_from` | 原始 UI 工作流来源；派生模板记录父模板 ID |
| `display_name` | 页面展示名 |
| `placeholders` / `required_placeholders` | 允许与必需的占位符 |
| `bindings` | 占位符 → `{node, input}`，即写入哪个节点的哪个输入 |
| `size_map` | 画幅比例 → `[宽, 高]`，是宽高的唯一来源 |
| `fixed` | 只读固定参数：`steps / cfg / sampler / scheduler / shift / length / fps / width / height / model / lora` |
| `models` | 依赖模型（相对 models 目录的路径），供扫描与下载使用 |

内置画幅桶：图像 `1:1 1328×1328`、`9:16 928×1664`、`16:9 1664×928`、`3:4 1140×1472`、`4:3 1472×1140`；视频 `9:16 512×896`、`16:9 896×512`、`1:1 768×768`。

### 10.5 模板回落与画幅解析

- **模板回落**：指定模板 ID 为空或不存在时，按用途回落到内置 ID（`image → qwen_image_accel`、`video_i2v → minimax_h3_accel`、`video_t2v → minimax_h3_accel_t2v`），并在结果中返回中文 `warning`。
- **画幅优先级**：条目 `aspect_ratio` > 工程 `settings.output.aspect_ratio` > `1:1`；再经模板 `size_map` 映射为宽高。
- **视频画幅**：只支持 `9:16 / 16:9 / 1:1`，其它比例按浮点比例距离取最接近的一档，并返回降级说明。
- 模板缺少 `size_map` 时直接 `INVALID_INPUT`，不猜测尺寸。

### 10.6 seed 与产物命名

- `seed = int(sha256("<条目ID>:<revision>")[:8], 16)`，同一 revision 可复现，不同 revision 自动变化；调用方不传 seed。
- `filename_prefix = ndw/<条目ID>_r<revision>`，路径不安全字符替换为 `_`。
- 最终产物按 `<条目ID>_r<revision>.<ext>` 落到 `assets/images` 或 `assets/video`；取回先写临时目录 `.ndw-tmp-<id>-r<rev>`，成功后再改名。
- `providers.image/video=comfyui` 时，失败只写 `asset.status=failed` 与中文错误，**不回退离线占位图**；i2v 缺首帧才降级为 t2v 并记录原因。
- 提交 → 轮询 → 取回整段包在 `orchestration/gpu_queue.py` 的 GPU 锁内，串行执行。

## 11. 模型扫描与线上地址反查

### 11.1 扫描顺序

`media/comfy_models.resolve_models_dir` 依次尝试：

1. 配置 `comfyui.models_dir`；
2. ComfyUI 启动日志里的 `Adding extra search path <类别> <绝对路径>`（新日志优先），把 `models/vae` 之类的子目录归并为 models 根目录；
3. 常见安装路径。

都找不到时抛 `ValueError`，提示用户填 `comfyui.models_dir` 或确认 ComfyUI 启动日志。`scan()` 递归列出权重文件（跳过下载暂存目录 `.ndw-downloads/`），按模板依赖生成 `config/comfy/models.json`，记录 `key / bytes / present / templates / license / license_gate / commercial_use / note`，可选手工固定 `repo` + `hf_filename`；目录内其它权重也一并收录。

### 11.2 反查链路

`resolve()` 为每个本地模型按以下顺序找线上来源，任一步命中即停：

| 顺序 | 来源 | 判定 |
| --- | --- | --- |
| 1 | `models.json` 手工固定 | 直接用 `repo` + `hf_filename` |
| 2 | ComfyUI-Manager 缓存 | 按文件名精确索引，再用仓库 sibling 校验体积 |
| 3 | 同目录 sidecar | `.metadata.json` / `.civitai.info` 里的 `repo` + `filename` 或内嵌 HF 直链 |
| 4 | 可信仓库候选 | 逐个候选仓库做 sibling 精确匹配 |
| 5 | HF 搜索 API | 按文件名主干搜候选仓库（最多 8 个），再精确匹配 sibling |

匹配以文件名精确相等为准，本地体积已知时再比对体积；命中的下载直链按当前镜像端点拼 `/resolve/main/`。单个模型网络失败只写入该条 `error`，不影响其它模型。

### 11.3 缓存、镜像链与落盘

- **缓存**：结果写 `config/comfy/models.local.json` 的 `items[key]`（`repo / hf_filename / url / source / size / resolved_at / error`）；`resolve` 默认复用缓存，`--refresh` 才重新联网。该文件是用户级数据，不入库。
- **镜像链**：默认 `["https://hf-mirror.com", "https://huggingface.co"]`，配置键 `models.download_mirrors`；音频模型（`tts/model_store.py`）与 ComfyUI 模型共用同一链，下载结果返回 `mirror_used` 与逐镜像 `attempts`。
- **下载与断点**：HF 仓库走 `hf_hub_download`（带断点），普通直链用 HTTP `Range` 续传；先落 `.ndw-downloads/` 暂存，体积校验通过后**同卷 `os.replace` 原子改名**、跨卷 `shutil.move`，失败保留暂存目录便于重试。
- **门禁**：模型声明需授权/禁止商用时必须 `accept_license`；体积超过 15GB 或未知时必须 `yes` 确认。

## 12. Web 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/ui/models` | 两个目录（模型根、ComfyUI models）、镜像链，以及音频/图像/视频三组清单：本地状态、体积、许可、线上地址、解析失败原因 |
| `POST` | `/api/ui/models/install` | 请求体 `{group, key, accept_license, yes}`，启动后台下载线程，立即返回 `{job_id}` |
| `GET` | `/api/ui/models/install/{job_id}` | 下载任务状态与日志尾部（最多 40 行） |
| `POST` | `/api/ui/models/resolve` | 重新反查线上地址 |
| `POST` | `/api/ui/comfy/health` | ComfyUI 连通性与模板自检 |

路由由 `web/models_api.py` 的 `register_models_routes` 注册。此外：`/api/config` 的 `editable` 新增 `comfyui.models_dir`（并返回 ComfyUI 健康状态、模板清单）；`/api/ui/media` 每行新增只读 `template / aspect_ratio / width / height`，页面不提供任何调参入口。
