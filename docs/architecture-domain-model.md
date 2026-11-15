# 领域模型重构方案

> 状态：**方案待确认**（第一轮只出方案，不改业务代码）
> 适用工程：`/Users/zhaohaochen/git/novel-drama-workbench`
> 前置基线：[architecture.md](architecture.md)（分层与适配器契约）、[data-model.md](data-model.md)（现有字段与状态机）、`schema/*.json`（落盘契约）
> 范围：把当前「散函数 + 裸 dict」的项目结构，改成「领域实体 + 值对象 + 聚合 + 应用服务 + 适配器 + 接口」的面向对象结构。
> 不在本轮范围：任何业务代码改动、schema 字段增删、前端改动、真实模型调用。

---

## 1. 为什么改

### 1.1 现状取证

| 证据 | 数值 | 说明 |
| --- | --- | --- |
| Python 文件总数 | 168 个 / 78 782 行 | `src/novel_drama/` 下 |
| 最大单文件 | `web/app.py` 6 154 行、`writing/service.py` 5 546 行、`adapters/writing.py` 2 003 行 | 均超 1 500 行上限 |
| HTTP 路由 | 167 条，全部注册在 `web/app.py` 的 `create_app()` 内 | 无按领域拆分 |
| CLI 模块 | 18 个 `cli_*.py`，`cli.py` 1 854 行 | 命令与业务混排 |
| 实体表示 | `models/entities.py` 409 行，全部是「返回 dict 的工厂函数」 | 无类型、无行为、无校验 |
| 校验位置 | 全部在 `store/schema_validator.py` 的 JSON Schema | 领域规则与落盘格式绑定 |
| 领域规则位置 | 分散在 `store/voice_cast.py`(1 164)、`tts/line_state.py`(593)、`writing/cast_sync.py`(748)、`tts/routing.py`(405)、`orchestration/pipeline.py` 等 | 同一规则多处复制 |

具体症状：

1. **实体是裸 dict**：`entities.new_line()` 返回 dict，谁都能 `line["tts"]["status"] = "ready"`，没有任何地方保证「文本为空不能有音频」「revision 必须随正文变更 +1」。这类不变量现在靠调用方自觉，或散落在 `line_state.py`、`voice_cast.py`、`schema` 三处各写一份。
2. **规则与 I/O 混在一起**：`store/voice_cast.py` 的 `resolve_cast_voice()` 既做 10 级优先级解析（纯领域规则），又读多个文件（I/O）；`tts/line_state.py` 的 `revert_line()` 既算差异又直接 `store.save_chapter()`。
3. **一个文件装多个上下文**：`writing/service.py` 同时承担大纲生成、正文生成、导演标注、设定/角色/世界生成、角色清洗、大纲绑定、出图提示词拼装（`build_art_prompt`），函数间靠 dict key 约定协作。
4. **分层靠约定**：`web/app.py` 直接 `import` 了 `writing.editing`、`media.comfy_media`、`models.ids`，绕过编排层；`orchestration/pipeline.py` 里的 `split_chapter_scenes` 直接 new 场景 dict 写进章节。
5. **跨聚合引用用嵌套 dict**：章节里内嵌 `scenes[].lines[]`，坑位与台本脚本（`scripts/<chapterId>.json` 的 `shots[].lines[]`）是两套并行的台词行结构，同一 `line_id` 在两个文件里各存一份，靠 `materialize.py` 同步。

### 1.2 改造目标

- 实体清晰：每个业务对象有唯一类、明确字段、明确不变量，不再用裸 dict 传递核心数据。
- 封装到位：改状态只能走实体方法，领域规则只有一份实现。
- 目录对应领域：目录名说得出「这是什么业务」，而不是「这是什么技术动作」。
- 可增量落地：不一次性大重构，每阶段独立可验证、可回滚。

---

## 2. 建模原则（先定边界，再谈抽象）

这一节是整份方案的自律条款，后续所有设计都必须服从，避免「为抽象而抽象」。

### 2.1 什么时候才建对象

| 条件 | 建模方式 |
| --- | --- |
| 有独立生命周期，需要被按 ID 引用（章节、角色、任务） | **实体** |
| 有需要被强制的不变量（锚点必须哈希匹配、音色必须有授权） | **实体或值对象** + 构造期校验 |
| 没有身份，只描述「是什么」，相等即内容相等（时间区间、音色绑定、音频规格） | **值对象**（`frozen dataclass`） |
| 只是两个字段的袋子，没有任何规则 | **不建对象**，继续用 `TypedDict` 或 dict |
| 纯派生结果（时间轴、导出包） | **读模型**，不进聚合，不承担不变量 |
| 供应商协议结构（`TextGenRequest`、`ComfyJobStatus`） | 留在 **适配器层**，不下沉领域层 |

明确拒绝的做法：

- 不引入 ORM、Repository 泛型基类、UnitOfWork、事件总线、CQRS。仓储就是现有 `store/*` 的薄包装：`get() -> 实体` / `save(实体)`。
- 不给 `id` 单独包一层类。`ChapterId` 用 `NewType` 做编译期品牌即可，运行时仍是 `str`。
- 不为每个实体都建仓储。只有**聚合根**有仓储，聚合内实体只能经聚合根读写。
- 不改落盘 JSON 的字段名与结构。实体的 `to_dict()` 直接产出当前 `schema/*.json` 的形状，Schema 仍是落盘唯一契约。

### 2.2 落地形态

实体用 `dataclass`，落盘键名保持 snake_case，与现有 schema 一一对应。示意（不是本轮要写的代码）：

```python
@dataclass
class Chapter:
    """章节聚合根：正文与版本是它唯一负责的真相。"""
    id: ChapterId
    work_id: WorkId
    index: int
    title: str
    status: ChapterStatus
    revision: Revision
    content: str | None = None
    blocks: list[ChapterBlock] = field(default_factory=list)
    ...

    def rewrite_content(self, text: str) -> None:
        """改写正文：重算字数、派生段落块、revision 自增，并把旧版本交给仓储归档。

        参数:
            text: 新正文。

        异常:
            InvariantViolation: 正文超过 200000 字或与当前正文相同。
        """
```

约定：

- **构造即校验**：`__post_init__` 或 `from_dict()` 内执行不变量；校验失败抛 `InvariantViolation`（领域异常），由应用层翻译成中文提示 + 退出码。
- **状态变更只走方法**：禁止外部直接赋值 `chapter.status = ...`；提供 `mark_scripted()`、`mark_stale()` 等语义方法，内部校验状态迁移合法性。
- **派生字段不存两份**：`word_count`、`blocks`、`scene_count` 由 `content` 计算，`to_dict()` 时写出（保持 schema 兼容），`from_dict()` 时忽略文件里的旧值并重算。
- **跨聚合只存 ID**：实体里不出现别的聚合的完整对象，只出现 `CharacterId` / `VoiceId` 这类标识。
- 每个文件 ≤ 1 500 行（目标 300~600 行），每个函数写中文注释（功能 / 参数 / 返回 / 异常）。

---

## 3. 领域对象清单

### 3.1 总览

| # | 对象 | 类型 | 所属聚合 | 落盘位置 |
| --- | --- | --- | --- | --- |
| 1 | Work（作品/工程元数据） | 实体（根） | Work | `project.json#work` |
| 2 | ProjectSettings | 值对象 | Work | `project.json#settings` |
| 3 | StoryBible（设定库） | 实体（根） | StoryBible | `settings/bible.json` |
| 4 | Act（幕） | 实体 | StoryBible | `bible.json#outline.acts[]` |
| 5 | ChapterPlan（大纲章节） | 实体 | StoryBible | `bible.json#outline.acts[].chapters[]` |
| 6 | Beat（节拍） | 实体 | StoryBible | `...chapters[].beats[]` |
| 7 | World（世界条目） | 实体 | WorldLibrary | `settings/worlds.json` |
| 8 | Chapter（章节正文） | 实体（根） | Chapter | `chapters/<id>.json` |
| 9 | ChapterBlock（正文段落块） | 值对象 | Chapter | 同上 `blocks[]`（派生） |
| 10 | ContinuationSession（续写会话） | 实体 | Chapter | 同上 `continuations[]` |
| 11 | ImportSnapshot（导入快照引用） | 值对象 | Chapter | 同上 `source` |
| 12 | Scene（场景，v1） | 实体 | Chapter | 同上 `scenes[]` |
| 13 | Character（角色） | 实体 | Cast | `characters.json#characters[]` |
| 14 | CharacterState（状态卡） | 值对象 | Cast | 角色内 `state` |
| 15 | VoiceBinding（音色绑定） | 值对象 | Cast | 角色内 `voice_binding` |
| 16 | VoiceProfile（音色描述） | 值对象 | Cast | 角色内 `voice_profile` |
| 17 | Voice（音色） | 实体 | VoiceLibrary | `voices.json#voices[]` |
| 18 | VoiceVariant（音色变体） | 实体 | VoiceLibrary | `...voices[].variants[]` |
| 19 | NarratorPersona（旁白人格） | 实体 | VoiceCast | `project.json#settings.tts.cast.narrators[]` |
| 20 | Script（导演脚本） | 实体（根） | Script | `scripts/<chapterId>.json` |
| 21 | Shot（镜头） | 实体 | Script | `...shots[]` |
| 22 | Line（台词行） | 实体 | Script | `...shots[].lines[]` |
| 23 | Anchor（正文锚点） | 值对象 | Script | 各元素 `anchor` |
| 24 | TtsState（配音状态） | 值对象 | Script | 行内 `tts` |
| 25 | SfxEvent（音效事件） | 实体 | Script | `...shots[].sfx_events[]` |
| 26 | SilenceEvent（静音事件） | 实体 | Script | `...silence_events[]` |
| 27 | Visual（画面） | 实体 | Script | `...visuals[]` |
| 28 | Animation（动效） | 实体 | Script | `...animations[]` |
| 29 | BgmPlan（配乐计划） | 实体 | Script | `...bgm` |
| 30 | Job（生成/配音任务） | 实体（根） | Job | `.ndw/queue.json#jobs[]` |
| 31 | ModelCallRecord（模型调用记录） | 实体（只追加） | ModelCallLog | `.ndw/logs/*.jsonl` |
| 32 | LineTake（配音产物侧车快照） | 值对象（旁挂） | Script | `assets/voice/lines/<line>_r<rev>.json` |
| 33 | Timeline（时间轴） | 读模型（派生） | — | `timelines/<chapterId>.timeline.json` |
| 34 | ProductionSettings | 值对象 | Work | `project.json#settings.production` |
| 35 | TtsProviderSpec / RoutingPolicy | 值对象 | Work | `project.json#settings.tts` |

> 「实体 / 值对象」判定口径：有独立 ID 且状态会随时间演进的是实体；没有 ID 且相等即内容相等的是值对象。`Scene`(12) 是过渡态：v2 目标下场景的唯一归属是 `Script.Shot`(21)，第 5 阶段完成后 `Chapter.scenes` 废弃（见 §7.3）。

### 3.2 故事上下文（story）

#### 3.2.1 Work — 作品/工程元数据（实体，聚合根）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `WorkId` | 前缀 `wrk_`，创建后不变 |
| `title` | `str` | 1~200 字，去首尾空白后非空 |
| `author` | `str` | ≤100 字，可空 |
| `genre` | `list[GenreTag]` | 去重，单项 ≤40 字 |
| `logline` | `str` | ≤500 字 |
| `language` | `LanguageCode` | 匹配 `^[a-z]{2}-[A-Z]{2}$`，默认 `zh-CN` |
| `usage` | `UsagePurpose` | 枚举 `commercial` / `personal`；`commercial` 时许可受限 provider 出局 |
| `style_guide` | `str` | ≤2 000 字 |
| `target_duration_ms` | `DurationMs` | ≥0 |
| `created_at` / `updated_at` | `Timestamp` | UTC ISO8601，秒级，`Z` 结尾 |

不变式：`id` 不可变；`title` 非空；`updated_at` 由 `touch()` 统一刷新，禁止调用方手写。
方法：`rename(title)`、`change_usage(purpose)`、`touch()`。

#### 3.2.2 ProjectSettings — 工程设置（值对象）

按现有 `project.schema.json` 拆成子值对象，字段名不变：

| 子对象 | 关键字段 | 说明 |
| --- | --- | --- |
| `WritingSettings` | `adapter`(固定 `omlx`)、`protocol`、`endpoint`、`model`、`context_window`、`temperature`、`stream`、`thinking` | 写作模型只允许本机 omlx |
| `TtsSettings` | `adapter`、`model`、`default_provider_id`、`provider_order`、`routing`、`cast`、`max_chunk_ms`、`chunk_silence_ms`、`max_speed_delta` | `routing`、`cast` 见 3.4 |
| `SfxSettings` | `adapter`、`default_gain_db` | |
| `ComfySettings` | `base_url`、`poll_interval_ms`、`job_timeout_ms`、`templates{image,video_t2v,video_i2v}` | |
| `OutputSettings` | `sample_rate`、`channels`、`loudness_target_lufs`、`video_max_ms`(固定 5000)、`aspect_ratio`、`image_width/height` | `aspect_ratio` 是尺寸唯一来源 |
| `GpuSettings` | `lock_path`、`max_concurrent`(固定 1)、`unload_on_switch`、`poll_interval_ms` | 单卡串行是硬约束 |
| `ProductionSettings` | `silences_ms{11 项}`、`audio{format,sample_rate,bitrate_kbps}` | 时间轴与导出共用 |

不变式：`max_concurrent == 1`；`video_max_ms == 5000`；`aspect_ratio` 只能是 `1:1 / 3:4 / 4:3 / 9:16 / 16:9`；`silences_ms` 全部 0~10 000；`production.audio.sample_rate` 必须等于 `output.sample_rate`（当前由 `save_production()` 手工同步，改为值对象构造时强制一致）。

#### 3.2.3 StoryBible — 设定库（实体，聚合根）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `schema_version` | `int` | 固定 1 |
| `settings_revision` | `Revision` | 任何设定变更 +1，单调递增 |
| `work_id` | `WorkId` | 必须存在 |
| `premise` | `Premise`（VO） | `logline` 非空；`target_length` ∈ `short/novella/novel/series` |
| `outline` | `Outline`（VO，内含 `acts`） | 见 3.2.4 |
| `style` | `StyleSpec`（VO） | `tone` 非空；`pov` ∈ `first/second/third_limited/third_omniscient`；`tense` ∈ `past/present` |
| `continuity` | `Continuity`（VO） | `timeline[]`、`open_threads[]`（`status` ∈ `open/resolved/abandoned`） |
| `characters_ref` / `voices_ref` | `str` | 默认 `characters.json` / `voices.json` |
| `created_at` / `updated_at` | `Timestamp` | |

不变式：`settings_revision` 单调；`WorkId` 与工程一致；`OpenThread.status` 只能从 `open` 走向 `resolved`/`abandoned`，不可回退。
方法：`add_act()`、`remove_act()`、`locate_chapter_plan(chapter_id)`、`touch_settings()`。

#### 3.2.4 Act — 幕（实体）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `ActId` | 前缀 `act_` |
| `title` | `str` | ≤200 字 |
| `summary` | `str` | ≤2 000 字 |
| `chapters` | `list[ChapterPlan]` | 见 3.2.5；幕内顺序即阅读顺序 |
| `origin` | `OriginMark`（VO） | 见 3.2.7，可空 |

不变式：同一 `StoryBible` 内 `ActId` 唯一；幕之间顺序由数组位置决定，不存 `index`（当前实现也没有，避免两套序号）。
方法：`append_chapter()`、`reorder_chapters(ids)`、`mark_continuation(origin)`。

#### 3.2.5 ChapterPlan — 大纲章节（实体）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `ChapterId` | 前缀 `chp_`；**必须与正文 `Chapter.id` 相同** |
| `index` | `int` | ≥1，全书唯一且连续 |
| `title` | `str` | ≤200 字 |
| `summary` | `str` | ≤2 000 字 |
| `beats` | `list[Beat]` | 见 3.2.6 |
| `pov_character_id` | `CharacterId \| None` | 必须存在于 Cast |
| `pov_mode` | `PovMode \| None` | `None` 表示跟随全书 |
| `character_arcs` | `list[CharacterArc]`（VO） | `character_id` 必须存在；`from_state`/`to_state` ≤500 字 |
| `world_ids` | `list[WorldId]` | 必须存在于 WorldLibrary |
| `cast_synced` | `bool` | 角色识别是否已跑过，一次置真不回退 |
| `cast_synced_at` | `Timestamp \| None` | `cast_synced=true` 时必填 |
| `cast_created` / `cast_reused` / `cast_extras` / `cast_pending` | `list[str]` | 姓名（非 ID），≤60 字/项 |

不变式（重点）：`ChapterPlan.id == Chapter.id`。当前工程出现过「设定库章节 ID 与 `chapters/*.json` ID 不一致」，根因就是这条规则没有任何地方强制，`_chapter_beats()` 只能回退去读 `chapter.outline` 文本。重构后该规则进 `ChapterPlan` 构造校验，不一致直接拒绝。

#### 3.2.6 Beat — 节拍（实体）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `BeatId` | 前缀 `bea_` |
| `summary` | `str` | ≤1 000 字，非空 |
| `character_ids` | `list[CharacterId]` | 去重，必须存在于 Cast |
| `extra_characters` | `list[str]` | 只在本章出现的龙套名，≤60 字 |
| `turning_point` | `bool` | 关键转折，供配图/视频优先级 |
| `target_words` | `int` | 0~20 000 |

不变式：同一 `ChapterPlan` 内 `BeatId` 唯一；`summary` 非空；`character_ids` 不得为空数组以外的脏值（允许纯旁白章）。
方法：`bind_characters(ids, known)`（校验存在性）、`retarget_words(n)`。

#### 3.2.7 值对象：OriginMark / Premise / StyleSpec / Continuity / CharacterArc

| 值对象 | 字段 | 校验 |
| --- | --- | --- |
| `OriginMark` | `kind`(`expansion`/`continuation`)、`mode`(`after_act`/`after_chapter`/`append`)、`anchor_act_id`、`anchor_chapter_id`、`created_at` | `kind=expansion` 时 `mode ∈ {after_act, after_chapter}`；`kind=continuation` 时 `mode=append` |
| `Premise` | `logline`、`theme`、`core_conflict`、`ending_intent`、`target_audience`、`target_length`、`target_words` | `logline` 1~500 字 |
| `StyleSpec` | `tone`、`pov`、`tense`、`prose_style`、`forbidden[]`、`visual_style` | `tone` 非空 |
| `Continuity` | `timeline[{at,event,chapter_id}]`、`open_threads[{id,text,status}]` | `thread_id` 前缀 `thr_` |
| `CharacterArc` | `character_id`、`from_state`、`to_state`、`note` | `character_id` 必填 |

#### 3.2.8 Chapter — 章节正文（实体，聚合根）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `schema_version` | `int` | 固定 1 |
| `id` | `ChapterId` | `chp_` |
| `work_id` | `WorkId` | |
| `index` | `int` | ≥1；工程内唯一 |
| `title` | `str` | 1~200 字 |
| `outline` | `str \| None` | ≤8 000 字（正文反推的大纲） |
| `synopsis` | `str \| None` | ≤4 000 字，滚动摘要 |
| `content` | `str \| None` | ≤200 000 字 |
| `word_count` | `int` | **派生**：非空白字符数 |
| `pov_character_id` | `CharacterId \| None` | |
| `pov_mode` | `PovMode \| None` | |
| `voice_overrides` | `dict[CharacterId, VoiceOverride]` | 键必须存在；值必须带 `reason`；仅旁白/龙套允许 |
| `status` | `ChapterStatus` | 见 3.12 枚举 |
| `blocks` | `list[ChapterBlock]` | **派生**：由 `content` 重建 |
| `script_path` | `str \| None` | `scripts/<id>.json` |
| `source` | `ImportSnapshot \| None` | 导入原文快照引用 |
| `continuations` | `list[ContinuationSession]` | 续写/改写会话 |
| `scenes` | `list[Scene]` | v1 兼容，见 §7.3 废弃计划 |
| `revision` | `Revision` | ≥1，正文/结构变更 +1 |
| `created_at` / `updated_at` | `Timestamp` | |

不变式：

1. `index` 在工程内唯一；`status` 迁移只允许 `draft → written → split → scripted → voiced → visualized → composed`，任意状态可跳 `stale`，`stale` 只能回到 `written`。
2. `content` 变更 ⇒ `revision + 1`、重算 `word_count` 与 `blocks`、`status` 至少回退到 `written`、旧版本交给仓储归档。**这是本次要收进实体的第一条规则**（当前 `ProjectStore.save_chapter()` 用「比对 content 字符串」的隐式方式实现）。
3. `status ∈ {scripted, voiced, visualized, composed}` 时 `script_path` 必须非空。
4. `word_count` 不接受外部赋值，只由 `rewrite_content()` / `append_content()` 重算。
5. 正文为空字符串时存 `None`，不留 `""`（避免「空正文算不算写过」的分歧）。

方法：`rewrite_content(text)`、`append_content(text)`、`replace_range(start, end, text)`、`mark_scripted(script_id)`、`mark_stale(reason)`、`bind_pov(character_id, mode)`、`set_voice_override(...)`。

#### 3.2.9 值对象：ChapterBlock / ImportSnapshot

| 值对象 | 字段 | 校验 |
| --- | --- | --- |
| `ChapterBlock` | `id`(`blk_`)、`index`、`kind`(`paragraph`/`dialogue`/`heading`)、`char_start`、`char_end`、`text_hash`(`sha256:...`) | `char_end > char_start`；`text_hash` 与实际文本一致；由正文重建，不接受外部构造 |
| `ImportSnapshot` | `snapshot_id`、`file`、`sha256`、`chars`、`imported_at`、`source_path`、`tool` | 快照文件必须存在且哈希一致，否则导入即可疑 |

#### 3.2.10 ContinuationSession — 续写会话（实体）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `ContinuationId` | `cnt_` |
| `mode` | `ContinuationMode` | `continue` / `outline_driven` / `rewrite_range` |
| `instruction` | `str \| None` | ≤4 000 字 |
| `target_words` | `int \| None` | 100~20 000 |
| `outline_ref` | `str \| None` | ≤300 字 |
| `range` | `CharRange \| None` | `rewrite_range` 时必填 |
| `context` | `ContextRequest`（VO） | `include_bible` 必填；`budget_tokens` 1 024~262 144 |
| `status` | `ContinuationStatus` | `pending/running/done/failed/canceled` |
| `produced` | `CharRange \| None` | `done` 时必填 |
| `model` / `usage` | `str \| None` / `TokenUsage \| None` | 模型名与 token 用量留痕 |
| `created_at` | `Timestamp` | |

不变式：`mode=rewrite_range` ⇔ `range` 非空；`status=done` ⇒ `produced` 非空且 `produced.end > produced.start`；同一章节同一时刻最多一个 `running`。

### 3.3 角色上下文（cast）

#### 3.3.1 Cast — 角色表（聚合根）

角色表整体作为聚合根 `Cast`，`Character` 是聚合内实体。理由：当前系统存在三条**表级**不变量——
① 姓名/别名在全书内唯一（`cast_sync._index_characters` 与 `character_merge` 的归一化比较）；
② 关系是双向一致的（A→B 有 `relations` 时 B→A 也应有记录，目前无人保证）；
③ 合并角色要把所有引用迁移干净（`character_merge.py` 526 行就是在做这件事）。
这三条都无法靠单个 `Character` 自洽，必须由聚合根协调。规模上单本约几十到一百多个角色，整表加载与当前 `load_characters()` 行为一致，不引入性能问题。

`Cast` 对外只暴露：`add(character)`、`rename(id, name)`、`merge(source, target)`、`find_by_name(name)`、`index_by_voice(voice_id)`、`assert_unique_names()`。

#### 3.3.2 Character — 角色（实体）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `CharacterId` | `chr_`；旁白固定 `chr_narrator` 且 `role=narrator` |
| `name` | `str` | 1~60 字；全书唯一（归一化去空白小写后比较） |
| `aliases` | `list[str]` | 去重；不得与任何角色的 `name`/`aliases` 冲突 |
| `role` | `CharacterRole` | `protagonist/antagonist/supporting/narrator/minor` |
| `gender` | `Gender \| None` | `male/female/other/unknown` |
| `age_range` | `str \| None` | ≤40 字 |
| `personality` | `list[str]` | 单项 ≤40 字 |
| `background` | `str` | ≤2 000 字 |
| `speech_style` | `SpeechStyle`（VO） | `tempo/pitch/accent` ≤40 字，`catchphrases[]` ≤60 字/项 |
| `voice_binding` | `VoiceBinding \| None` | 见 3.3.4 |
| `voice_profile` | `VoiceProfile \| None` | 见 3.3.5 |
| `stock` | `StockSlot \| None` | 仅 `role=minor` 可用 |
| `appearance_prompt` / `negative_prompt` | `str` | ≤2 000 / ≤1 000 字 |
| `voice_description_zh/en` | `str` | ≤1 000 字，中英各一份 |
| `appearance_desc_zh/en` | `str` | ≤1 200 字 |
| `appearance_negative_zh/en` | `str` | ≤800 字 |
| `portrait_asset` | `AssetPath \| None` | 相对路径，禁止绝对路径 |
| `image_assets` | `CharacterImages`（VO） | `fullbody/closeup/turnaround` |
| `state` | `CharacterState \| None` | 见 3.3.3 |
| `created_at` / `updated_at` | `Timestamp` | |

不变式：

1. `role=narrator` ⇔ `id=chr_narrator`（当前由 schema 的 `allOf/if-then` 保证，改为实体构造校验）。
2. `role=minor` 才能有 `stock`；`stock` 存在时 `stock.slot` 必须匹配 `^[a-z][a-z0-9_]{1,39}$`。
3. `voice_binding` 非空时，参与配音的角色必须有可解析的音色（`voice_id` 或 `voice_library_id` 至少一个）。
4. 分配了 `voice_description_zh/en` 时两者必须成对出现（当前只能靠 `voice_design` 的代码顺序保证）。
5. `aliases` 与 `name` 归一化后不得重复。

方法：`rename(new_name)`、`add_alias(alias)`、`bind_voice(binding)`、`unbind_voice()`、`update_state(state)`、`set_stock(slot)`。

#### 3.3.3 值对象：CharacterState / CharacterRelation

| 值对象 | 字段 | 校验 |
| --- | --- | --- |
| `CharacterState` | `situation`(≤1 000)、`relationship_notes{CharacterId→≤500}`、`knowledge[]`、`relations[CharacterRelation]`、`as_of_chapter_id` | `relations` 的 `target_id` 必须存在于 Cast；`as_of_chapter_id` 必须存在 |
| `CharacterRelation` | `target_id`、`label`(≤40)、`sentiment`(`ally/enemy/family/romance/neutral`)、`since_chapter_id`、`note`(≤500) | `target_id != 自身`；同一 target 只允许一条 |

#### 3.3.4 值对象：VoiceBinding

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `adapter` | `str` | ≤40 字，必填 |
| `voice_id` | `VoiceId \| None` | v1 兼容，长度 ≤120（注意：v1 是供应商侧 ID，不是 `voc_`） |
| `voice_library_id` | `VoiceId \| None` | v2，优先于 `voice_id` |
| `default_variant_id` | `VariantId \| None` | 必须是所绑 `Voice` 的变体之一 |
| `emotion_variant_map` | `dict[Emotion, VariantId]` | 值必须属于所绑 `Voice`；键必须是合法情绪枚举 |
| `fallback_voice_id` | `VoiceId \| None` | 不得等于主音色 |
| `provider_id` / `fallback_provider_id` | `str \| None` | ≤64 字 |
| `reference_audio` | `AssetPath \| None` | 克隆参考音 |
| `seed` | `int \| None` | |
| `speed` | `float` | 0.5~2.0 |
| `emotion_map` | `dict[Emotion, str]` | 统一情绪 → 引擎标签 |

不变式：`voice_id` 与 `voice_library_id` 至少一个非空；`default_variant_id`、`emotion_variant_map`、`fallback_voice_id` 的**存在性校验要跨聚合**（需要 `VoiceLibrary`），因此解析放在领域服务 `resolve_voice()` 里，绑定对象自身只做形状校验。

#### 3.3.5 值对象：VoiceProfile

结构化音色描述，字段较多（`summary`/`gender`/`age_feel`/`pitch`/`timbre[]`/`pace`/`energy`/`breathiness`/`raspiness`/`accent`/`design_prompt`/`negative_prompt[]`/`confidence`/`status`/`source`…）。按现有 schema `additionalProperties: true` 保留**可扩展**（不锁死字段），但以下进校验：

- `gender` ∈ `male/female/other/unknown`；`gender_source` ∈ `character/manual/inferred`；
- `age_feel` ∈ `child/teen/young_adult/adult/middle_aged/elderly`；
- `breathiness`/`raspiness`/`confidence`/`role_distinctiveness` ∈ [0,1]；
- `status` ∈ `draft/ready/stale/missing`；`status=stale` 必须带 `stale_reason`。

#### 3.3.6 Voice — 音色（聚合根，实体）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `VoiceId` | `voc_` |
| `name` | `str` | 1~120 字 |
| `default_provider_id` | `str \| None` | ≤64 字 |
| `language` | `LanguageCode` | 必填 |
| `gender` | `Gender` | 必填（无 `null`） |
| `tags` | `list[str]` | ≤40 字/项 |
| `default_variant_id` | `VariantId` | 必须存在于 `variants` |
| `variants` | `list[VoiceVariant]` | 至少 1 个；`VariantId` 唯一 |
| `status` | `VoiceStatus` | `ready/unusable/archived` |
| `revision` | `Revision` | ≥1 |
| `created_at` / `updated_at` | `Timestamp` | |

不变式：`default_variant_id` ∈ `variants[].id`；`status=ready` 时至少存在一个 `status=ready` 且 `source=preset` 或有 `reference+consent` 的变体；删除变体前必须先改 `default_variant_id`。

#### 3.3.7 VoiceVariant — 音色变体（实体）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `VariantId` | `vav_` |
| `name` | `str` | 1~120 字，如「默认」「愤怒」「低语」 |
| `source` | `VoiceSource` | `preset/cloned/recorded/designed` |
| `emotion_tags` | `list[VariantEmotionTag]` | 至少 1 项；含 `default` 表示兜底 |
| `preset_name` | `PresetVoiceName \| None` | `source=preset` 时必填，取值限定 9 个官方预设 |
| `reference` | `VoiceReference \| None` | `source≠preset` 时必填 |
| `prompt_cache` | `PromptCache \| None` | `status ∈ pending/ready/failed`；`ready` 时 `path` 与 `hash` 必填 |
| `consent` | `VoiceConsent \| None` | `source≠preset` 时必填且 `confirmed=true` |
| `generation` | `VoiceGeneration \| None` | voice design 记录：`engine`/`prompt`/`seed`/`batch_id`/`generated_at` |
| `quality` | `VoiceQuality \| None` | `status ∈ unchecked/pass/warn/fail` |
| `provider_availability` | `list[ProviderAvailability]` | provider 维度可用性 |
| `status` | `VariantStatus` | `ready/pending/unusable` |
| `gender_source` | `GenderSource \| None` | `character/manual` |
| `created_at` | `Timestamp` | |

不变式（当前写在 schema 的 `allOf` 里，改为实体方法）：

1. `source=preset` ⇒ `preset_name` 非空；
2. `source ∈ {cloned,recorded,designed}` ⇒ `reference` 与 `consent` 非空，且 `consent.confirmed=true`；
3. `reference.duration_ms` ∈ [2 000, 20 000]（克隆最少 3 秒）；
4. `provider_availability[].provider_id` 不重复；
5. 变体一旦被任何 `VoiceBinding` 引用，`status` 不得直接置 `unusable`，必须先解绑（跨聚合，由应用服务校验）。

方法：`mark_unusable(reason)`、`record_generation(meta)`、`attach_preview(provider_id, asset)`。

#### 3.3.8 值对象：VoiceReference / VoiceConsent / PromptCache / ProviderAvailability

| 值对象 | 字段 | 校验 |
| --- | --- | --- |
| `VoiceReference` | `audio_path`、`duration_ms`、`text`、`snr_db`、`channels`、`sample_rate`、`sha256`、`denoise` | `duration_ms` 2 000~20 000；`channels ∈ {1,2}`；`sample_rate` 16 000~96 000；`sha256` 用于校验参考音未被误替换 |
| `VoiceConsent` | `confirmed`、`note`、`evidence_path` | `confirmed=false` 时禁止合成（不是警告，是拒绝） |
| `PromptCache` | `status`、`path`、`hash`、`model`、`created_at` | `ready` 时 `path`/`hash` 必填；`hash` 变化即失效 |
| `ProviderAvailability` | `provider_id`、`status`、`clone_supported`、`preview_asset_id`、`similarity_note`、`reason`、`checked_at` | `status ∈ ready/pending/unusable`；`unusable` 时 `reason` 必填 |

#### 3.3.9 NarratorPersona / VoicePoolSlot / NarratorSwitch（值对象，VoiceCast 上下文）

| 对象 | 字段 | 校验 |
| --- | --- | --- |
| `NarratorPersona` | `id`(`npr_`)、`label`、`voice_id`、`voice_profile` | `label` 非空；`voice_id` 必须存在 |
| `VoicePoolSlot` | `slot`、`label`、`voice_id`、`gender`、`age_feel` | `slot` 匹配 `^[a-z][a-z0-9_]{1,39}$`，落盘后不可改 |
| `NarratorSwitch` | `at_chapter_id`、`end_chapter_id`、`persona_id`、`reason`、`scope`(`from_here`/`this_chapter_only`) | `end >= at`（按章节 index）；区间不得重叠 |

### 3.4 台本上下文（script）

#### 3.4.1 Script — 导演脚本（聚合根，实体）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `schema_version` | `int` | 固定 1 |
| `id` | `ScriptId` | `scr_` |
| `chapter_id` | `ChapterId` | 必须存在 |
| `work_id` | `WorkId` | |
| `source` | `ScriptSource`（VO） | `chapter_revision`、`settings_revision`、`content_hash` 三项必填 |
| `status` | `ScriptStatus` | `draft/ready/stale` |
| `style` | `ScriptStyle`（VO） | `visual_style`、`narration_voice_id`、`bgm_mood` |
| `shots` | `list[Shot]` | 顺序即时间顺序 |
| `warnings` | `list[str]` | ≤500 字/条 |
| `created_at` / `updated_at` | `Timestamp` | |

不变式：

1. `source.chapter_revision` / `content_hash` 与章节当前值不一致 ⇒ `status` 必须为 `stale`（`is_stale(chapter)` 判定，不落盘额外标记）。
2. `shots[].index` 连续从 1 开始，不跳号。
3. 同一 `Script` 内 `ShotId` / `LineId` 全局唯一。
4. `status=ready` 要求所有 shot 的 `anchor.state != orphan`（当前只在告警里提示，重构后升级为状态门禁，可在应用层按开关降级）。

#### 3.4.2 Shot — 镜头/场景（实体）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `ShotId` | `sht_` |
| `index` | `int` | ≥1 |
| `scene_label` | `str` | 1~120 字 |
| `location` | `str \| None` | ≤200 字 |
| `time_of_day` | `TimeOfDay` | `dawn/morning/day/dusk/night/unknown` |
| `mood` / `goal` | `str \| None` | ≤200 / ≤500 字 |
| `anchor` | `Anchor` | 必填 |
| `transition_in` / `transition_out` | `Transition`（VO） | `type ∈ cut/fade/dissolve/none`；`duration_ms` ≤10 000 |
| `status` | `ShotStatus` | `draft/ready` |
| `lines` | `list[Line]` | 见 3.4.3 |
| `sfx_events` | `list[SfxEvent]` | |
| `bgm` | `BgmPlan \| None` | |
| `visuals` | `list[Visual]` | |
| `animations` | `list[Animation]` | |
| `silence_events` | `list[SilenceEvent]` | |
| `revision` | `Revision` | ≥1 |

不变式：`lines`、`sfx_events`、`visuals`、`animations`、`silence_events` 各列表内 `index` 从 1 连续；子对象 ID 前缀与类型一致（`lin_`/`sfx_`/`img_`/`vid_`/`sil_`）。

#### 3.4.3 Line — 台词行（实体）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `LineId` | `lin_` |
| `index` | `int` | ≥0，镜头内连续 |
| `kind` | `LineKind` | `dialogue/narration/inner_monologue` |
| `speaker_id` | `CharacterId` | 必须存在；旁白 `chr_narrator`，未识别 `chr_unknown` |
| `voice` | `LineVoiceOverride \| None` | 行级覆盖：`provider_id`/`voice_id`/`variant_id`/`speed`/`reason`/`emotion_map` |
| `text` | `str` | 1~4 000 字，去空白后非空 |
| `emotion` | `Emotion` | 10 值枚举 |
| `emotion_intensity` | `float` | [0,1] |
| `paralinguistic` | `list[Paralinguistic]` | `laugh/sigh/breath/cry/whisper`，去重 |
| `target_duration_ms` | `int \| None` | ≥0 |
| `speed` | `float` | 0.5~2.0 |
| `pause_before_ms` / `pause_after_ms` | `int` | 0~10 000 |
| `delivery_note` | `str \| None` | ≤500 字 |
| `anchor` | `Anchor` | 必填 |
| `tts` | `TtsState` | 见 3.4.5 |
| `revision` | `Revision` | ≥1 |

不变式（都是当前散落各处的规则，收进实体）：

1. `kind=dialogue` ⇒ `speaker_id != chr_narrator`；`kind=narration` ⇒ `speaker_id == chr_narrator`。
2. `text` 变更 ⇒ `revision + 1`，并把 `tts` 置 `stale`（派生判定，不写盘，见 3.4.5）。
3. `paralinguistic` 含 `whisper` 时 `emotion` 建议为 `whisper`（只告警不阻断，当前 `routing` 的降级语义）。
4. `anchor.state=orphan` 的行不得进入配音（当前由 `line_state` 隐式跳过，重构后显式化）。

方法：`retarget_text(text)`、`change_speaker(id)`、`retag_emotion(e, intensity)`、`apply_take(take)`、`mark_tts_failed(error)`。

#### 3.4.4 值对象：Anchor / Position / Transition

| 值对象 | 字段 | 校验 |
| --- | --- | --- |
| `Anchor` | `block_id`、`char_start`、`char_end`、`text_hash`、`line_id?`、`state`(`exact/shifted/orphan`) | `block_id` 必须存在于章节 blocks；`text_hash` 必须与 `char_start..char_end` 的实际文本一致；`shifted` 表示字符区间修正过、`orphan` 表示找不到对应块 |
| `Position` | `mode`(`before_line`/`after_line`/`during_line`/`shot_start`/`shot_end`)、`line_id?`、`offset_ms` | `mode ∈ {before_line,after_line,during_line}` ⇒ `line_id` 必填；`offset_ms` ±600 000 |
| `Transition` | `type`、`duration_ms` | `duration_ms` 0~10 000 |

`Anchor` 是本次重构收益最直接的值对象：`director/markup.py` 里 200 行游标纠偏逻辑（`_anchor`/`_orphan_anchor`/`_normalized_map`）全部围绕它，收进值对象后可单测穷举「精确 / 漂移 / 失配」三态。

#### 3.4.5 值对象：TtsState / RoutingDecision / TtsChunk

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `status` | `PersistedTtsStatus` | 只允许 `pending/queued/running/ready/failed`；`stale`/`skipped` 是派生态，**不得写盘** |
| `asset_id` | `AssetPath \| None` | `ready` 时必填且文件存在 |
| `duration_ms` | `int \| None` | `ready` 时必填，必须为实测值 |
| `synthesized_revision` | `int \| None` | 与 `Line.revision` 比较判 stale（旧侧车兜底路径） |
| `provider_id` / `model_version` | `str \| None` | 实际使用的 provider 与模型版本 |
| `voice_id` / `variant_id` | `VoiceId` / `VariantId` | 实际使用的音色 |
| `routing` | `RoutingDecision \| None` | 见下 |
| `chunks` | `list[TtsChunk]` | 长行切块，索引 0 基连续 |
| `error` | `TtsError \| None` | `code/retryable/hint/message` |
| `updated_at` | `Timestamp \| None` | |

`RoutingDecision`：`mode`(`auto/locked_line/locked_character/locked_project/manual`)、`policy_revision`、`input_hash`、`selected{provider_id,variant_id,score,reasons}`、`rejected[{provider_id,stage,reason,detail}]`、`degradations[{kind,from,to,detail,reason}]`、`decided_at`。
不变式：`mode=auto` ⇒ `input_hash` 非空（同输入必须复用同一决策）；`selected.provider_id` 必须出现在候选集中；`rejected[].stage ∈ availability/license/capability/variant/resource/score`。

`TtsChunk`：`index`、`text`(≤1 000)、`asset_id`、`duration_ms`、`provider_id`、`variant_id`、`status`(`pending/ready/failed`)。

#### 3.4.6 SfxEvent / SilenceEvent / Visual / Animation / BgmPlan（实体）

| 对象 | 关键字段 | 枚举/校验 |
| --- | --- | --- |
| `SfxEvent` | `id`(`sfx_`)、`index`、`kind`、`prompt`(≤1 000)、`prompt_en`(≤1 000)、`duration_ms`(100~600 000)、`loop`、`gain_db`(-60~12)、`fade_in_ms`/`fade_out_ms`(≤60 000)、`position`、`anchor`、`asset`、`revision` | `kind ∈ ambience/foley/impact/transition/music_sting`；`kind=ambience` ⇒ `loop=true`；`asset.status ∈ pending/queued/running/ready/failed/skipped`；`prompt_en` 非空（当前工厂函数注释已要求「缺失时调用方先报错」，改为构造校验） |
| `SilenceEvent` | `id`(`sil_`)、`index`、`duration_ms`(50~30 000)、`position`、`anchor`、`reason` | 停顿只能靠切分与后期插静音，Qwen3-TTS 无 SSML |
| `Visual` | `id`(`img_`)、`index`、`shot_type`、`composition`、`character_ids[]`、`action`、`style`、`prompt`(≤6 000)、`negative_prompt`、`aspect_ratio`、`width/height`、`workflow_template_id`、`anchor`、`draw`、`candidates[]`、`status`、`revision` | `shot_type ∈ 特写/中景/全景/空镜`；`aspect_ratio` 五档；`character_ids` 必须存在；`status ∈ pending/queued/generating/candidates_ready/selected/failed/stale` |
| `Animation` | `id`(`vid_`)、`index`、`visual_id`、`mode`(`i2v/t2v`)、`duration_ms`(500~5 000)、`motion`、`camera_move`、`prompt`、`audio_policy`(`keep/mute/duck`)、`fallback_visual_id`、`anchor`、`draw`、`candidates[]`、`status`、`revision` | `mode=i2v` ⇒ `visual_id` 必填；`camera_move ∈ static/slow_push_in/pan_left/pan_right/handheld`；`status` 多一个 `skipped` |
| `BgmPlan` | `id`(`bgp_`)、`scope`(`chapter/shot`)、`prompt_en`(1~1 000)、`narrative_function`、`mood_family`、`energy`/`intensity`([0,1])、`style`、`instrumentation[]`、`tempo_bpm`(20~220)、`duration_hint_ms`、`loop_required`、`duck_under_dialogue`、`source`(`llm/manual/rule`)、`status`(`planned/asset_ready/rendered/missing`)、`segments[]`、`revision` | `mood_family` 9 值；`scope=chapter` 时 `segments` 可为空 |

`Candidate`（抽卡候选，值对象）：`id`(`cnd_`)、`index`、`asset_id`、`job_id`、`seed`、`params_hash`、`status`(`candidate/selected/rejected/expired/failed`)、`created_at`、`expires_at`、`width/height`、`duration_ms`(≤5 000)、`error`。同一个 `Visual` 下最多一个 `selected`。

### 3.5 生产上下文（production）

#### 3.5.1 Job — 生成/配音任务（实体，聚合根）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `JobId` | `job_` |
| `kind` | `JobKind` | `writing/tts/sfx/image/video/mix/export` |
| `target` | `JobTarget`（VO） | 至少一个 ID；键类型受 `kind` 约束（见下） |
| `status` | `JobStatus` | `queued/running/succeeded/failed/canceled/interrupted` |
| `priority` | `int` | ≤999；约定 writing/tts 100、sfx 120、image 140、video 160、mix/export 200 |
| `idempotency_key` | `str` | `sha256(kind|target|revision|adapter|params_hash)`，构造时计算 |
| `params_hash` | `str \| None` | |
| `attempts` / `max_attempts` | `int` | `attempts ≤ max_attempts`（默认 3） |
| `depends_on` | `list[JobId]` | 依赖必须先完成 |
| `adapter` | `str \| None` | |
| `error` | `AdapterFailure \| None` | `code/message/retryable/detail` |
| `artifacts` | `list[Artifact]` | `{kind,path,meta}` |
| `timings` | `JobTimings`（VO） | `queued_at/started_at/finished_at/duration_ms` |
| `logs_path` | `str \| None` | |
| `created_at` | `Timestamp` | |

`JobTarget` 允许的键（按 kind）：

| kind | 必填键 | 可选键 |
| --- | --- | --- |
| `writing` | `chapter_id` | `act_id`、`line_id` |
| `tts` | `line_id` | `chapter_id`、`scene_id` |
| `sfx` | `sfx_id` | `chapter_id`、`scene_id` |
| `image` | `image_id` | `chapter_id`、`scene_id` |
| `video` | `video_id` | `chapter_id`、`scene_id` |
| `mix` / `export` | `chapter_id` | `timeline_id` |

不变式：状态只能 `queued → running → succeeded|failed|canceled|interrupted`，终态不可再迁移（重跑必须新建 Job）；`attempts` 超过 `max_attempts` 必须落 `failed`；`succeeded` 时 `artifacts` 非空或显式标记为空产物。
方法：`start()`、`succeed(artifacts)`、`fail(error)`、`interrupt()`、`retry()`（返回新 Job 或原地 `attempts+1`，二选一在实现阶段定）。

#### 3.5.2 ModelCallRecord — 模型调用记录（实体，只追加）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | `str` | `lg_` |
| `ts` | `Timestamp` | |
| `kind` | `str` | 固定 `llm` |
| `action` | `str` | `llm.chat` |
| `label` | `str` | 中文展示名 |
| `trace_id` | `str` | 关联一次操作 |
| `project` | `str` | 工程路径 |
| `target` | `str` | 模型 + 操作名 |
| `summary` | `str` | 流式/非流式 + 提示摘要 |
| `status` | `CallStatus` | `ok`/`partial`/`error`/`canceled` |
| `duration_ms` | `int` | |
| `first_token_ms` | `int \| None` | 流式首字延迟 |
| `usage` | `TokenUsage`（VO） | `prompt_tokens`/`completion_tokens`，上游未返回时为空并记 `usage_note` |
| `reason` | `AbortReason \| None` | `natural_end/user_cancel/page_unload/sse_reconnect/degeneration/watchdog/content_retry/superseded/timeout/unknown` |
| `error` | `str` | ≤4 000 字，已脱敏 |
| `detail` | `dict` | 请求/响应片段，已脱敏 |

不变式：只追加不修改；密钥类字段绝不落盘（沿用 `oplog` 现有脱敏）；`status=partial` 必须带 `reason`。

#### 3.5.3 LineTake — 配音产物侧车快照（值对象，旁挂）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `line_id` / `revision` | `LineId` / `Revision` | 身份 |
| `params` | `TakeParams`（VO） | `text/emotion/emotion_intensity/speed/delivery_note/target_duration_ms/instruct/speaker_id/provider_id/model_version/voice_id/variant_id/chunk_*` |
| `fingerprint` | `str` | `params` 归一化后的哈希，判 stale 的唯一依据 |
| `created_at` | `Timestamp` | |

不变式：`fingerprint` 必须由 `params` 重算得出；快照只读，重跑生成新快照。`stale` 判定 = `line_fingerprint(current_params) != snapshot.fingerprint`，这条规则从 `tts/line_state.py` 收进 `domain/script/rules/line_state.py`。

#### 3.5.4 Timeline — 时间轴（读模型，派生）

不作为实体，也不进聚合。它由 `Chapter + Script + LineTake + ProductionSettings` 纯函数计算得出，字段与现有 `timeline.schema.json` 一致。只保留一条规则：**同输入必须同输出**（`compute_source_hash` 一致即视为可复用），不需要封装成对象，只要一个 `build_timeline()` 领域服务函数。

### 3.6 枚举清单（替代裸字符串）

| 枚举 | 取值 | 使用位置 |
| --- | --- | --- |
| `ChapterStatus` | draft, written, split(过渡保留), scripted, voiced, visualized, composed, stale | Chapter |
| `ScriptStatus` | draft, ready, stale | Script |
| `ShotStatus` | draft, ready | Shot |
| `LineKind` | dialogue, narration, inner_monologue | Line |
| `Emotion` | neutral, joy, sad, angry, fear, surprise, tender, excited, whisper, serious | Line、VoiceVariant.emotion_tags、VoiceBinding.emotion_map |
| `Paralinguistic` | laugh, sigh, breath, cry, whisper | Line |
| `PersistedTtsStatus` | pending, queued, running, ready, failed | TtsState（派生态 stale/skipped 不入枚举的落盘集合） |
| `DerivedTtsStatus` | pending, stale, failed, ready, skipped | 展示与筛选（`line_state`） |
| `CharacterRole` | protagonist, antagonist, supporting, narrator, minor | Character |
| `Gender` | male, female, other, unknown | Character、Voice、VoiceProfile |
| `AgeFeel` | child, teen, young_adult, adult, middle_aged, elderly | VoiceProfile、VoicePoolSlot |
| `VoiceSource` | preset, cloned, recorded, designed | VoiceVariant |
| `VariantStatus` | ready, pending, unusable | VoiceVariant |
| `VoiceStatus` | ready, unusable, archived | Voice |
| `PresetVoiceName` | Vivian, Serena, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee | VoiceVariant |
| `PovMode` | first, second, third_limited, third_omniscient | Chapter、ChapterPlan、StyleSpec |
| `Tense` | past, present | StyleSpec |
| `TimeOfDay` | dawn, morning, day, dusk, night, unknown | Shot |
| `TransitionType` | cut, fade, dissolve, none | Transition |
| `SfxKind` | ambience, foley, impact, transition, music_sting | SfxEvent |
| `PositionMode` | before_line, after_line, during_line, shot_start, shot_end | Position |
| `AnchorState` | exact, shifted, orphan | Anchor |
| `VisualStatus` | pending, queued, generating, candidates_ready, selected, failed, stale | Visual |
| `AnimationStatus` | 同上 + skipped | Animation |
| `AudioPolicy` | keep, mute, duck | Animation |
| `BgmMoodFamily` | calm, tense, sad, romantic, mysterious, action, comedic, epic, horror | BgmPlan |
| `JobKind` | writing, tts, sfx, image, video, mix, export | Job |
| `JobStatus` | queued, running, succeeded, failed, canceled, interrupted | Job |
| `ContinuationMode` | continue, outline_driven, rewrite_range | ContinuationSession |
| `ContinuationStatus` | pending, running, done, failed, canceled | ContinuationSession |
| `RoutingMode` | auto, locked_line, locked_character, locked_project, manual | RoutingDecision |
| `CallStatus` | ok, partial, error, canceled | ModelCallRecord |
| `AbortReason` | natural_end, user_cancel, page_unload, sse_reconnect, degeneration, watchdog, content_retry, superseded, timeout, unknown | ModelCallRecord |

### 3.7 标识类型（`NewType`，运行时仍是 str）

`WorkId`(`wrk_`)、`ChapterId`(`chp_`)、`ActId`(`act_`)、`BeatId`(`bea_`)、`WorldId`(`wld_`)、`CharacterId`(`chr_`)、`VoiceId`(`voc_`)、`VariantId`(`vav_`)、`NarratorPersonaId`(`npr_`)、`ScriptId`(`scr_`)、`ShotId`(`sht_`)、`LineId`(`lin_`)、`SfxId`(`sfx_`)、`ImageId`(`img_`)、`VideoId`(`vid_`)、`BgmPlanId`(`bgp_`)、`SilenceId`(`sil_`)、`CandidateId`(`cnd_`)、`BlockId`(`blk_`)、`ContinuationId`(`cnt_`)、`ThreadId`(`thr_`)、`JobId`(`job_`)。

不变式：前缀与实体类型必须一致；本文档与 `models/ids.py` 的 `new_id(prefix)` 是同一份来源。

---

## 4. 聚合与边界

### 4.1 聚合清单

| 聚合根 | 内部实体/值对象 | 强一致范围 |
| --- | --- | --- |
| `Work` | Work、ProjectSettings、ProductionSettings | 作品元数据与设置；一次 `save_project()` 全量落盘 |
| `StoryBible` | StoryBible、Act、ChapterPlan、Beat、Premise/Style/Continuity | 大纲整体；一次 `save_bible()` |
| `WorldLibrary` | World | 世界条目库 |
| `Chapter` | Chapter、ChapterBlock、ContinuationSession、ImportSnapshot、Scene(v1) | 正文 + 版本 + 段落块 + 续写会话 |
| `Cast` | Character、CharacterState、VoiceBinding、VoiceProfile、SpeechStyle、StockSlot | 整张角色表（名字唯一、关系双向、合并迁移） |
| `VoiceLibrary` | Voice、VoiceVariant、VoiceReference、Consent、PromptCache | 音色与其变体 |
| `Script` | Script、Shot、Line、Anchor、TtsState、SfxEvent、SilenceEvent、Visual、Animation、BgmPlan | 一章节的台本整体 |
| `Job` | Job、JobTarget、Artifact、JobTimings | 单个任务的生命周期 |
| `ModelCallLog` | ModelCallRecord | 只追加 |

### 4.2 必须一起变更（同一聚合、同一次保存）

| 场景 | 为什么必须原子 |
| --- | --- |
| 章节正文 ⇄ 段落块 ⇄ revision ⇄ 历史版本 | 块是正文的派生索引，正文一变块必失效；版本归档必须基于「旧快照 + 新内容」成对完成，否则会丢版本 |
| 章节正文 ⇄ `synopsis` / `outline` | 反推大纲与摘要是正文的摘要视图，正文变更后不同步就会让后续上下文用错摘要 |
| 角色名 ⇄ 别名集合 ⇄ 引用 | 改名必须同时更新别名与所有引用（台本 speaker、大纲 character_ids、章节 voice_overrides），否则出现「说话人指向不存在角色」 |
| 角色 ⇄ 关系双向记录 | A→B 有 `relations` 而 B→A 无记录，会让关系图与提示词注入不一致 |
| 音色 ⇄ 变体 ⇄ default_variant_id | 删变体不改默认值，会让绑定指向不存在的变体 |
| 台本 ⇄ 镜头 ⇄ 台词行 ⇄ 锚点 | 锚点的 `block_id`/`text_hash` 依赖章节块；台词行 index、镜头 index 必须连续，单个改动会波及顺序 |
| 任务 ⇄ attempts ⇄ artifacts ⇄ timings | 任务状态机与产物列表必须同一次落盘，否则重启后出现「成功但无产物」 |
| 音色绑定变更 ⇄ 行 stale 判定 | 不是强一致：绑定是角色聚合，stale 是派生读；只需保证「读的时候按当前绑定重算」，不要求写回 |

### 4.3 可以独立变更

| 对象 | 独立理由 |
| --- | --- |
| `WorldLibrary` | 世界条目只被大纲按 ID 引用，改动不影响章节正文 |
| `VoiceLibrary` | 与角色解耦，只被 `VoiceBinding` 按 ID 引用；新增变体不影响已有台词行（是否 stale 由解析后的音色决定） |
| `Job` | 每个任务独立生命周期，队列快照整体落盘但语义上互不依赖（除 `depends_on`） |
| `ModelCallRecord` | 只追加，不参与任何一致性 |
| `Timeline` | 纯派生，随时可重建 |
| `ChapterPlan` 的 `cast_synced*` 标记 | 只影响是否补跑识别，不影响正文 |

### 4.4 跨聚合只通过标识引用

```
Chapter ──chapter_id──> Script ──speaker_id──> Cast.Character
   │                        │                      │
   │                        └──voice_id──> VoiceLibrary.Voice ──variant──> VoiceVariant
   └──pov_character_id──> Cast.Character
StoryBible.ChapterPlan ──character_id──> Cast.Character
StoryBible.ChapterPlan ──world_id──> WorldLibrary.World
Job.target ──ids──> Chapter / Script / Scene
```

禁止：`Chapter` 持有 `Character` 对象、`Script` 持有 `Voice` 对象、`Job` 持有 `Chapter` 对象。跨聚合一致性由应用服务在一次用例里读多个聚合、改一个、必要时再改第二个。

### 4.5 跨聚合的最终一致（应用服务负责）

| 链条 | 触发 | 处理 |
| --- | --- | --- |
| 正文变更 → 台本 stale | `Chapter.rewrite_content()` | 应用服务比对 `script.source.chapter_revision/content_hash`，不匹配则 `Script.mark_stale()`（写另一聚合，第二次保存） |
| 正文变更 → 配音 stale | `Chapter.revision` 变化 | 不写盘：`line_state` 读时按 `LineTake.fingerprint` 现算 |
| 角色改名 → 台本/大纲引用 | `Cast.rename()` | 应用服务迁移 `Script` 的 `speaker_id`（ID 不变则无需迁移）与大纲里的姓名文本（事实性文本需人工确认，不自动改） |
| 角色音色重绑 → 行 stale | `Cast.bind_voice()` | 不写盘，读时重算 |
| 新音色入库 → 角色绑定 | voice design 完成 | 应用服务：`VoiceLibrary.save()` → `Cast.bind_voice()` → `Script` 行 stale 自动派生 |
| 台本就绪 → 入队配音 | `Script.status=ready` | 应用服务逐行生成 `Job`，任务与台本分属不同聚合 |

---

## 5. 分层与职责

### 5.1 四层职责

| 层 | 目录 | 放什么 | 禁止 |
| --- | --- | --- | --- |
| 领域层 | `domain/` | 实体、值对象、枚举、领域服务（纯规则）、领域异常 | 不读文件、不连网络、不 import 其他三层；不出现 `Path`、`httpx`、`json.dump` |
| 应用服务层 | `application/` | 用例编排：读聚合 → 调领域方法 → 调适配器 → 保存；事务边界；进度与取消 | 不写「规则本身」（规则必须在 domain）；不拼 SQL/HTTP 细节；不 import `interface` |
| 适配器层 | `adapters/` | LLM、TTS、SFX/BGM、ComfyUI、ffmpeg、持久化、日志 | 不做业务判断、不跨适配器调用、不写领域状态 |
| 接口层 | `interface/` | HTTP 路由、CLI 命令、DTO 转换、参数校验（语法级） | 不写业务规则、不直接读写文件、不直接 import `adapters` 之外还需要领域细节 |

依赖方向（只允许向下）：

```
interface ──> application ──> domain
                   │
                   └──────> adapters ──> domain（只依赖值对象/异常，不依赖聚合）
```

说明：适配器**可以**依赖 `domain` 的值对象与异常（例如持久化 adapter 用 `Chapter.from_dict()` 构造聚合、用 `InvariantViolation` 报错），但不能反过来让 `domain` 依赖适配器。

### 5.2 允许的 import 矩阵

| 从 \ 到 | domain | application | adapters | interface | infrastructure |
| --- | --- | --- | --- | --- | --- |
| domain | ✅（同层） | ❌ | ❌ | ❌ | ❌ |
| application | ✅ | ✅（同层） | ✅ | ❌ | ✅ |
| adapters | ✅（仅 value_objects/errors） | ❌ | ✅ | ❌ | ✅ |
| interface | ✅（仅枚举与 DTO 映射） | ✅ | ❌（经 application 调用） | ✅ | ✅ |
| infrastructure | ❌ | ❌ | ❌ | ❌ | ✅ |

用脚本强制（`scripts/check_layering.py`，第 0 阶段就建），不靠自觉。

### 5.3 其他硬性约束

1. **禁止跨层塞逻辑**：HTTP 路由只做「解析参数 → 调 application → 组装响应」。当前 `web/app.py` 中 `_export_preflight`、`_resolve_voice_binding`、`_validate_config_patch` 这类带业务判断的函数必须下沉。
2. **禁止 interface → interface**：`web/actions.py` 现在「Web 调 CLI」是临时桥。迁移期允许保留，但**只允许未迁移命令走桥**，且每个命令迁移完成后必须删掉桥分支；第 6 阶段结束时 `actions.py` 删除。
3. **禁止适配器互调**：`tts` 适配器不能调 `sfx` 适配器；需要组合时在 application 里组合。
4. **禁止 application 直接 `open()` 工程文件**：一律经 `adapters/persistence` 的仓储。
5. **异常翻译只在接口层**：领域抛 `InvariantViolation`，应用层抛 `UseCaseError(code, message)`，接口层映射 HTTP 码 / CLI 退出码（沿用现有 0~6 语义）。
6. 单文件 ≤1 500 行；超过即拆，且拆分按「一个聚合 / 一个用例」而不是按「工具函数 / 私有 helper」。
7. **章节场景数据的读取归属**（阶段 6-3 起）：`chapter.scenes` 的唯一读入口是 `application/production/lines_source.py`（`read_scenes` / `chapter_view`）。`adapters/**` **不得自行读取** `chapter["scenes"]` 或调用 `load_chapter` 后遍历场景/台词——需要场景数据的适配器一律由 application 先派生好，再作为入参传入（只改函数签名，不改依赖方向）。适配器只负责「拿数据产出产物」，不回读工程结构。写点统一走步骤 3 的 `save_lines`，读路径拿到的派生视图不得回写章节。

---

## 6. 目录结构建议

```
src/novel_drama/
├── domain/                          # 领域层（无 I/O、无框架）
│   ├── shared/
│   │   ├── ids.py                   # 标识类型与 ULID 生成
│   │   ├── timestamps.py            # Timestamp / Revision / DurationMs
│   │   ├── enums.py                 # 跨上下文共享枚举（Emotion/Gender/AgeFeel）
│   │   ├── errors.py                # InvariantViolation / DomainError
│   │   └── text.py                  # 文本归一化、字符区间、哈希（供 VO 复用）
│   ├── story/
│   │   ├── entities/{work,bible,act,chapter_plan,beat,chapter,world}.py
│   │   ├── value_objects/{premise,style,continuity,origin,block,import_snapshot,continuation}.py
│   │   └── rules/{chapter_lifecycle,outline,status_transition}.py
│   ├── cast/
│   │   ├── entities/{cast,character,voice,voice_variant,narrator_persona}.py
│   │   ├── value_objects/{voice_binding,voice_profile,speech_style,stock_slot,state,relation}.py
│   │   └── rules/{uniqueness,voice_resolution,cast_validation}.py
│   ├── script/
│   │   ├── entities/{script,shot,line,sfx_event,silence_event,visual,animation,bgm_plan}.py
│   │   ├── value_objects/{anchor,position,transition,tts_state,routing_decision,tts_chunk,candidate}.py
│   │   └── rules/{anchor,line_state,markup_parser,splitting}.py
│   └── production/
│       ├── entities/{job,model_call}.py
│       ├── value_objects/{job_target,artifact,timings,adapter_failure,take_snapshot}.py
│       └── rules/{idempotency,retry_policy,priority,timeline}.py
├── application/                     # 应用服务层（用例编排）
│   ├── story/
│   │   ├── generate_outline.py
│   │   ├── apply_outline.py
│   │   ├── generate_chapter.py
│   │   ├── continue_chapter.py
│   │   ├── edit_chapter.py
│   │   ├── summarize_chapter.py
│   │   └── world.py
│   ├── cast/
│   │   ├── capture_cast.py
│   │   ├── sync_cast.py
│   │   ├── merge_cast.py
│   │   ├── voice_design.py
│   │   └── voice_cast.py
│   ├── script/
│   │   ├── annotate.py
│   │   ├── materialize.py
│   │   ├── split_lines.py
│   │   └── resplit.py
│   ├── voice/
│   │   ├── synthesize_line.py
│   │   ├── batch_synthesize.py
│   │   └── fill_voice.py
│   ├── production/
│   │   ├── enqueue_job.py
│   │   ├── run_job.py
│   │   ├── gpu_queue.py
│   │   └── produce_pipeline.py
│   └── support/
│       ├── context_assembler.py
│       ├── thinking_policy.py
│       ├── degeneration_guard.py
│       └── cancel.py
├── adapters/                        # 适配器层
│   ├── llm/{base,omlx,offline}.py
│   ├── tts/{base,registry,providers/*.py}
│   ├── sfx/{base,registry,providers/*.py}
│   ├── bgm/{base,registry,providers/*.py}
│   ├── comfyui/{gateway,workflow,models,media}.py
│   ├── media/{ffmpeg,mixer,mux,concat,jianying,export,timeline_io}.py
│   ├── persistence/
│   │   ├── project_store.py
│   │   ├── chapter_store.py
│   │   ├── bible_store.py
│   │   ├── cast_store.py
│   │   ├── voice_store.py
│   │   ├── script_store.py
│   │   ├── queue_store.py
│   │   ├── imports_store.py
│   │   ├── groups_store.py
│   │   ├── schema_validator.py
│   │   └── mappers/{chapter,cast,voice,script,job}.py
│   └── observability/{oplog.py,model_call_recorder.py}
├── interface/
│   ├── http/
│   │   ├── app.py                   # 组装：路由注册、异常映射
│   │   ├── routers/{story,cast,voice,script,media,models,logs,jobs,config}.py
│   │   └── dto/{requests,responses}.py
│   └── cli/
│       ├── main.py
│       └── commands/{setting,outline,chapter,cast,voice,line,script,media,produce,models,comfy,world}.py
└── infrastructure/
    ├── config.py
    ├── paths.py
    └── logging.py
```

每个目录的职责：

| 目录 | 职责 | 判定标准（放错了吗） |
| --- | --- | --- |
| `domain/shared` | 跨上下文的标识、时间、枚举、错误、文本工具 | 出现任何 `Path`/`open`/`httpx` 就是放错了 |
| `domain/<ctx>/entities` | 有身份、有生命周期、有状态迁移的对象 | 能写 `save` 就是放错了（保存属于 adapter） |
| `domain/<ctx>/value_objects` | 不可变、构造即校验 | 需要 ID 就是放错了 |
| `domain/<ctx>/rules` | 跨实体/跨值对象的纯规则（解析、判定、状态机） | 需要读文件就是放错了 |
| `application/<ctx>` | 一个文件一个用例；读聚合、调领域、调适配器、保存 | 出现大段 if/else 业务判定就是规则没下沉 |
| `adapters/*` | 外部世界：模型、ffmpeg、文件、日志 | 出现「该不该重试」「状态该不该变」就是越界 |
| `adapters/persistence/mappers` | 实体 ⇄ JSON dict，保持 schema 形状 | 出现业务规则就是越界 |
| `interface/http/routers` | 路由与 DTO | 出现文件读写或状态计算就是越界 |
| `infrastructure` | 配置、路径、日志基建 | 被 domain/application 反向依赖就是设计错了 |

---

## 7. 现有模块 → 新结构映射

### 7.1 领域与规则类迁移

| 现有模块（行数） | 去向 | 处理 |
| --- | --- | --- |
| `models/entities.py`（409，dict 工厂） | `domain/*/entities/*.py` | **废弃**，逐实体替换 |
| `models/ids.py`（~60） | `domain/shared/ids.py` | 直接迁移 |
| `models/duration.py` | `domain/shared/timestamps.py` + `domain/production/rules/timeline.py` | 拆分（时长估算属规则） |
| `tts/line_state.py`（593） | `domain/script/rules/line_state.py`（纯判定）+ `application/voice/line_state.py`（回滚写盘） | **拆分**：`resolve_line_state`/`diff_params` 是纯规则；`revert_line` 含 I/O |
| `tts/routing.py`（405） | `domain/cast/rules/routing.py`（硬过滤/打分/确定性决策）+ `application/voice/route.py` | **拆分** |
| `tts/fingerprint.py` | `domain/production/value_objects/take_snapshot.py` | 合并 |
| `store/voice_cast.py`（1164） | `domain/cast/rules/voice_resolution.py`（10 级优先级）+ `domain/cast/rules/cast_validation.py` + `application/cast/voice_cast.py`（章节覆盖读写） | **三拆** |
| `writing/cast_sync.py`（748） | `domain/cast/rules/normalize.py` + `application/cast/sync_cast.py` | **拆分** |
| `writing/degeneration.py`（397） | `domain/script/rules/degeneration.py` | 迁移（纯文本判定） |
| `text/splitter.py`（1262） | `domain/script/rules/splitting.py` | 迁移；超 1500 前已需按「场景切分 / 引语扫描 / 说话人归属」三拆 |
| `text/dialogue.py`、`names.py`、`titles.py` | `domain/shared/text.py` + `domain/script/rules/` | 合并 |
| `director/markup.py`（888） | `domain/script/rules/markup_parser.py` | 迁移（纯解析，输出领域对象） |
| `media/timeline_builder.py`（430） | `domain/production/rules/timeline.py`（计算）+ `adapters/media/timeline_io.py`（读写） | **拆分** |
| `character_merge.py`（526） | `domain/cast/entities/cast.py` 的 `merge()` + `application/cast/merge_cast.py` | **拆分**（规则进聚合，引用迁移进用例） |
| `models/entities.py::speaker_display_name` | `domain/cast/rules/display.py` | 迁移 |

### 7.2 用例类迁移

| 现有模块（行数） | 去向 | 处理 |
| --- | --- | --- |
| `writing/service.py`（5546） | `application/story/{generate_outline,apply_outline,generate_chapter,continue_chapter,summarize_chapter,edit_chapter}.py`、`application/cast/{capture_cast,sync_cast}.py`、`application/script/annotate.py`、`domain/cast/rules/normalize.py` | **拆成 ≥10 个文件**（当前一个文件同时管 7 个上下文，是全仓最大结构问题） |
| `writing/expand.py`（1280） | `application/story/outline_expand.py` | 迁移 |
| `writing/outline_hier.py`（1123） | `application/story/outline_hierarchical.py` | 迁移 |
| `writing/outline_edit.py`（624） | `application/story/outline_edit.py` | 迁移 |
| `writing/continuation.py`（1097） | `application/story/continue_chapter.py` | 迁移（与 `service.generate_chapter` 的去重另议） |
| `writing/editing.py`（551） | `application/story/edit_chapter.py` | 迁移 |
| `writing/summarize.py`（612） | `application/story/summarize_chapter.py` | 迁移 |
| `writing/consistency.py`、`content_guard.py` | `application/story/consistency.py` | 合并 |
| `writing/cast_capture.py`（883） | `application/cast/capture_cast.py` | 迁移 |
| `writing/character_plan.py`（682）、`character_updates.py`（342） | `application/cast/character_plan.py` | 合并 |
| `writing/act_batch.py`（422） | `application/story/act_batch.py` | 迁移 |
| `writing/worlds.py`（467） | `application/story/world.py` | 迁移 |
| `writing/budgets.py`、`thinking.py`、`watchdog.py` | `application/support/{thinking_policy,watchdog}.py` | 合并 |
| `writing/chapter_checkpoint.py`、`recap.py` | `application/story/generate_chapter.py` 内部 | 合并 |
| `tts/service.py`（723） | `application/voice/synthesize_line.py` | 迁移 |
| `tts/batch.py`（840）、`voice_batch.py`（724）、`voice_fill.py`（1280） | `application/voice/{batch_synthesize,fill_voice}.py` | **合并 + 三拆**（三者职责重叠，需在实现阶段先确认边界） |
| `tts/voice_casting.py`（678）、`cast_autofill.py`（625） | `application/cast/voice_cast.py` | 合并 |
| `tts/voice_design.py`（1191） | `application/cast/voice_design.py` | 迁移 |
| `tts/cache.py`、`reuse.py`、`emotion_map.py`、`builtin_voices.py` | `domain/voice` 值对象 + `adapters/tts` | 拆分 |
| `director/materialize.py` | `application/script/materialize.py` | 迁移（跨聚合写章节，属应用层） |
| `orchestration/jobs.py` | `application/production/{enqueue_job,run_job}.py` + `domain/production/rules/{idempotency,retry_policy}.py` | **拆分** |
| `orchestration/pipeline.py` | `application/script/split_lines.py` + `domain/script/rules/splitting.py` | **拆分**（当前把规则与入队混在一起） |
| `orchestration/gpu_queue.py`（392） | `application/production/gpu_queue.py` + `adapters/lock.py` | 拆分（锁文件属适配器） |
| `orchestration/produce.py`（589）、`resplit.py`（396） | `application/production/produce_pipeline.py`、`application/script/resplit.py` | 迁移 |

### 7.3 存储与适配器迁移

| 现有模块（行数） | 去向 | 处理 |
| --- | --- | --- |
| `store/project_store.py`（650） | `adapters/persistence/{project_store,chapter_store}.py` + `mappers/chapter.py` | **拆分**；返回实体而非 dict |
| `store/settings_store.py`、`world_store.py` | `adapters/persistence/bible_store.py`、`world` | 合并/迁移 |
| `store/voice_store.py`、`voice_cast.py` | `adapters/persistence/{voice_store,cast_store}.py` | 拆分 |
| `store/queue_store.py`、`imports_store.py`、`groups_store.py` | `adapters/persistence/*` | 迁移 |
| `store/schema_validator.py` | `adapters/persistence/schema_validator.py` | 迁移（仍是落盘契约的唯一校验点） |
| `adapters/base.py`（364） | `adapters/{llm,tts,sfx}/base.py` | **按能力拆分** |
| `adapters/writing.py`（2003） | `adapters/llm/{omlx,base}.py` | **拆分**；`_record_llm_call`/`_record_budget_stats` 与 `oplog` 重叠，统一到 `adapters/observability` |
| `adapters/offline_writing.py`（1327） | `adapters/llm/offline.py` | 迁移 |
| `adapters/tts.py`、`sfx.py`、`comfyui.py`（537） | `adapters/{tts,sfx,comfyui}/` | 迁移 |
| `tts/providers/*`、`sfx/providers/*`、`bgm/providers/*` | `adapters/{tts,sfx,bgm}/providers/*` | 直接迁移，改动 import |
| `tts/model_store.py`（1132）、`tts/sidecar.py`、`tts/alignment.py` | `adapters/tts/{model_store,sidecar,alignment}.py` | 迁移 |
| `media/*`（ffmpeg/mixer/mux/concat/export/jianying/denoise/normalize/bundle） | `adapters/media/*` | 迁移，保持纯命令拼装 |
| `media/comfy_*.py`（1283+573+423+349+…） | `adapters/comfyui/{models,media,workflow,generate}.py` | 迁移 |
| `oplog.py`（1441） | `adapters/observability/{oplog,model_call_recorder}.py` | **拆分**（记录结构进领域，读写留在适配器） |
| `config.py`（472） | `infrastructure/config.py` | 迁移 |

### 7.4 接口层迁移

| 现有模块（行数） | 去向 | 处理 |
| --- | --- | --- |
| `web/app.py`（6154，167 路由） | `interface/http/app.py` + `routers/{story,cast,voice,script,media,models,logs,jobs,config}.py` | **必拆**，按路由前缀切；`_export_preflight` 等业务函数下沉 application |
| `web/stream_api.py`（1049） | `interface/http/routers/stream.py` | 迁移；SSE 组装保留在接口层 |
| `web/voice_tools_api.py`（1562）、`models_api.py`（1507）、`align_api.py`、`sfx_api.py`、`bgm_api.py`、`character_desc_api.py`、`continue_api.py`、`outline_edit_api.py`、`logs_api.py`（466） | `interface/http/routers/*` | 迁移 |
| `web/job_tracker.py`（523） | `application/production/job_tracker.py` | 迁移（任务状态属应用层） |
| `web/llm_plan.py`（568） | `application/support/llm_plan.py` | 迁移（调用次数计算是业务口径） |
| `web/actions.py` | 删除 | **废弃**：迁移完成后不再 Web 调 CLI |
| `cli.py`（1854）+ 18 个 `cli_*.py` | `interface/cli/main.py` + `commands/*.py` | **拆分**，一个命令族一个文件 |

### 7.5 明确废弃

| 对象 | 原因 |
| --- | --- |
| `models/entities.py` 的 dict 工厂 | 被领域实体取代 |
| `web/actions.py` 的 CLI 桥 | 接口层不应互调 |
| `orchestration/pipeline.py::split_chapter_scenes/split_scene_lines` | 规则搬进 domain，入队搬进 application |
| `chapter.scenes`（v1 字段） | 第 5 阶段由 `Script.shots` 唯一承接；在此之前保留，不双写 |
| `tts/service.py` 的 `split_text`/`split_text_chunks` 两份切分 | 与 `text/splitter.py` 语义重叠，合并为一份 |
| `adapters/writing.py` 内部的调用记录逻辑 | 与 `oplog` 重复 |

---

## 8. 分阶段落地顺序

排序原则：**先建骨架与守卫 → 再改被依赖最多的实体（角色、章节）→ 再收规则 → 再改接口**。每阶段都是独立可交付、可回滚的，不留半成品。

### 阶段 0：骨架与守卫（不改任何业务行为）

| 项 | 内容 |
| --- | --- |
| 改动范围 | 新增 `domain/shared/*`（ids/timestamps/enums/errors/text）、`scripts/check_layering.py`、`tests/fixtures/` 里的实体往返样例；`docs/README.md` 增加本文索引 |
| 风险 | 极低。只新增文件，不改 import |
| 验收 | ① 分层检查脚本对现有代码零误报（现有代码全部在豁免名单）② 故意在 `domain/` 里 import `adapters` 能被脚本拦下 ③ 现有 `pytest` 全绿 ④ 记录基线：168 文件 / 78 782 行 / 超 1500 行文件清单 |

### 阶段 1：角色与章节实体（被最多模块依赖）

| 项 | 内容 |
| --- | --- |
| 改动范围 | 新增 `domain/cast/{entities/character,value_objects/*,rules/uniqueness}`、`domain/story/{entities/chapter,value_objects/block,import_snapshot}`；实现 `from_dict()` / `to_dict()`，**字段与现有 schema 一一对应**；暂不接入调用方 |
| 风险 | 中。字段映射遗漏会导致往返丢字段。缓解：写往返测试，用真实工程 `chapters/*.json`、`characters.json` 做样本，断言 `to_dict(from_dict(x)) == x`（除派生字段） |
| 验收 | ① 往返测试覆盖「真实工程样本 + 边界样本（空正文/超长/无锚点）」全部通过 ② `schema_validator` 校验 `to_dict()` 输出通过 ③ 现有 `pytest` 全绿 ④ 新文件均 ≤600 行 |

### 阶段 2：仓储改为返回实体（先章节，后角色）

| 项 | 内容 |
| --- | --- |
| 改动范围 | `adapters/persistence/mappers/chapter.py`、`cast.py`；`ProjectStore.load_chapter()` 返回 `Chapter`，`save_chapter(chapter)` 接收实体；调用方按需 `to_dict()` 过渡。提供 `Chapter.dict_view()` 只读视图给未迁移代码 |
| 风险 | **中高**。调用方大量就地改 dict（`chapter["status"] = ...`），改成实体后会抛 `TypeError`。缓解：① 一次只切一个聚合（先 chapter，跑全量测试与真实工程读写回归，再切 cast）② `dict_view()` 返回深拷贝只读副本，未迁移代码改它不会静默成功 ③ 不保留「实体可当 dict 用」的兼容层，避免半吊子状态 |
| 验收 | ① 真实工程 `chapters/`、`characters.json` 读→写→再读，`diff` 为 0（只允许 `updated_at` 变化）② 全量 `pytest` 全绿 ③ 全仓 `grep` 确认没有 `chapter["` 形式的就地写入残留（白名单逐条登记） |

### 阶段 3：领域规则下沉（角色、章节、台词行优先）

| 项 | 内容 |
| --- | --- |
| 改动范围 | ① `Chapter.rewrite_content()` 接管 revision/word_count/blocks/历史版本归档（替代 `ProjectStore.save_chapter` 的字符串比对）② `Cast` 接管名字/别名唯一性与 `merge()`（替代 `cast_sync` 的索引逻辑与 `character_merge` 的迁移）③ `Line` + `TtsState` + `LineTake` 接管 stale 判定（替代 `tts/line_state.resolve_line_state`）④ `VoiceBinding` + `resolve_voice()` 接管 10 级优先级（替代 `store/voice_cast`） |
| 风险 | **高**。这些规则现在有大量隐含行为（靠 revision 兜底、僵尸态降级、speaker 反推）。缓解：① 每组规则先写「规则表驱动测试」，用现有测试用例与真实工程数据作为黄金样本，新实现必须与旧实现逐条对齐后才删旧代码 ② 一次只下沉一组规则，四组分别提交 |
| 验收 | ① 四组规则各有参数化测试，覆盖正常 + 每条不变量 + 边界 ② 新实现与旧实现同时在真实工程上跑，输出逐字段比对一致 ③ 删除旧实现后全量 `pytest` 全绿 ④ `store/voice_cast.py`、`tts/line_state.py` 行数下降 ≥50% |

### 阶段 4：应用服务收口（story / cast 用例）

| 项 | 内容 |
| --- | --- |
| 改动范围 | `writing/service.py` 按 §7.2 拆成 `application/story/*` 与 `application/cast/*`；`interface/cli/commands/*` 改为调 application；Web 对已迁移用例改为直接调 application（不再经 `actions.py` 桥） |
| 风险 | 中高。`generate_chapter` 相关逻辑有断点续跑、退化熔断、看门狗、摘要合并等大量分支，拆分易漏。缓解：① 先做「机械搬迁」（只移动函数与 import，不改逻辑），单独验证 ② 再逐个用例整理职责边界 ③ 每个用例迁移后立即跑对应测试与真实工程最小跑通 |
| 验收 | ① `application/*` 下单文件 ≤800 行，无 `writing/service.py` ② CLI 命令输出与迁移前逐字节一致（对固定工程做快照对比）③ 全量 `pytest` 全绿 ④ Web 侧已迁移路由不再调用 `actions.run` |

### 阶段 5：台本 / 生产聚合（Scene → Shot 收口）

| 项 | 内容 |
| --- | --- |
| 改动范围 | 新增 `domain/script/*`、`domain/production/*`；`Script` 聚合接管镜头/台词/锚点；`Scene` 降级为 v1 只读；`Job` 聚合接管状态机与幂等键；`ModelCallRecord` 结构进领域、读写留适配器；`Timeline` 进 `domain/production/rules` |
| 风险 | **最高**。台词行现在同时存在于 `chapter.scenes` 与 `scripts/*.json`，切换归属会牵动配音、时间轴、导出、告警定位。缓解：① 先让两处读同一个实体（`Scene` 与 `Shot` 共用 `Line` 值映射），再切唯一写入点 ② 真实工程做「切前 / 切后」时间轴与导出包逐字节对比 ③ 保留 `chapter.scenes` 只读回填一个版本周期（未上线阶段可直接重建，但需先确认） |
| 验收 | ① `timelines/*.json` 切前切后一致 ② 配音产物路径与侧车字段一致 ③ 导出包 `exports/` 内容哈希一致 ④ `scene` 与 `shot` 不再双写（脚本检查禁止两条写入路径） |

### 阶段 6：接口层拆分与清理

| 项 | 内容 |
| --- | --- |
| 改动范围 | `web/app.py` 拆成 `interface/http/routers/*`；`cli*.py` 拆进 `interface/cli/commands/*`；删除 `web/actions.py`、`models/entities.py`、`orchestration/pipeline.py` 旧函数；`adapters/writing.py` 拆分并接入统一观测 |
| 风险 | 中。路由拆分易漏注册、前缀冲突。缓解：① 用「路由清单快照」对比拆分前后 167 条逐条一致 ② 前端 281 处 `/api` 调用与后端 203 条路由逐条比对（沿用 2026-11-15 收口整合的做法） |
| 验收 | ① 路由/命令清单与拆分前逐条一致 ② 全量 `pytest` + 前端模块语法检查通过 ③ 全仓无文件超 1500 行 ④ 分层检查脚本零豁免（除 `infrastructure`） |

### 阶段工时与依赖

依赖链：阶段 0 → 1 → 2 → 3；阶段 3 完成后 4 与 5 可并行（4 只依赖 story/cast，5 依赖 script/production）；阶段 6 最后。

---

## 9. 风险与回滚

| 风险 | 触发点 | 缓解 | 回滚 |
| --- | --- | --- | --- |
| 落盘格式漂移 | 阶段 1/2 的 `to_dict()` | 往返测试 + schema 校验 + 真实工程 diff | 实体只新增不改调用方，删除新文件即可 |
| 规则行为漂移 | 阶段 3 | 新旧实现并跑比对；规则表驱动测试 | 保留旧函数一个版本周期，开关切换 |
| 调用方大量报错 | 阶段 2 | 一次一个聚合；`dict_view()` 只读 | 单聚合回退（chapter 与 cast 独立） |
| 台本/场景双写混乱 | 阶段 5 | 先统一读、再切写；时间轴与导出逐字节对比 | 保留 `chapter.scenes` 写路径，`Script` 只读 |
| 目录重构打乱并行会话 | 全阶段 | 每次改动只动一个目录族；不同会话按目录分工 | 按阶段分支回滚，Stage 0~6 各自独立可停 |
| 文件拆分引入循环 import | 阶段 4/6 | 分层检查脚本 + `python -m compileall` + 导入图检查 | 拆分粒度回退到上一个可编译状态 |

---

## 10. 待确认问题

以下问题会影响实现阶段的具体做法，需在动工前确认：

1. **持久化契约**：是否确认 `schema/*.json` 继续作为唯一落盘契约，实体 `to_dict()` 严格对齐、不新增/重命名字段？（建议：确认。这样重构不改数据，风险最小）
2. **Scene 归属**：`chapter.scenes` 是否按方案在第 5 阶段废弃、由 `Script.shots` 唯一承接？还是保留两套、只统一实体类型？（建议：废弃。双份台词行是所有 stale 判定混乱的根源）
3. **Web 与 CLI 的关系**：是否确认迁移完成后 Web 直接调 application、删除 `web/actions.py` 桥？（建议：确认。接口层互调不符合分层目标）
4. **TTS 批处理边界**：`tts/batch.py`、`voice_batch.py`、`voice_fill.py` 三者合并前，需业务确认「整章批量 / 音色试听批量 / 补跑填充」是否真的是三个用例，还是同一用例的三种范围参数。
5. **任务重试语义**：`Job.retry()` 是「原地 attempts+1」还是「新建 Job」？当前 `max_attempts` 与幂等键的组合语义在 `orchestration/jobs.py` 与 `gpu_queue.py` 里不完全一致，需统一后写进实体。
6. **ModelCallRecord 落点**：是否维持 JSONL 追加（现状），还是进工程内结构化存储？影响是否把它建成正式实体。
7. **历史数据**：未上线阶段可直接重建工程数据，是否同意在阶段 5 用「重建」代替「双写兼容」？

---

## 11. 本轮产出与下一步

- 本文档：`docs/architecture-domain-model.md`
- 修改记录：已在 `项目修改记录文档.md` 登记（状态：待确认）
- 下一步：**等待确认**。确认后从阶段 0（骨架与守卫）开始，按阶段逐项交付与验证，不一次性大改。
