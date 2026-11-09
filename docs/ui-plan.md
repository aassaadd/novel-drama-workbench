# 页面与命令行清单

## 1. 界面整体框架

桌面工作台，Web 前端（本地 `http://127.0.0.1:8765`）+ 可选的同功能 CLI。

```
┌──────────────────────────────────────────────────────────────────────┐
│ 顶栏：当前工程 ▾ | 章节 ▾ | 全局状态（GPU 队列 3 排队 / 1 运行）| 设置 │
├──────────┬───────────────────────────────────────────┬───────────────┤
│ 左侧导航 │ 主工作区（随页面切换）                      │ 右侧检查器     │
│ 工程列表 │                                           │ 选中项属性     │
│ 章节     │                                           │ / 产物预览     │
│ 角色     │                                           │ / 错误详情     │
│ 台词校对 │                                           │               │
│ 时间轴   │                                           │               │
│ 渲染队列 │                                           │               │
│ 产物     │                                           │               │
└──────────┴───────────────────────────────────────────┴───────────────┘
```

通用约定：

- 所有耗时操作不阻塞界面：操作 → 入队 → 顶栏与队列页显示进度。
- 所有实体状态用统一色：灰（未开始）、蓝（排队/运行）、绿（完成）、黄（过期需重跑）、红（失败）。
- 任何错误必须给出「错误码 + 中文说明 + 可操作按钮」（重试 / 去设置 / 查看日志）。
- 未对接的适配器在按钮上直接禁用并提示「适配器未对接」，不弹假成功。

## 2. 页面清单

### P1 工程列表（启动页）

| 项 | 内容 |
| --- | --- |
| 目标 | 选择/新建作品，看到每个工程的进度概览 |
| 核心操作 | 新建工程（标题、作者、类型、语言、目标时长）；打开工程；从 txt/markdown 导入；删除工程（仅移除索引，不删目录需二次确认）；查看工程路径 |
| 展示 | 卡片：书名、章节数、已完成配音行数/总行数、成片数、最近修改时间、磁盘占用 |
| 空态 | 引导「新建第一个工程」+ 示例工程入口 |
| 异常 | 目录被移动/权限不足 → 标红并给出实际检查路径 |

### P2 章节编辑

| 项 | 内容 |
| --- | --- |
| 目标 | 写/改正文与大纲，拆分场景 |
| 布局 | 左：章节目录；中：正文编辑器（Markdown）；右：场景列表 + 章节元信息 |
| 核心操作 | 新建/删除章节、调整章节顺序；编辑标题/大纲/正文；保存（自动保存 + 显式保存）；「生成大纲/续写」调用写作适配器（流式输出，可中止）；「拆场景」预览后确认；手工新增/合并/拆分场景；编辑场景的地点/时间/氛围/目标/摘要 |
| 关键交互 | 拆场景前展示 diff：原文段落 → 归属场景；确认后才写入；已有场景时提示「将覆盖 N 个场景」，需二次确认 |
| 状态 | 字数、场景数、`draft/split/voiced/composed` 徽标；正文变更后提示「下游产物已过期」并给「重跑影响项」按钮 |
| 异常 | 写作适配器未对接 → 生成类按钮禁用并给原因；生成中断保留已输出内容为草稿 |

### P3 角色与音色库

| 项 | 内容 |
| --- | --- |
| 目标 | 维护跨章节复用的角色档案与音色绑定 |
| 布局 | 左：角色列表（含旁白 `chr_narrator` 固定置顶）；右：角色详情表单 |
| 核心操作 | 新增/删除/重命名角色；编辑别名、定位、性格、背景、说话风格、外貌提示词、负向提示词；绑定音色（下拉选择引擎提供的 `voice_id`）；试听（输入示例句 → 单行合成）；设置情绪映射与默认语速；标记「未使用」角色 |
| 展示 | 角色卡显示：已绑音色、参演行数、缺音色告警 |
| 关键规则 | 音色绑定变更 → 该角色所有行标记过期，并给「批量重跑」入口；删除角色时若仍被台词行引用，必须阻止并列出引用位置 |
| 异常 | TTS 适配器未对接 → 音色下拉为空并提示；`list_voices` 失败展示错误码 |

### P4 台词校对

| 项 | 内容 |
| --- | --- |
| 目标 | 校对切分结果与表演参数，这是全流程质量的关键页 |
| 布局 | 上：场景切换 + 筛选器；中：台词表（虚拟滚动）；右：选中行的详细参数与试听 |
| 表格列 | 序号、类型（对白/旁白/独白）、说话人（可改）、文本（可编辑）、情绪、强度、语速、前/后停顿、TTS 状态、实测时长 |
| 核心操作 | 改说话人（自动补全角色名/别名）；改类型；批量设置情绪/语速；合并相邻行；拆分一行；插入旁白；删除行；单行试听/重跑；「重新切分本场景」（保留人工修改标记的提示） |
| 筛选 | 只看 `unknown` 说话人、只看失败行、只看过期行、只看旁白 |
| 关键规则 | 文本或音色变化 → 该行 `tts` 过期；行序调整 → 时间轴失效；所有改动即时落盘并显示「未保存」到「已保存」 |
| 空态 | 未拆场景 → 引导去章节页拆场景 |

### P5 时间轴

| 项 | 内容 |
| --- | --- |
| 目标 | 试听整章、微调起止与音量、确认配图/视频位置 |
| 布局 | 上：播放器（播放/暂停/进度/总时长/音量）；中：多轨时间轴（voice / sfx / music / video / image）；下：选中片段属性 |
| 核心操作 | 重建时间轴；播放/暂停/跳转到行；拖动片段起止（吸附到行边界，默认开启）；调整 `gain_db`、淡入淡出；静音单轨；把音效从「锚定行」改为绝对时间；替换某行的音频版本；导出时间轴 JSON |
| 展示 | 波形（voice 轨）、图片缩略图、视频首帧；`estimated=true` 的片段用虚线边框并提示「未实测时长」 |
| 关键规则 | 人工微调后 `revision+1`；上游任何输入变化时提示「时间轴已过期」，可选择保留人工调整或重建 |
| 异常 | 缺产物 → 该段留空并标黄，不阻塞试听其他部分；ffmpeg 不可用 → 播放器降级为逐行试听 |

### P6 渲染队列

| 项 | 内容 |
| --- | --- |
| 目标 | 观察与控制所有 GPU/CPU 任务 |
| 布局 | 上：汇总（排队/运行/成功/失败/跳过）+ 全局暂停；中：任务表；右：任务详情（参数、日志尾部、错误） |
| 表格列 | 任务 ID、类型、目标（章/场/行）、状态、尝试次数、耗时、适配器、幂等键短哈希、创建时间 |
| 核心操作 | 重试失败项；重试全部失败；取消排队/运行项；提升优先级；清空已完成；打开日志；复制幂等键；暂停队列（暂停后不取新任务，运行中任务跑完） |
| 实时性 | 每 2 秒轮询队列快照；运行中显示 ComfyUI 节点名与进度 |
| 关键规则 | 同一幂等键不重复入队；`NOT_IMPLEMENTED` 的失败项给出「去设置/去对接」跳转而不是无限重试 |
| 异常 | ComfyUI 不可达 → 顶部横幅提示地址与检查方法，队列暂停而非全部失败 |

### P7 产物预览与导出

| 项 | 内容 |
| --- | --- |
| 目标 | 看成片、导出、核对清单 |
| 布局 | 左：产物列表（按章节）；中：播放器/图片查看；右：导出配置 |
| 核心操作 | 播放成片；导出 MP4 / MP3 / WAV / SRT；选择规格（分辨率、帧率、采样率、响度）；导出后打开所在目录；删除旧产物；对比两个版本 |
| 展示 | 导出 manifest：包含行数、总时长、使用估算时长的行数、降级项（视频→图、音效缺失） |
| 关键规则 | 导出前做完整性检查（缺 TTS 的行、缺音色的角色、超 5 秒的视频），列出问题并可「忽略并导出」 |
| 异常 | 磁盘不足 / ffmpeg 缺失 → 明确提示所需空间与安装方式 |

### P8 设置

| 项 | 内容 |
| --- | --- |
| 目标 | 配置适配器与本机环境 |
| 核心操作 | 写作适配器（omlx）：协议类型（HTTP/CLI，待确认）、地址/可执行路径、模型标识、上下文窗口、停止条件、温度；TTS 适配器：引擎与默认音色、并发（固定 1）；音效适配器：引擎与默认时长；ComfyUI：地址、超时、轮询间隔、三类模板 ID 映射；GPU：锁文件路径、最大并发（固定 1）；输出规格默认值 |
| 展示 | 每个适配器有「测试连接」按钮，显示版本/耗时/错误码；模板映射显示「未找到文件」校验结果 |
| 关键规则 | 配置只写本机 `config/config.yaml`，不写进工程；密钥仅展示掩码；保存前校验必填项 |
| 异常 | omlx 协议未确认 → 明确标注「协议待确认，暂不可用」，不猜测默认值 |

## 3. CLI 命令清单

设计目标：**不打开界面即可跑完整条流水线**，所有命令支持 `--project <dir>`（也可用环境变量 `NDW_PROJECT`），输出支持 `--json` 便于脚本消费。

### 3.1 全局

```bash
ndw --version
ndw --project ~/novels/changan doctor          # 环境自检
ndw --project ~/novels/changan --json doctor
```

### 3.2 工程与文本（M1）

| 命令 | 作用 | 关键参数 |
| --- | --- | --- |
| `ndw init <dir> --title 书名` | 新建工程（生成目录与 `project.json`） | `--author` `--language` `--genre` |
| `ndw project list` | 列出本机工程（读 `~/.ndw/workspaces.json` 索引） | `--json` |
| `ndw project info` | 打印作品元数据与统计 | — |
| `ndw chapter add --title 第一章` | 新增章节 | `--index` `--outline` `--content-file` |
| `ndw chapter import <file>` | 导入 txt/markdown 并按标题切章 | `--pattern` `--max-chars` |
| `ndw chapter list` | 章节列表与状态 | `--json` |
| `ndw chapter show <chapterId>` | 输出章节 JSON | — |
| `ndw scene split --chapter <id>` | 拆分场景 | `--dry-run` `--force` `--max-chars` |
| `ndw character add --name 沈砚 --role protagonist` | 新增角色 | `--alias`（可多次） `--appearance` |
| `ndw character list` | 角色列表（含缺音色告警） | `--json` |
| `ndw character bind-voice --character <id> --voice <voiceId>` | 绑定音色 | `--speed` `--adapter` |
| `ndw line split --chapter <id>` | 切分台词行 | `--scene`（可多次） `--dry-run` `--force` |
| `ndw line list --chapter <id>` | 台词表 | `--status unknown\|failed\|stale` `--json` |
| `ndw line set --chapter <id> --line <lineId>` | 修改说话人/情绪/语速/停顿 | `--speaker` `--emotion` `--speed` `--pause-before` `--pause-after` `--text` `--kind` |

### 3.3 配音与时间轴（M2）

| 命令 | 作用 | 关键参数 |
| --- | --- | --- |
| `ndw tts plan --chapter <id>` | 只规划不执行，列出待合成行与预计数量 | `--json` |
| `ndw tts run --chapter <id>` | 批量合成（入队） | `--scene` `--line` `--retry-failed` `--wait` |
| `ndw tts run --line <id>` | 单行重跑 | `--wait` |
| `ndw timeline build --chapter <id>` | 重建时间轴 | `--dry-run` `--keep-manual` |
| `ndw timeline show --chapter <id>` | 打印各轨片段与警告 | `--json` |

### 3.4 音效、配图、视频（M3/M4）

| 命令 | 作用 | 关键参数 |
| --- | --- | --- |
| `ndw sfx plan --chapter <id>` | 按场景生成音效事件草案（规则 + 写作适配器） | `--dry-run` `--per-scene 2` |
| `ndw sfx run --chapter <id>` | 生成音效并落位 | `--scene` `--retry-failed` |
| `ndw image plan --chapter <id>` | 规划关键图（选段 + 拼提示词） | `--per-scene 1` `--json` |
| `ndw image run --chapter <id>` | 调 ComfyUI 出图 | `--scene` `--key-image` `--wait` |
| `ndw video plan --chapter <id>` | 规划 5 秒视频段落 | `--max 1` `--mode t2v\|i2v` |
| `ndw video run --chapter <id>` | 生成视频（强制 ≤ 5000ms） | `--video` `--wait` |
| `ndw comfy health` | 检查 ComfyUI 连通性与队列 | `--json` |
| `ndw comfy templates` | 列出本地模板与占位符校验结果 | — |

### 3.5 混音、导出、队列

| 命令 | 作用 | 关键参数 |
| --- | --- | --- |
| `ndw mix --chapter <id>` | 混音（语音 + 音效 + 音乐 + 视频音轨） | 默认只打印 ffmpeg 计划；`--render` 真正执行（M4） |
| `ndw export --chapter <id> --format mp4` | 导出成片（本轮为占位） | `--format mp4\|mp3\|srt\|manifest`；`--width` `--height` `--allow-incomplete` 随 M4 加入 |
| `ndw export --work --format mp3` | 导出整部作品音频（M4） | — |
| `ndw queue list` | 队列快照 | `--status failed` `--json` |
| `ndw queue run` | 启动常驻 worker 消费队列 | `--once` `--poll 2` |
| `ndw queue retry --job <id>` | 重试指定任务 | `--all-failed` |
| `ndw queue cancel --job <id>` | 取消任务 | `--all` |
| `ndw pipeline run --chapter <id> --from split --to timeline` | 一条命令串起阶段（export 阶段随 M4 加入） | `--only tts,timeline` `--dry-run` `--force` `--wait` |
| `ndw serve` | 启动本地 Web 工作台 | `--host 127.0.0.1` `--port 8765` |

### 3.6 退出码约定

| 码 | 含义 |
| --- | --- |
| 0 | 成功（或队列已全部成功） |
| 2 | 参数/配置错误 |
| 3 | 适配器未对接（`NOT_IMPLEMENTED`） |
| 4 | 前置条件缺失（未拆场景、未绑音色、缺首帧图、ComfyUI 不可达） |
| 5 | 任务执行失败（存在 `failed` 任务） |
| 6 | 校验失败（schema 不通过） |

### 3.7 全流程无界面示例

> 目标形态示例。本轮骨架中，M3/M4 相关参数（`--per-scene`、`--max`、`--mode`、`--allow-incomplete` 等）尚未实现，以各小节标注为准。

```bash
# 1. 建工程与文本
ndw init ~/novels/changan --title 长夜将明
cd ~/novels/changan
ndw chapter add --title 第一章 --content-file ./ch1.md --outline "沈砚发现卷宗缺页"
ndw scene split --chapter chp_xxx
ndw character add --name 沈砚 --role protagonist --appearance "35 岁刑警，短寸头，深色风衣"
ndw character bind-voice --character chr_xxx --voice voice_shen_yan
ndw line split --chapter chp_xxx
ndw line list --chapter chp_xxx --status unknown     # 人工/脚本修正后 set

# 2. 配音与时间轴
ndw tts run --chapter chp_xxx --wait
ndw timeline build --chapter chp_xxx

# 3. 音效与配图
ndw sfx plan --chapter chp_xxx
ndw sfx run --chapter chp_xxx --wait
ndw image plan --chapter chp_xxx --per-scene 1
ndw image run --chapter chp_xxx --wait

# 4. 短视频与导出
ndw video plan --chapter chp_xxx --max 1
ndw video run --chapter chp_xxx --wait
ndw export --chapter chp_xxx --format mp4,srt,manifest --allow-incomplete
```

## 4. 页面与 CLI 的能力对照

| 能力 | 页面 | CLI |
| --- | --- | --- |
| 新建工程 | P1 | `ndw init` |
| 写正文/拆场景 | P2 | `chapter add` / `scene split` |
| 角色与音色 | P3 | `character add` / `bind-voice` |
| 台词校对 | P4 | `line split` / `line set` |
| 配音 | P4 单行 + P6 批量 | `tts plan` / `tts run` |
| 音效 | 场景编辑内的音效面板 | `sfx plan` / `sfx run` |
| 配图/视频 | 场景编辑内的媒体面板 | `image/video plan|run` |
| 时间轴 | P5 | `timeline build` / `show` |
| 混音导出 | P7 | `mix` / `export` |
| 队列 | P6 | `queue list|run|retry|cancel` |
| 设置 | P8 | `doctor` / 直接改 `config.yaml` |

## 5. 本轮（骨架）界面交付程度

- 页面：仅提供 `ndw serve` 的 API 骨架与健康检查；前端页面为后续里程碑实现，本文件作为验收依据。
- CLI 可真实运行：`init`、`doctor`、`project list|info`、`chapter add|import|list|show`、`character add|list|bind-voice`、`scene split`、`line split|list|set`、`tts plan|run`（规划与入队真实，执行时因适配器未对接失败）、`sfx|image|video plan|run`（规划真实，执行失败）、`timeline build|show`、`mix`（只输出 ffmpeg 计划）、`comfy health|templates`、`queue list|run|retry|cancel`、`pipeline run`。
- 涉及模型的执行路径（`tts run --wait`、`sfx/image/video run --wait`、`comfy health`、`export`）按约定返回 `NOT_IMPLEMENTED`，退出码 3；`queue run` 作为常驻消费入口只打印处理数量。

## 6. 阶段 7 落地范围（P4 台词校对）

阶段 2~6 已把「行状态 / 接缝 / 范围选择 / 原文快照 / 虚拟分组」的 CLI 契约跑通，UI 只做消费方，不新增业务判定。

**做**：P4 台词校对页 + 一个最小壳（工程/章节切换）+ 队列抽屉。
**不做**：P1 完整工程管理、P2 正文编辑器、P3 音色库、P5 时间轴、P7 导出、P8 设置（继续用 `config/config.yaml`）。

### 6.1 页面与内容

| 页面 | 区域 | 放什么 | 数据来源（命令 → JSON 根键） |
| --- | --- | --- | --- |
| 壳 | 顶栏 | 工程下拉、工程概览、章节目录 | `project list --json` → `[]`；`project info --json` → `work/chapters`；`chapter list --json` → `[]` |
| P4 | 顶部 | 七态计数徽标（点徽标即筛选） | `line list --json` → `counts` |
| P4 | 左栏 | 场景列表 + 筛选（状态多选 / 只看 `unknown` 说话人 / 只看过期 / 文本搜索） | `chapter show --json` → `scenes`；筛选状态值走 `line list --status` |
| P4 | 中栏 | 台词表（虚拟滚动）：序号、类型、说话人、文本、情绪、强度、语速、前后停顿、TTS 状态、实测时长 | `line list --json` → `lines[]`（`state` 已由指纹派生，前端不再判断） |
| P4 | 右栏 | 选中行参数表单、「当前 vs 上次生成」字段对照、回滚、单行试听/重跑 | `line diff --json`；`line set`；`line revert`；`tts run --line --wait` |
| P4 | 分组抽屉 | 分组列表与「已覆盖 N 行」、未分组/重复分组告警、选中分组的影响预览与执行 | `group list --json` → `groups/detect`；`group diff --json` → `changes/covered_by`；`group apply --json` |
| P4 | 原文抽屉 | 段落级对照（missing / added / modified）与单段定位 | `chapter source --diff --json` → `summary/items`；`chapter source --paragraph N --json` |
| P4 | 底部控制条 | 「本次将生成 N 行」预览 → 开始 → 进度 | `tts plan --json` → `jobs/counts`；`tts run`（入队）；`queue list --json` 每 2s 轮询 |

### 6.2 实现约定

- 前端：`web/static/` 原生 HTML/JS，不引入构建链；`ndw serve` 挂静态目录，接口仍是 `/api/*`。
- 写操作统一走 `web/actions.py` 调 CLI 的 `cmd_*` 函数（不重写业务逻辑），把退出码映射成 HTTP：
  `0→200`、`1→409`（拒绝且未改盘）、`2→400`、`3→501`、`4→412`、`5→500`、`6→422`。
- 状态色沿用 §1 约定；`stale` 必须显示为黄并给「重跑本行」按钮，不允许前端自算。
- 表格只渲染 `line list` 已给出的字段，筛选也在后端做，避免前后端两套状态判定。

### 6.3 阶段 7 验收

1. 台词表的状态计数与 `line list` 终端输出完全一致。
2. 改一行语速 → 该行变黄（`stale`），`diff` 显示「1.0 → 0.9」；回滚后回绿（`ready`）。
3. 底部控制条预览行数 = 表内 `pending + stale + failed` 行数。
4. 分组 `apply` 后仅命中行变黄，未命中行颜色不变。
5. 原文抽屉能定位到被删段落的 `missing`，并给出当前对应位置。
6. 所有失败给出「错误码 + 中文说明 + 可操作按钮」，不出现假成功。
7. 生成期间只轮询 `queue list`：整轮生成里 `/api/ui/lines`（整表）调用次数为 0，行状态只经 `line list --line` 单行回填、只重画那一行 DOM；队列有任务执行时轮询 2 秒，空闲时放宽到 4 秒（worker 尚未入队最多等 30 秒，队列转空后再等 6 秒收尾）。
8. 关掉服务再起，浏览器本地状态清空后手动填入工程路径即可打开，不依赖任何服务端内存状态。
9. 模型缺失时「开始」禁用并在底栏写明原因（provider + 错误码 + 路径），`POST /api/ui/generate` 返回 412；界面与 `serve --config` 读同一份配置。
10. 「开始」必须触发真实 CosyVoice3 合成并产出 wav：队列 `execute_job` 已接线为一行一 job 调 `run_batch`，落盘/侧车/状态已实测通过；真合成的整章实跑待 GPU 空闲后补验。
