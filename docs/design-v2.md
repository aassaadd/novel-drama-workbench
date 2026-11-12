# 详细设计 v2：七工具工作台与导演脚本

| 项 | 内容 |
| --- | --- |
| 版本 | v2.0（设计定稿待评审） |
| 日期 | 2026-11-09 |
| 状态 | 详细设计；本轮不写实现代码 |
| 前置 | `docs/PRD.md` v1、`docs/data-model.md` v1、`docs/architecture.md` |
| 本文范围 | 工具边界、导演脚本格式、双向映射、续写、音色、抽卡、状态机、GPU 调度、UI、里程碑 |

**v1 与 v2 的关系**：v1 把「章节 → 场景 → 台词 → 音效/图/视频」全部内嵌在一份章节 JSON 里，适合先跑通链路；v2 引入**设定库**与**导演脚本**两层，让「写作」「配音」「图像」「视频」可以各自独立运行，并通过锚点与正文对齐。v1 的 `scene` 在 v2 中由 `shot`（镜头）承接，`line` 仍是配音与时间轴的最小单位。

---

## 1. 工具边界

七个工具共用一个工程目录与一套设定库，**各自可独立启动、独立重跑**。工具之间不直接调用，只通过落盘数据与任务队列交互。

```
                    ┌────────────────────────────┐
                    │  设定工坊（唯一写入设定库） │
                    └─────────────┬──────────────┘
                                  │ 设定库（只读快照 + revision）
        ┌───────────────┬─────────┴─────────┬───────────────┐
        ▼               ▼                   ▼               ▼
   小说生成        导演脚本            配音工具        图像/视频工具
        │               │                   │               │
        └──────► 正文 ◄─┴──► 导演脚本 ◄─────┴───────────────┘
                                  │
                                  ▼
                          合成导出（时间轴 + 混音）
```

| 工具 | 输入 | 输出 | 独立运行 | 共享方式 |
| --- | --- | --- | --- | --- |
| 设定工坊 | 用户录入、导入 txt/md、可选模型辅助 | `settings/bible.json`（世界观 + 大纲 + 文风 + 连续性）、`characters.json`、`voices.json` | 可单独使用（先建设定再写小说） | 写设定库，产出 `settings_revision` |
| 小说生成 | 设定库快照、章节大纲、前文摘要、续写思路 | `chapters/<id>.json`（正文 + 段落块 + 章节摘要） | 只写小说，不生成脚本 | 只读设定库，按 `settings_revision` 记录依赖 |
| 导演脚本 | 章节正文、设定库、角色表 | `scripts/<chapterId>.json`（场景/镜头/台词/音效/画面/动画/转场） | 可对已有正文单独重跑 | 只读正文与设定，写脚本 |
| 配音 | 导演脚本的台词行、角色音色绑定、音色库 | `assets/voice/<lineId>_r<rev>.wav`、行内实测时长 | 可只给一章/一个角色/一行配音 | 读脚本与音色库，回写行 `tts` |
| 图像 | 导演脚本的画面元素、角色外貌设定 | `assets/image/<visualId>_c<candidate>_r<rev>.png`、候选列表 | 可只抽某镜头的图 | 读脚本，写候选，不碰台词 |
| 视频 | 导演脚本的动画元素、选中的图（i2v 首帧） | `assets/video/<animationId>_c<...>_r<rev>.mp4`（≤5s） | 可只做指定动画 | 读脚本与图像候选 |
| 合成导出 | 导演脚本、选中产物、时间轴 | `timelines/`、`exports/`（MP4/MP3/SRT/manifest） | 可只混音或只导出 | 只读全部上游，写派生物 |

**共享设定库的约束**：

1. 只有设定工坊写 `settings/` 与 `characters.json`、`voices.json`；其他工具只读。
2. 每次设定变更 `settings_revision + 1`；章节、脚本记录其依赖的 `settings_revision`，不相等即标 `stale`。
3. 工具可以只运行一部分：例如没有配音产物时，图像工具仍可运行；合成工具对缺失产物用占位或跳过（见 §7）。

---

## 2. 导演脚本格式（核心）

导演脚本是**生产用中间层**：向上承接小说正文，向下供配音、图像、视频、合成读取。一个章节一份 `scripts/<chapterId>.json`，可被人工编辑。

### 2.1 文件结构

```jsonc
{
  "schema_version": 1,
  "id": "scr_01JBM90001",
  "chapter_id": "chp_01JBM7X3Q9",
  "work_id": "wrk_01JBM7X3Q9",
  "source": {
    "chapter_revision": 12,          // 生成时依赖的章节版本
    "settings_revision": 3,          // 生成时依赖的设定库版本
    "content_hash": "sha256:..."     // 章节正文哈希
  },
  "status": "draft",                 // draft|ready|stale
  "style": {
    "visual_style": "写实电影感，冷调，浅景深",
    "narration_voice_id": "voc_narrator_01",
    "bgm_mood": "低沉弦乐"
  },
  "shots": [ /* 见 2.2 */ ],
  "warnings": [],
  "created_at": "2026-11-09T12:00:00Z",
  "updated_at": "2026-11-09T12:00:00Z"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | integer | 是 | 脚本结构版本，从 1 开始 |
| `id` / `chapter_id` / `work_id` | string | 是 | ID 前缀分别为 `scr_` / `chp_` / `wrk_` |
| `source.*` | object | 是 | 生成依赖快照，用于增量判定（见 §3） |
| `status` | enum | 是 | `draft` 人工可编辑；`ready` 可下游消费；`stale` 上游已变需重算 |
| `shots[]` | array | 是 | 镜头列表，按播放顺序 |
| `warnings[]` | array | 否 | 生成期发现的问题（未识别说话人、锚点漂移等） |

### 2.2 镜头（shot）

```jsonc
{
  "id": "sht_01JBM90002",
  "index": 1,
  "scene_label": "深夜·警局走廊",
  "location": "警局走廊",
  "time_of_day": "night",            // dawn|morning|day|dusk|night|unknown
  "mood": "压抑",
  "goal": "沈砚发现卷宗缺页",
  "anchor": { /* 见 §3.1 */ },
  "transition_in": { "type": "cut", "duration_ms": 0 },
  "transition_out": { "type": "fade", "duration_ms": 600 },
  "lines": [ /* 见 2.3 */ ],
  "sfx_events": [ /* 见 2.4 */ ],
  "visuals": [ /* 见 2.5 */ ],
  "animations": [ /* 见 2.6 */ ],
  "silence_events": [ /* 见 2.7 */ ],
  "revision": 1
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` / `index` | string / integer | 是 | 镜头 ID 与顺序 |
| `scene_label` | string | 是 | 展示名，如「深夜·警局走廊」 |
| `location` / `time_of_day` / `mood` | string / enum / string | 否 | 供音效、画面提示词拼装 |
| `goal` | string | 否 | 本镜头戏剧目标 |
| `anchor` | object | 是 | 回指正文的锚点（§3.1） |
| `transition_in` / `transition_out` | object | 否 | `{type: cut|fade|dissolve|none, duration_ms}`；默认 `cut` |
| `lines[]` | array | 是 | 台词行，可空数组（纯画面镜头） |
| `sfx_events[]` | array | 否 | 音效事件 |
| `visuals[]` | array | 否 | 画面/关键图 |
| `animations[]` | array | 否 | 5 秒动画 |
| `silence_events[]` | array | 否 | 显式停顿/静音事件（§2.7）；Qwen3-TTS 无 SSML，停顿必须显式建模 |
| `revision` | integer | 是 | 镜头内容版本 |

### 2.3 台词行（line）

```jsonc
{
  "id": "lin_01JBM90003",
  "index": 1,
  "kind": "dialogue",                // dialogue|narration|inner_monologue
  "speaker_id": "chr_01JBM7X3Q9",
  "voice": {
    "provider_id": null,             // 行级 provider 覆盖；为空走角色 → 工程默认 → 回落链
    "voice_id": "voc_shen_yan_01",   // 音色库引用；为空表示用角色默认绑定
    "variant_id": null,              // 直接指定音色变体；为空按情绪映射解析
    "speed": 0.95,                   // 后期 atempo 变速系数（provider 无原生语速时）
    "emotion_map": { "serious": "serious_low" }
  },
  "text": "说重点。",
  "emotion": "serious",              // neutral|joy|sad|angry|fear|surprise|tender|excited|whisper|serious
  "emotion_intensity": 0.7,          // 0~1
  "speed": 0.95,                     // 0.5~2.0，后期变速
  "pause_before_ms": 200,
  "pause_after_ms": 400,
  "delivery_note": "压低声音",
  "anchor": { /* §3.1，必填 */ },
  "tts": {
    "status": "pending",             // pending|running|ready|failed|stale
    "asset_id": null,
    "duration_ms": null,
    "synthesized_revision": null,
    "provider_id": null,             // 实际使用的 provider（解析结果）
    "model_version": null,           // 实际模型版本，切换 provider 后重算依据
    "voice_id": null,
    "variant_id": null,              // 实际使用的变体，音色漂移排查依据
    "chunks": [],                    // 切块记录（index/text/asset_id/duration_ms/provider_id/variant_id/status）
    "error": null,
    "updated_at": null
  },
  "revision": 1
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `speaker_id` | string | 是 | 角色 ID；旁白固定 `chr_narrator`；未识别 `chr_unknown` |
| `voice.provider_id` | string | 否 | 行级 provider 覆盖；为空按角色 → 工程默认 → `provider_order` 回落（§5.5） |
| `voice.voice_id` | string | 否 | 优先于角色绑定；为空走 `characters[].voice_binding.voice_library_id` |
| `voice.variant_id` | string | 否 | 直接指定音色变体；为空时按「行情绪 → 角色 `emotion_variant_map` → 默认变体」解析（§5.5） |
| `text` | string | 是 | 实际朗读文本（不含「他说」提示语）；超长时按 §5.8 切分 |
| `emotion` | enum | 是 | 统一情绪枚举；克隆音色下用于**选变体**，不传给 TTS |
| `emotion_intensity` | number | 否 | 默认 0.5；折算为 0~3 四档强度，用于选同强度的音色变体（§5.7） |
| `paralinguistic` | array | 否 | 副语言：`laugh\|sigh\|breath\|cry\|whisper`；路由硬条件之一（§5.6） |
| `speed` | number | 否 | 默认 1.0；**后期变速**系数（ffmpeg atempo），不是 TTS 参数 |
| `pause_before_ms` / `pause_after_ms` | integer | 否 | 参与时间轴排版 |
| `delivery_note` | string | 否 | 表演提示，仅供人工参考；不传给 TTS（克隆模型不支持自然语言指令） |
| `anchor` | object | 是 | 必须能定位到正文；纯旁白也回指段落 |
| `tts` | object | 是 | 配音状态；`synthesized_revision != line.revision` 即过期 |

> 旁白不是独立实体：`kind = narration` 且 `speaker_id = chr_narrator`，复用同一套配音与时间轴逻辑。

### 2.4 音效事件（sfx_event）

```jsonc
{
  "id": "sfx_01JBM90004",
  "index": 1,
  "kind": "ambience",               // ambience|foley|impact|transition|music_sting
  "prompt": "深夜警局走廊，日光灯电流声，远处键盘声",
  "prompt_en": "Late-night police station corridor, fluorescent hum, distant keyboard clicks",
  "duration_ms": 12000,
  "loop": true,
  "gain_db": -12.0,
  "fade_in_ms": 800,
  "fade_out_ms": 1200,
  "position": {
    "mode": "during_line",          // before_line|after_line|during_line|shot_start
    "line_id": "lin_01JBM90003",
    "offset_ms": 0
  },
  "anchor": { /* §3.1，必填；通常锚定到所在镜头或某行 */ },
  "asset": {
    "status": "pending",            // pending|queued|running|ready|failed|skipped
    "asset_id": null,
    "actual_duration_ms": null,
    "adapter": "placeholder",
    "cache_key": null,
    "error": null
  },
  "revision": 1
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kind` | enum | 是 | 决定默认时长与音量模板 |
| `prompt` | string | 是 | 生成/检索描述 |
| `duration_ms` | integer | 是 | 期望时长；循环音效为单段长度 |
| `loop` | boolean | 否 | 是否循环铺满锚定范围 |
| `position.mode` | enum | 是 | `shot_start` 表示镜头开头（v2 新增，替代 absolute） |
| `position.line_id` | string | 相对行模式必填 | 该行删除后事件降级到镜头开头并记 warning |
| `anchor` | object | 是 | 回指正文；用于改文后判断是否需要重排 |

### 2.5 画面（visual）

```jsonc
{
  "id": "img_01JBM90005",
  "index": 1,
  "shot_type": "中景",              // 特写|中景|全景|空镜
  "composition": "人物在画面左侧，右侧是翻开的卷宗",
  "character_ids": ["chr_01JBM7X3Q9"],
  "action": "沈砚低头看卷宗，手指停在缺口处",
  "style": "写实电影感，冷调日光灯，浅景深",
  "prompt": "35 岁男性刑警站在深夜警局，翻开的卷宗缺页，冷调日光灯，电影感侧逆光",
  "negative_prompt": "低质, 多余手指, 水印",
  "aspect_ratio": "3:4",
  "width": 1152,
  "height": 1536,
  "workflow_template_id": "qwen_image_2.1_3stage",
  "params": { "steps": 8, "cfg": 1.0 },
  "anchor": { /* §3.1，必填 */ },
  "draw": {
    "batch_size": 4,                // 本轮抽卡张数
    "candidate_retention_days": 7,
    "selected_candidate_id": null
  },
  "candidates": [ /* 见 §6 */ ],
  "status": "pending",              // pending|queued|generating|candidates_ready|selected|failed|stale
  "revision": 1
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `shot_type` / `composition` / `action` | string | 否 | 结构化画面描述，供人工校对与提示词重拼 |
| `character_ids[]` | string[] | 否 | 出场角色；出图时自动拼角色外貌设定 |
| `prompt` | string | 是 | 最终提示词（自动拼装或人工改写） |
| `anchor` | object | 是 | 图在时间轴上的落点依据 |
| `draw.batch_size` | integer | 否 | 默认 4；上限由配置决定 |
| `candidates[]` | array | 是 | 至少一个候选；抽卡结果 |
| `status` | enum | 是 | 见 §7 |

### 2.6 动画（animation）

```jsonc
{
  "id": "vid_01JBM90006",
  "index": 1,
  "visual_id": "img_01JBM90005",     // i2v 首帧来源；t2v 可为空
  "mode": "i2v",                     // i2v|t2v
  "duration_ms": 5000,               // 硬上限 5000
  "motion": "镜头缓慢推进，日光灯轻微闪烁，人物沉默转头",
  "camera_move": "slow_push_in",     // static|slow_push_in|pan_left|pan_right|handheld
  "prompt": "镜头缓慢推进，日光灯轻微闪烁，人物沉默转头",
  "negative_prompt": "画面抖动, 变形",
  "workflow_template_id": "ltx_2.3_i2v",
  "params": {},
  "audio_policy": "mute",            // keep|mute|duck
  "fallback_visual_id": "img_01JBM90005",
  "anchor": { /* §3.1，必填 */ },
  "draw": {
    "batch_size": 2,
    "candidate_retention_days": 7,
    "selected_candidate_id": null
  },
  "candidates": [],
  "status": "pending",               // pending|queued|generating|candidates_ready|selected|failed|skipped|stale
  "revision": 1
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `duration_ms` | integer | 是 | `maximum: 5000`，schema 与 UI 双重限制 |
| `mode` | enum | 是 | `i2v` 必须提供 `visual_id` |
| `visual_id` | string | i2v 必填 | 首帧图；未选中候选时取该 visual 的选中候选 |
| `audio_policy` | enum | 是 | 视频自带音轨与配音冲突时的处理 |
| `fallback_visual_id` | string | 否 | 视频失败时回退静态图 |

### 2.7 静音 / 停顿事件（silence_event）

Qwen3-TTS **没有 SSML、没有停顿参数**，停顿只能靠「标点 + 片段切分 + 后期插入静音」。因此显式建模：

```jsonc
{
  "id": "sil_01JBM90011",
  "index": 1,
  "duration_ms": 600,
  "position": {
    "mode": "after_line",          // before_line|after_line|during_line|shot_start|shot_end
    "line_id": "lin_01JBM90003",
    "offset_ms": 0
  },
  "anchor": { /* §3.1 */ },
  "reason": "对白之间的呼吸停顿"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `duration_ms` | integer | 是 | 50~30000；时间轴按实测语音轨插入空白 |
| `position.mode` | enum | 是 | `shot_end` 为 v2 新增，用于镜头收尾留白 |
| `position.line_id` | string | 相对行模式必填 | 该行被删除时降级到镜头结尾并告警 |
| `reason` | string | 否 | 生成依据，便于人工校对 |

**与台词行 `pause_before_ms` / `pause_after_ms` 的分工**：行内停顿字段描述「这一行前后自然的呼吸」，由时间轴排版直接使用；`silence_event` 描述「跨行、跨镜头或需要单独标注的留白」，优先级更高，两者同时存在时以静音事件为准并去重。

### 2.8 转场

镜头级两个字段，不单独建实体：

```jsonc
"transition_in":  { "type": "fade", "duration_ms": 400 },
"transition_out": { "type": "cut",  "duration_ms": 0 }
```

`type` 枚举：`cut`（硬切，默认）、`fade`（黑场淡入淡出）、`dissolve`（叠化）、`none`（无转场，直接接）。`duration_ms` 参与时间轴总时长计算；`cut`/`none` 为 0。

---

## 3. 双向映射与增量重算

### 3.1 锚点（anchor）

所有脚本元素都带同一个锚点结构：

```jsonc
{
  "block_id": "blk_01JBM90000",   // 必填：正文段落块 ID
  "char_start": 120,              // 必填：块内起始偏移（Unicode 码点，0 基）
  "char_end": 125,                // 必填：块内结束偏移（不含）
  "text_hash": "sha256:...",      // 必填：该范围原文的哈希（规范化：去首尾空白、全半角统一）
  "line_id": "lin_01JBM90003"     // 可选：稳定行 ID，优先用于定位
}
```

定位算法（从强到弱）：

1. **行 ID 命中**：`line_id` 存在且该行仍在脚本 → 直接定位。
2. **哈希命中**：`chapter.blocks[block_id]` 内 `[char_start, char_end)` 的文本哈希等于 `text_hash` → 未变。
3. **邻域搜索**：在 `char_start ± 200` 范围搜索相同文本 → 命中则更新偏移，标记 `shifted`。
4. **全文搜索**：在章节正文搜索唯一匹配 → 命中则更新 `block_id` 与偏移，标记 `shifted`。
5. **孤儿**：以上都失败 → 元素标 `orphan`，进入「待处理」列表，不参与下游生成。

### 3.2 正文段落块（block）

正文章节新增 `blocks[]`（由正文派生，可重建）：

```jsonc
{
  "id": "blk_01JBM90000",
  "index": 0,
  "kind": "paragraph",            // paragraph|dialogue|heading
  "char_start": 0,                // 相对整章正文的偏移
  "char_end": 120,
  "text_hash": "sha256:..."
}
```

块 ID 在正文未改动的段落上保持稳定（按 `text_hash + 顺序` 匹配）；只改标点也会导致哈希变化，但邻域搜索仍能定位。

### 3.3 增量重算规则

| 改动 | 重算范围 | 不重算 |
| --- | --- | --- |
| 改某行台词文本（同一块内） | 该行 `revision+1` → 重切该行 → 该行 TTS；若文本长度变化影响时间 → 重建时间轴 | 同镜头其他行、同镜头图/视频（除非提示词引用了该句） |
| 改说话人 | 该行 TTS（换音色） | 其他行 |
| 改情绪/语速/停顿 | 该行 TTS | 其他行 |
| 改角色音色/克隆音色 | 该角色全部台词行 TTS | 图像、视频、音效 |
| 改角色外貌设定 | 引用该角色的所有 `visuals` 标 `stale` → 重生成提示词与图；依赖这些图的 `animations` 也标 `stale` | 台词、音效 |
| 改镜头氛围/镜头描述 | 该镜头的 `visuals` 提示词重拼 → 图重新抽卡；依赖图的动画重跑 | 台词 TTS（文本未变） |
| 改音效提示词/时长 | 该音效事件重生成 | 其他元素 |
| 在块内插入/删除句子 | 该块内的行重新切分；受影响行 TTS；锚点邻域搜索 | 其他块 |
| 整章重写 | 重新生成导演脚本（旧脚本归档为 `history`）；全部元素重新生成 | —（本次全量） |
| 删除某行 | 锚定该行的音效降级到镜头开头；画面锚点转孤儿或顺延到下一行 | 其他行 TTS |

**判定口径**：元素级 `input_revision` = 该元素自身 `revision`（文本/参数变化）+ 依赖的 `settings_revision` + 依赖的角色/音色 `revision`。任一变化即标 `stale`，入队时重算；无变化直接复用产物。

### 3.4 改文后的最小重算流程

```
正文 diff
  → 重算 blocks（稳定 ID）
  → 逐元素 anchor 定位（命中/漂移/孤儿）
  → 漂移元素更新锚点（不动内容）
  → 文本变化的行 revision+1
  → 依赖设定/角色变化的元素标 stale
  → 仅把 stale 元素加入队列
  → 时间轴按 source_hash 判定是否重建
```

---

## 4. 续写机制

### 4.1 续写单元

章节内的续写会话落盘在 `chapters/<id>.json` 的 `continuations[]`：

```jsonc
{
  "id": "cnt_01JBM90007",
  "mode": "continue",             // continue|outline_driven|rewrite_range
  "instruction": "沈砚发现卷宗缺页后，先怀疑内鬼，不要立刻摊牌",
  "target_words": 1200,
  "outline_ref": "outline.chapter_01.beat_3",  // 可选，绑定大纲节点
  "range": null,                   // rewrite_range 时必填 {char_start,char_end}
  "context": {
    "include_bible": true,
    "include_character_cards": ["chr_01JBM7X3Q9"],
    "include_previous_summaries": 3,
    "include_recent_chars": 4000,
    "budget_tokens": 120000
  },
  "status": "done",                // pending|running|done|failed|canceled
  "produced": { "char_start": 5200, "char_end": 6800 },
  "model": "Qwen3.8-27B-oQ4e-mtp",
  "usage": { "prompt_tokens": 0, "completion_tokens": 0 },
  "created_at": "2026-11-09T12:00:00Z"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `mode` | enum | 是 | 继续写 / 按大纲写 / 改写指定区间 |
| `instruction` | string | 否 | 用户的续写思路；为空则纯按大纲自动续写 |
| `target_words` | integer | 否 | 目标字数，默认 1000 |
| `outline_ref` | string | 否 | 大纲节点引用，便于自动续写时对齐 |
| `range` | object | rewrite 时必填 | 待改写区间（字符偏移） |
| `context.*` | object | 是 | 上下文装配参数 |
| `status` / `produced` | enum / object | 是 | 生成状态与落盘区间 |

### 4.2 上下文装配顺序（从高到低优先级）

1. **系统约束**：输出格式、字数、禁止事项、文风 `style_guide`。
2. **设定摘要**：`bible.compact`（世界观、核心冲突、时间线、禁忌）。
3. **角色卡**：仅本章出场角色（姓名、关系、性格、说话风格、当前状态）。
4. **前文章节摘要**：最近 N 章 `synopsis`（默认 3 章）。
5. **当前章已写正文尾部**：最近 4000 字（可配置），保证衔接。
6. **续写思路**：用户 `instruction`。
7. **大纲节点**：当前 beat 与下一 beat。

预算 262K token，但按 `budget_tokens`（默认 120K）留出输出余量。超出时从第 5 项开始截断，仍超则减少第 4 项摘要数量；设定与角色卡不截断。

### 4.3 长文一致性

| 机制 | 落地方式 | 更新时机 |
| --- | --- | --- |
| 滚动章节摘要 | 每章 `synopsis` ≤ 400 字，由写作模型在章节完成时生成 | 章节写完 / 人工触发 |
| 设定检索 | 先做关键词 + 角色名匹配（不引入向量库）；命中 `bible.glossary` 与角色卡 | 每次装配上下文 |
| 角色状态卡 | `characters[].state` 记录当前章节的处境、关系、已知信息 | 每章完成后由模型建议、人工确认 |
| 连续性检查 | 检查人名/称谓/音色绑定/时间线冲突，输出 warning | 章节完成后 |

> 向量检索列为开放问题（§11），首版不引入，避免额外依赖与不可控召回。

---

## 5. 音色系统与 TTS Provider 抽象

**核心约束：配音不绑死单一 TTS。** 上层只按「能力」挑 provider，不按模型名写分支；模型差异全部收敛在适配器内。Qwen3-TTS 1.7B 是当前唯一已实测的 provider，作为参考实现，不是唯一实现。

### 5.1 Provider 抽象与统一接口

三类对象固定，provider 只实现这三类：

```jsonc
// 合成请求（统一入参，各 provider 自行取舍）
{
  "request_id": "ttsreq_01JBM90001",
  "provider_id": "qwen3_tts_base",     // 解析后的实际 provider
  "model": "Qwen3-TTS-1.7B-Base",      // 模型标识与版本，写入产物溯源
  "text": "说重点。",                   // 单个切块文本，不是整行
  "language": "zh-CN",
  "voice": {
    "voice_id": "voc_01JBM90008",
    "variant_id": "vav_01JBM90009",
    "preset_name": null,
    "reference_audio": "assets/voices/ref/vav_01JBM90009.wav",
    "prompt_cache": "assets/voices/prompts/vav_01JBM90009.pt",
    "instruct": null                   // 仅支持指令控情绪的 provider 才用
  },
  "emotion": "serious",                // 统一情绪枚举；provider 自行决定用/不用
  "speed": 0.95,                       // 统一倍率；不支持原生变速的 provider 由后期 atempo
  "pause_before_ms": 200,
  "pause_after_ms": 400,
  "output": { "format": "wav", "sample_rate": 24000, "channels": 1 },
  "stream": false,
  "seed": 1234,
  "constraints": { "max_concurrent": 1 } // 调度层注入，provider 只读
}

// 合成结果（成功）
{
  "request_id": "ttsreq_01JBM90001",
  "status": "ok",
  "asset_id": "assets/voice/chunks/lin_01JBM90003_k0_r2_v1.wav",
  "duration_ms": 1430,
  "sample_rate": 24000,
  "channels": 1,
  "format": "wav",
  "provider_id": "qwen3_tts_base",
  "model": "Qwen3-TTS-1.7B-Base",
  "model_version": "1.7b-base-2026-10",
  "voice_id": "voc_01JBM90008",
  "variant_id": "vav_01JBM90009",
  "applied": { "emotion": false, "speed": false, "ssml": false }, // 实际生效项
  "warnings": ["emotion_fallback:default_variant"],
  "usage": { "chars": 4, "audio_ms": 1430, "gpu_ms": 2100 },
  "created_at": "2026-11-09T12:00:00Z"
}

// 合成结果（失败）
{
  "request_id": "ttsreq_01JBM90001",
  "status": "error",
  "provider_id": "qwen3_tts_base",
  "code": "PROVIDER_OOM",              // 见 §5.13 错误码
  "message": "显存不足：需要约 11GB，当前可用 3.2GB",
  "retryable": true,
  "hint": "关闭 ComfyUI 或换用更小 provider",
  "attempts": 2
}
```

统一要求：

| 项 | 规定 |
| --- | --- |
| 模型标识与版本 | 结果必须回写 `model` + `model_version`，产物溯源与重算都依赖它 |
| 本地权重路径 | 只在 provider 配置里出现（`weights_path`），不写入音色变体，不进音色库 |
| 运行后端 | 声明 `backend: mlx \| torch_mps \| torch_cuda \| http \| cli`，调度层据此判断显存与是否需卸载 |
| 健康检查 | `health(timeout_s) -> {ok, detail, loaded_models, free_memory_gb}` |
| 显存与并发约束 | 声明 `vram_estimate_gb`、`max_concurrent`、`supports_batch`、`unload`（端点/命令/策略） |
| 错误 | 统一错误码 + 可重试标记 + 可操作 hint（见 §5.13） |

### 5.2 能力矩阵（capability）

每个 provider 必须声明以下能力，**上层按能力选择，不按模型名判断**：

```jsonc
{
  "provider_id": "qwen3_tts_base",
  "display_name": "Qwen3-TTS 1.7B Base（本地）",
  "model": "Qwen3-TTS-1.7B-Base",
  "model_version": "1.7b-base-2026-10",
  "backend": "http",                    // mlx|torch_mps|torch_cuda|http|cli
  "weights_path": "/Users/me/models/Qwen3-TTS-1.7B-Base",
  "endpoint": "http://127.0.0.1:9100",  // http/cli 后端才有
  "health_path": "/health",
  "vram_estimate_gb": 11,
  "max_concurrent": 1,
  "supports_batch": false,
  "unload": { "enabled": true, "path": "/v1/models/{model}/unload", "policy": "on_switch" },
  "capabilities": {
    "emotion_control": "reference",     // instruct|reference|none
    "emotion_instruction": false,       // 是否接受自然语言指令（"愤怒地"）
    "emotion_reference_variants": true, // 是否靠切换参考音频表现情绪
    "emotion_intensity_levels": 4,      // 情绪强度分档数；0=不分档
    "voice_clone": true,
    "clone_min_ref_ms": 3000,           // 能力下限
    "clone_recommended_ref_ms": 5000,
    "clone_prompt_cache": true,         // 是否支持预计算音色特征缓存
    "max_variants_per_voice": 12,       // 音色变体数量上限；null=不限制
    "max_text_chars": 120,              // 单次最长文本（约 30 秒）
    "max_text_ms": 30000,
    "nonverbal": ["laugh", "sigh"],     // 笑/叹/气声等非语言发声；空数组=不支持
    "nonverbal_control": "reference",   // native|reference|none：副语言的实现方式
    "native_duration_control": false,   // 是否支持目标时长控制
    "sample_rates": [24000, 48000],
    "output_formats": ["wav", "mp3"],
    "ssml": false,
    "pause_control": "silence_event",   // ssml|silence_event|none
    "streaming": true,
    "native_speed": false,              // 无原生语速，靠 atempo
    "native_pitch": false,
    "languages": ["zh-CN", "en-US"],
    "license": "Apache-2.0",
    "license_gate": "none",             // none|approval_required|non_commercial
    "commercial_use": true,
    "license_note": "允许商用；克隆需声音所有者授权",
    "quality_tier": "standard",         // draft|standard|premium：路由打分用
    "latency_tier": "standard",         // fast|standard|slow：路由打分用
    "consistency": {
      "stability": "high",              // high|medium|low：同角色跨句音色稳定度
      "pin_per_character": true         // 是否优先让同一角色固定用本 provider
    },
    "allow_auto_select": true           // 是否允许被自动路由选中；false 只能手动指定
  }
}
```

能力矩阵字段与上层行为的对应：

| 能力字段 | 上层怎么用 |
| --- | --- |
| `emotion_control` | `instruct`：把情绪写进 `instruct`；`reference`：按情绪选变体；`none`：忽略情绪并标 warning |
| `emotion_intensity_levels` | 情绪强度档位上限；需求档位高于此值时向下就近取档并标降级（§5.7） |
| `voice_clone` | 为 false 时，克隆变体在该 provider 上标不可用，不发起合成 |
| `max_variants_per_voice` | 超过上限时新建变体被拒绝并提示上限 |
| `max_text_chars` / `max_text_ms` | 决定切块粒度：取所有候选 provider 的最小值作为切分上限 |
| `nonverbal` | 剧本里的笑声/叹气只能路由到声明支持该类型的 provider，否则降级为文字 |
| `nonverbal_control` | `native`：直接按标记合成；`reference`：切到对应副语言参考变体；`none`：含副语言的行出局 |
| `native_duration_control` | 为 false 时，需求带「目标时长」只能靠切分 + 静音填充 + 微变速补偿（§5.7） |
| `sample_rates` / `output_formats` | 合成后统一重采样到工程规格；不支持的格式由 ffmpeg 转换 |
| `ssml` / `pause_control` | 不支持 SSML 时，停顿一律走显式静音事件（§2.7） |
| `streaming` | 为 false 时整块合成，不做边生成边播放 |
| `max_concurrent` | 与全局 `gpu.max_concurrent=1` 取小值 |
| `license` / `license_gate` / `commercial_use` | 许可硬过滤：商用工程只允许 `commercial_use=true` 且 `license_gate=none`（§5.7） |
| `quality_tier` / `latency_tier` | 自动路由打分的质量档位与耗时档位（§5.6） |
| `consistency.stability` / `pin_per_character` | 同角色历史一致性加分；`pin_per_character=true` 时优先保持同角色同 provider，避免音色跳 |
| `allow_auto_select` | 为 false 时自动路由直接跳过，但仍可手动指定/锁定 |

### 5.3 已实测的能力边界（Qwen3-TTS 1.7B 参考实现）

| 能力 | 结论 | 设计影响 |
| --- | --- | --- |
| 预设音色 | 9 个：Vivian、Serena、Uncle_Fu、Dylan（北京话）、Eric（四川话）、Ryan、Aiden、Ono_Anna、Sohee | 预设按 `preset_name` 直接引用，不复制音频 |
| 零样本克隆 | **3 秒参考音频即可，无需微调** | 克隆门槛低，但仍要求 3 秒以上、单人、低噪 |
| 克隆缓存 | 音色特征可预计算为 `voice_clone_prompt` 并复用 | 每个变体缓存一份，全篇复用同一份，避免音色漂移 |
| 克隆 + 情绪 | **Base 克隆模型不支持自然语言指令控情绪**；指令控情绪只在 CustomVoice / VoiceDesign 档 | 情绪只能靠「同一角色按情绪准备多套参考音频（每套 5~15 秒）」切换 |
| 语速 | 没有语速参数 | 设计中的 `speed` 降级为**后期 atempo 变速**，不传给 TTS |
| SSML / 停顿 | 不支持 SSML | 停顿靠标点、片段切分与后期插入静音（§2.7 静音事件） |
| 单次时长 | 本地建议 ≤30~40 秒，按句切 15~30 秒更稳 | 长行必须切块合成（§5.8） |
| 许可 | Apache-2.0，可商用 | 克隆仍需授权与合规提示 |

### 5.4 音色库与变体（`voices.json`）

一个「音色」= 一个角色可用的**变体集合**；变体是一套可独立合成的素材（预设音色、某情绪的克隆参考、或自录）。

```jsonc
{
  "schema_version": 1,
  "voices": [
    {
      "id": "voc_01JBM90008",
      "name": "沈砚-低沉男声",
      "default_provider_id": "qwen3_tts_base",   // 可空；空则跟随工程默认 provider
      "language": "zh-CN",
      "gender": "male",
      "tags": ["低沉", "冷静", "中年"],
      "default_variant_id": "vav_01JBM90009",
      "variants": [
        {
          "id": "vav_01JBM90009",
          "name": "默认",
          "source": "cloned",                 // preset|cloned|recorded
          "emotion_tags": ["default", "neutral", "serious"],
          "preset_name": null,
          "reference": {
            "audio_path": "assets/voices/ref/vav_01JBM90009.wav",
            "duration_ms": 12000,             // 克隆最少 3 秒；按情绪建议 5~15 秒
            "text": null,
            "snr_db": 32.5,
            "channels": 1,
            "sample_rate": 24000
          },
          "prompt_cache": {
            "status": "ready",
            "path": "assets/voices/prompts/vav_01JBM90009.pt",
            "hash": "sha256:...",
            "model": "Qwen3-TTS-1.7B-Base",
            "created_at": "2026-11-09T12:00:00Z"
          },
          "provider_availability": [           // provider × 变体：克隆实现不同，效果不同
            {
              "provider_id": "qwen3_tts_base",
              "status": "ready",               // ready|pending|unusable
              "clone_supported": true,
              "preview_asset_id": "assets/voices/preview/vav_01JBM90009_qwen3_tts_base.wav",
              "similarity_note": "与原声接近，气声保留较好",
              "checked_at": "2026-11-09T12:00:00Z"
            },
            {
              "provider_id": "other_tts",
              "status": "unusable",
              "clone_supported": false,
              "reason": "该 provider 不支持零样本克隆"
            }
          ],
          "consent": {
            "confirmed": true,
            "note": "本人授权，仅用于本作品",
            "evidence_path": "assets/voices/consent/vav_01JBM90009.txt"
          },
          "quality": { "status": "pass", "notes": [], "checked_at": "2026-11-09T12:00:00Z" },
          "status": "ready",
          "created_at": "2026-11-09T12:00:00Z"
        },
        {
          "id": "vav_01JBM90010",
          "name": "愤怒",
          "source": "cloned",
          "emotion_tags": ["angry", "excited"],
          "reference": {
            "audio_path": "assets/voices/ref/vav_01JBM90010.wav",
            "duration_ms": 9000
          },
          "prompt_cache": { "status": "ready", "path": "assets/voices/prompts/vav_01JBM90010.pt" },
          "consent": { "confirmed": true, "note": "同一授权人" },
          "status": "ready",
          "created_at": "2026-11-09T12:00:00Z"
        }
      ],
      "status": "ready",
      "revision": 1,
      "created_at": "2026-11-09T12:00:00Z",
      "updated_at": "2026-11-09T12:00:00Z"
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `default_variant_id` | string | 是 | 情绪未命中时的兜底变体 |
| `default_provider_id` | string | 否 | 该音色的建议 provider；为空跟随工程默认 |
| `variants[].source` | enum | 是 | `preset`（9 个预设）/`cloned`（克隆）/`recorded`（自录参考） |
| `variants[].emotion_tags` | string[] | 是 | 该变体适用的情绪；`default` 表示兜底 |
| `variants[].preset_name` | enum | preset 必填 | 9 个预设名之一 |
| `variants[].reference` | object | cloned/recorded 必填 | 参考音频路径、时长（2~20 秒）、可选文本、质量指标 |
| `variants[].prompt_cache` | object | 建议 | `voice_clone_prompt` 缓存路径与哈希；缺失则合成前先生成 |
| `variants[].provider_availability` | array | 用克隆/自录时建议 | 该变体在每个 provider 上的可用性、试听产物、克隆是否受支持 |
| `variants[].consent.confirmed` | boolean | cloned/recorded 必填 | 必须 `true`，否则禁止合成（合规闸门） |
| `variants[].status` | enum | 是 | `ready|pending|unusable` |

> **provider 解耦**：`voices.json` 不绑定任何模型。变体里的参考音频、prompt 缓存是共用资产；能否在某个 provider 上用，由 `provider_availability` 决定。`prompt_cache` 是 Qwen3-TTS 的概念，其他 provider 可换成自己的缓存字段，但结构保持一致以便上层统一读取。

### 5.5 provider 与变体的选择、回落链

选择分两层：**先定 provider，再在该 provider 可用的变体里定音色**。

**第一层：provider 解析**（命中即停）

1. 行级 `voice.provider_id`。
2. 角色 `voice_binding.provider_id`。
3. 工程默认 `tts.default_provider_id`。
4. 回落链：按 `tts.provider_order` 依次取第一个「健康且能力满足」的 provider。
5. 全部不可用 → 行标 `provider_missing`，阻断该行但继续其他行。

「能力满足」的最低条件：`voice_clone`（若用的是克隆变体）、文本长度 ≤ `max_text_chars`、需要非语言发声时 `nonverbal` 命中、商用作品时 `commercial_use=true`。

**第二层：音色变体解析**（在该 provider 下）

1. 行级 `voice.variant_id`。
2. 角色 `voice_binding.emotion_variant_map[emotion]` 中该 provider 可用的变体。
3. 角色 `voice_binding.default_variant_id` 在该 provider 下的可用变体。
4. 音色库 `voices[].default_variant_id` 在该 provider 下的可用变体。
5. 都不可用 → `voice_missing`，进入校对列表。

**降级规则**：

- 情绪没有对应变体：`emotion_control=instruct` 时把情绪写进 `instruct`；`=reference` 时回落默认变体并标「情绪变体缺失」；`=none` 时忽略情绪并标 warning。
- provider 不支持克隆但角色只有克隆变体：若配置了 `fallback_provider_id`，换 provider；否则该行失败。
- 变体在首选 provider 上不可用（未试听/缓存失败/授权未确认）：按 `provider_availability` 找下一个可用 provider；都没有则换 `fallback_voice_id`。
- 同一角色同一情绪必须始终解析到「同一 provider + 同一变体」，否则音色漂移；实际结果写入 `tts.provider_id`、`tts.model_version`、`tts.variant_id` 三项供溯源。

三级都为空时不再手工挑，交给自动路由（§5.6）。

### 5.6 自动路由（auto routing）

默认体验是「不逐句手选」。自动路由只在行、角色、工程三级都没有锁定时启动；锁定优先（§5.6.4）。

#### 5.6.1 需求向量

从导演脚本与台词行只读提取，不改台词：

```jsonc
{
  "line_id": "lin_01JBM90003",
  "character_id": "chr_01JBM90001",
  "emotion": { "label": "sad", "intensity": 2 },   // intensity 0~3 四档，由 emotion_intensity 折算
  "paralinguistic": ["cry", "breath"],              // 哭腔/气声/耳语/笑/叹；来自 line.paralinguistic
  "clone_required": true,                           // 角色可用变体只有克隆来源时为 true
  "text_chars": 42,
  "language": "zh-CN",
  "duration_control": { "mode": "target", "target_ms": 4200 },  // none|max|target
  "streaming_required": false,
  "usage": "commercial"                             // 来自 project.work.usage
}
```

| 字段 | 来源 |
| --- | --- |
| `emotion` | `line.emotion`；`intensity = round(line.emotion_intensity * 3)` |
| `paralinguistic` | `line.paralinguistic[]`；为空时从 `text` 的括注（如「（哽咽）」）识别，仅作建议 |
| `clone_required` | 角色该情绪目标变体的 `source=cloned/recorded` |
| `duration_control` | 行 `target_duration_ms`，或该镜头动画的时长约束；否则 `mode=none` |
| `streaming_required` | 试听/预览请求；正式批量合成恒为 false |
| `usage` | `project.work.usage`（`commercial|personal`） |

#### 5.6.2 候选筛选（硬过滤 → 打分）

硬过滤按顺序执行，任一条不满足即出局，并记录 `stage` 与 `reason`：

1. `enabled=true`、配置完整、`health` 通过。
2. `allow_auto_select=true`。
3. **许可**：`usage=commercial` 时要求 `commercial_use=true` 且 `license_gate=none`；`usage=personal` 时只排除 `license_gate=non_commercial`。
4. **能力**：情绪表达方式可用（`instruct` 且 `emotion_instruction`，或 `reference` 且角色存在该强度档变体）；`paralinguistic` 为空或全部 ∈ `nonverbal[]` 且 `nonverbal_control != none`；`clone_required` → `voice_clone=true`；`text_chars ≤ max_text_chars`；语言命中 `languages[]`；`duration_control.mode != none` 时要么 `native_duration_control=true`，要么允许后期补偿（§5.7）；`streaming_required` → `streaming=true`。
5. **变体可用**：目标变体在该 provider 的 `provider_availability.status=ready`。
6. **资源**：`vram_estimate_gb` 不超过当前可用余量，或可通过 `unload` 腾出（记录 `requires_unload=true`）。

打分（默认权重，可在 `config/config.yaml` 的 `tts.routing.weights` 调整）：

| 维度 | 默认权重 | 计分说明 |
| --- | --- | --- |
| 能力匹配度 | 30 | 情绪表达方式与副语言完全匹配得满分；需要降档（强度不足、副语言转文字）按缺口扣分 |
| 质量档位 | 25 | `premium=25`、`standard=18`、`draft=8` |
| 同角色一致性 | 20 | 该角色已有成功产物且同 provider：+20；同 provider 但无历史：+12；换 provider：0~5（`pin_per_character=true` 且 `stability=high` 时取高值） |
| 耗时档位 | 15 | `fast=15`、`standard=10`、`slow=3` |
| 资源余量 | 10 | 无需卸载 10；需卸载 5；余量紧张 2 |

并列处理固定为：总分相同 → `provider_order` 索引小者优先 → `provider_id` 字典序，保证同输入必得同一结果。

#### 5.6.3 可解释

每条台词把决策写入 `line.tts.routing`，UI 可展开查看「为什么选它、淘汰了谁」：

```jsonc
{
  "mode": "auto",                    // auto|locked_line|locked_character|locked_project|manual
  "policy_revision": 3,              // 路由权重/许可策略版本
  "input_hash": "sha256:...",        // 需求向量 + provider 快照 + 音色库 revision 的哈希
  "selected": {
    "provider_id": "qwen3_tts_base",
    "variant_id": "vav_01JBM90009",
    "score": 86,
    "reasons": ["情绪按变体匹配", "同角色历史一致", "许可允许商用"]
  },
  "rejected": [
    { "provider_id": "indextts_25", "stage": "license", "reason": "LICENSE_BLOCKED", "detail": "商用需申请授权，工程为商用" },
    { "provider_id": "other_tts", "stage": "capability", "reason": "UNSUPPORTED_NONVERBAL", "detail": "不支持 cry" }
  ],
  "degradations": [
    { "kind": "emotion_intensity_downgrade", "from": 3, "to": 2, "detail": "目标 provider 只有 3 档" }
  ],
  "decided_at": "2026-11-09T12:00:00Z"
}
```

`stage` 取值：`availability|license|capability|variant|resource|score`。`reason` 复用统一错误码（§5.13）。

#### 5.6.4 可锁定

| 粒度 | 字段 | 优先级 |
| --- | --- | --- |
| 行 | `line.voice.provider_id` | 1（最高） |
| 角色 | `character.voice_binding.provider_id` | 2 |
| 工程 | `project.settings.tts.routing.lock_provider_id` | 3 |

任一非空即视为锁定，跳过打分，`routing.mode` 记为对应 `locked_*`。工程 `routing.mode=manual` 时完全不自动，空缺直接报 `ROUTING_NO_CANDIDATE` 并列出每个 provider 的淘汰原因。

#### 5.6.5 失败与回落

- 自动选中的 provider 合成失败（OOM / 超时 / 模型缺失 / 许可阻断）→ 按 `provider_order` 从当前项之后继续降级。
- 锁定 provider 失败同样降级，但**不覆盖** `line.voice.provider_id`，只写 `routing.degradations[]`：`kind=provider_fallback, from, to, reason`。
- UI 在台词行标「已降级」徽标并展示原因；降级会改变听感，不允许静默。
- 重跑时默认先重试原 provider（`tts.routing.retry_original_first=true`），失败再按上次降级路径走。

#### 5.6.6 确定性

- 决策输入 = 需求向量 + provider 快照（`model_version` + capabilities 哈希）+ 音色库 `revision` + 路由权重 `policy_revision` + `usage`，合并计算 `input_hash`。
- 同 `input_hash` 且已有 `routing` 记录 → 直接复用，不重新打分；`input_hash` 变化才重算。
- 因此「重算影响面」只依赖可比较的哈希，不依赖模型当时的随机表现。

### 5.7 默认 provider 策略：仅许可干净模型

**默认启用集合**只含「可商用且无附加申请门槛」的 provider：`commercial_use=true` + `license_gate=none` + `allow_auto_select=true`。其余 provider 一律 `enabled=false`，需要用户显式开启。

**IndexTTS 2.5 自 2026-11-10 起为主力 provider**（CosyVoice3 降为备选）。下表数值均按实读结论填写：

```jsonc
{
  "provider_id": "indextts_25",
  "model": "IndexTTS-2.5",
  "backend": "torch-mps",            // 官方 PyTorch 实现；MPS 分支见官方 infer_v2_5.py:107-110
  "enabled": true,                   // 主力 provider
  "capabilities": {
    "emotion_control": "emo_vector_8d",   // 8 维 [happy,angry,sad,afraid,disgusted,melancholic,surprised,calm] + emo_alpha 权重
    "emotion_text": true,            // 文字情绪需构造时 use_qwen_emo=True（README.md:125-127）
    "emotion_audio_prompt": true,    // 另一段音频作情绪参考（infer_v2_5.py:507 emo_audio_prompt）
    "nonverbal": [],
    "nonverbal_control": "none",
    "native_duration_control": true, // duration_factor 0.5~2.0（README.md:105-112）
    "license_gate": "none",          // bilibili 许可 2.1 授予 royalty-free 许可（LICENSE:17），允许商用
    "commercial_use": true,
    "quality_tier": "premium",
    "latency_tier": "unmeasured",    // M2 上 RTF 尚未实测；0.45 / 1.1~1.5 均无我方实测依据
    "allow_auto_select": true
  }
}
```

- **不用 MLX 移植版**：第三方包 `index-tts-2.5-mlx` 自述「this port intentionally drops those」（`emo_vector`/`emo_audio_prompt`/`emo_text`），情绪是核心需求，故走官方 PyTorch + MPS。

- **许可硬过滤**：商用工程遇到 `license_gate != none` 或 `commercial_use=false` 直接出局，不参与打分；UI 显示「许可不允许商用」。只有 `usage=personal` 或已取得授权的工程才允许启用可选 provider。
- **工程用途标记**：`project.work.usage = commercial|personal`，默认 `personal`；导出商用成片前若含 `license_gate != none` 的 provider 产物，阻断并列出受影响台词行。

**两项能力短板的补偿方案**：

| 短板 | 补偿 | 代价与标注 |
| --- | --- | --- |
| 情绪强度无引擎参数 | 同一角色按强度准备多档参考变体（0 平静 / 1 轻 / 2 中 / 3 强，`emotion_intensity_levels=4`）；强度需求高于该 provider 档位数时**向下就近取档** | 变体维护量随档位线性增加；降档写 `emotion_intensity_downgrade` 并在 UI 标注 |
| 无原生时长控制 | 三层：① 切分粒度（15~30 秒）先满足大区间；② 静音填充对齐到目标时长（精度 ±20ms）；③ 仍不够时微变速 atempo，`≤10%` 不标记，`10~20%` 标 warning，`>20%` 需人工确认 | 变速会改变音高与节奏；**有嘴部特写/对口型要求的段落禁止 atempo**，只能靠切分 + 静音填充，且必须「先配音定稿、再出视频」——先出视频再改音频会导致口型不同步，i2v 无法回头修 |

### 5.8 长句切分与停顿节奏

| 规则 | 做法 |
| --- | --- |
| 切分目标 | 单块 15~30 秒（按实测语速折算约 60~120 字），硬上限 40 秒 |
| 切分点 | 优先句末标点（。！？）；其次分号、破折号；最后逗号；不切断引号内对白 |
| 拼接 | 片段在标点处拼接，片段之间插入 120~250ms 静音；行首/行尾停顿由 `pause_*` 控制 |
| 切分缓存 | `tts.chunks[]` 记录每一块的文本、产物、时长与所用变体，重录只重跑变化的块 |
| 语速 | 不做 TTS 参数；需要更快/更慢时用 atempo（0.9~1.1 之外会明显失真，超过 ±20% 需人工确认） |
| 静音 | 跨行、跨镜头、情绪转折的留白用 `silence_events` 显式标注（§2.7） |

### 5.9 克隆流程

1. **素材准备**：至少 3 秒，按情绪准备建议 5~15 秒；单人、无音乐、无混响、采样率 ≥ 24kHz；支持上传或本机录音。
2. **授权确认**：必须勾选「本人声音」或「已获得声音所有者授权」，可留存凭证路径；未确认不能创建克隆任务（schema 强制 `consent.confirmed=true`）。
3. **质量检查**：时长、声道、采样率、信噪比、是否多人声；不合格给出具体原因。
4. **预计算**：生成并缓存 `voice_clone_prompt`（按变体一份），记录参考音频与参数哈希；参考变化则缓存失效。
5. **试听**：用固定试听句（如「这是音色试听，今天天气很好。」）人工确认后变体 `status=ready`。
6. **绑定**：角色选择默认变体与情绪映射；已绑定角色相关台词行标 `stale`，不自动重跑。
7. **合规提示**：克隆第三方声音必须获得书面授权；素材与授权说明留存在 `assets/voices/`，随工程迁移。

### 5.10 重录

| 场景 | 操作 | 影响范围 |
| --- | --- | --- |
| 单句重录 | 台词行「重录」，可临时换变体 | 仅该行的块 |
| 按情绪重录 | 换该情绪的变体后重跑 | 该角色该情绪的行 |
| 按角色重录 | 角色页「重录全部台词」，可选章节范围 | 该角色在该范围内的行 |
| 按章节重录 | 配音页「整章重配」，只重跑 stale/failed | 本章需重跑的行 |
| 换音色重录 | 改角色绑定或单行覆盖 | 受影响行全部标 stale |

### 5.11 预设音色清单（Qwen3-TTS）

| 名称 | 说明 |
| --- | --- |
| Vivian | 女声预设 |
| Serena | 女声预设 |
| Uncle_Fu | 男声预设（中年） |
| Dylan | 北京话男声 |
| Eric | 四川话男声 |
| Ryan | 男声预设 |
| Aiden | 男声预设 |
| Ono_Anna | 女声预设 |
| Sohee | 女声预设 |

> 具体音色风格与适用角色由试听后人工决定；预设不参与克隆授权流程。

### 5.12 对比试听（多 provider A/B）

用途：同一句台词用多个 provider 生成，盲听比选，辅助「给角色选 provider」。

```jsonc
// assets/voice/compare/<lineId>_<batchId>.json
{
  "id": "cmp_01JBM90012",
  "line_id": "lin_01JBM90003",
  "text": "说重点。",
  "emotion": "serious",
  "variant_id": "vav_01JBM90009",
  "batch_id": "cmpb_01JBM90013",
  "items": [
    {
      "provider_id": "qwen3_tts_base",
      "model_version": "1.7b-base-2026-10",
      "status": "ready",
      "asset_id": "assets/voice/compare/lin_01JBM90003_qwen3_tts_base.wav",
      "duration_ms": 1430,
      "applied": { "emotion": false }
    },
    {
      "provider_id": "other_tts",
      "model_version": "x.y",
      "status": "failed",
      "code": "PROVIDER_UNSUPPORTED_EMOTION",
      "message": "该 provider 不支持按变体表达情绪"
    }
  ],
  "winner_provider_id": null,           // 人工盲听后填写；只作建议，不自动改绑定
  "created_at": "2026-11-09T12:00:00Z"
}
```

规则：

- 试听不写台词行产物、不改 `tts` 状态、不进时间轴；只落 `assets/voice/compare/` 与一条记录。
- 每次对比视为独立任务，按 provider 逐个串行执行（受 GPU 锁限制）。
- 结果可一键「应用到角色」：写 `voice_binding.provider_id`，受影响行标 `stale`，不自动重跑。
- 跨 provider 对比时，参考音频相同、情绪相同、切块相同，只变 provider，保证可比性。
- 同一 provider 的同一模型版本可重复试听（不同 seed），保留最近 3 次。

### 5.13 切换 provider 的重算影响面

**失效判定**：合成产物的有效输入包含 `provider_id + model_version + variant_id + text_hash + 切块参数 + 输出规格`。任一项变化即视为失效。

| 变更 | 失效 | 不失效 |
| --- | --- | --- |
| 全局默认 provider | 所有「未显式指定 provider」的行 → 块音频、行拼接音频、时间轴、字幕时间码 | 显式锁定 provider 的行；图像/视频/音效 |
| 角色按角色覆盖 provider | 该角色全部行（同上） | 其他角色；画面与动画 |
| 单行指定 provider | 仅该行块音频与拼接音频 | 同镜头其他行 |
| 切换模型版本（同 provider） | 该 provider 下所有行 | 其他 provider 的行 |
| 改 `provider_order`（回落链） | 仅原本走回落链的行 | 显式指定的行 |
| provider 能力变化（如新支持情绪指令） | 标 stale，等人工确认后再重跑 | 已完成的产物 |

**增量口径**：

- 块级失效：只重跑该 `chunk`，同行的其他块复用。
- 行级拼接：块有变化 → 行音频重拼，`duration_ms` 重算。
- 时间轴：行的 `provider_id`、`model_version`、`variant_id`、`duration_ms` 纳入 `source_hash`，变化即重建。
- 字幕：时间码依赖行起止，随行时长变化重建；文本不变则只更新时间。
- 视频/图像：不依赖 TTS，不受 provider 切换影响。

**错误码（统一）**：

| 码 | 含义 | 可重试 | 提示 |
| --- | --- | --- | --- |
| `PROVIDER_UNREACHABLE` | 服务/进程不可达 | 是 | 检查 endpoint 或启动命令 |
| `PROVIDER_UNAUTHORIZED` | 鉴权失败 | 否 | 检查配置文件中的密钥 |
| `PROVIDER_MODEL_MISSING` | 模型未安装/权重路径不存在 | 否 | 装模型或换 provider |
| `PROVIDER_OOM` | 显存/内存不足 | 是 | 卸载其他模型或换小模型 |
| `PROVIDER_TIMEOUT` | 合成超时 | 是 | 调大超时或缩短切块 |
| `PROVIDER_UNSUPPORTED_VOICE` | 不支持该音色/克隆 | 否 | 换变体或换 provider |
| `PROVIDER_UNSUPPORTED_EMOTION` | 不支持情绪控制 | 否 | 降级默认情绪或换 provider |
| `PROVIDER_LICENSE_BLOCKED` | 许可不允许当前用途 | 否 | 换 provider 或调整发布范围 |
| `PROVIDER_BAD_OUTPUT` | 产物为空/损坏/时长异常 | 是 | 重试；连续失败换 provider |

### 5.14 provider 配置与密钥

- 全部外置到 `config/config.yaml` 的 `tts.providers[]`，工程内只存 `provider_id` 引用，不存密钥、不存权重路径。
- 密钥支持 `api_key` 直填或 `api_key_env` 指定环境变量；两者都为空表示无需鉴权。
- 本地权重路径 `weights_path` 仅存在于本机配置；工程迁移到另一台机器时按 `provider_id` 重新绑定。
- 卸载策略在 provider 级声明：`unload.policy = on_switch | never | always`；`on_switch` 表示切到非 TTS GPU 任务前卸载。
- 配置缺失或能力字段不全时，该 provider 在 UI 标灰并提示缺哪一项，不静默使用默认值。
- 该段配置的校验规则见 `schema/tts-provider.schema.json`；对比试听记录见 `schema/voice-compare.schema.json`。

---

## 6. 抽卡机制

### 6.1 候选结构

画面与动画共用：

```jsonc
{
  "id": "cand_01JBM90009",
  "index": 1,
  "asset_id": "assets/image/img_01JBM90005_c1_r1.png",
  "job_id": "job_01JBM90010",
  "seed": 20261109,
  "params_hash": "sha256:...",
  "status": "candidate",            // candidate|selected|rejected|expired|failed
  "created_at": "2026-11-09T12:00:00Z",
  "expires_at": "2026-11-16T12:00:00Z",
  "width": 1152,
  "height": 1536,
  "duration_ms": null               // 视频候选才有
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` / `index` | string / integer | 是 | 候选 ID 与批内序号 |
| `asset_id` | string | 成功时必填 | 产物相对路径 |
| `seed` / `params_hash` | integer / string | 是 | 复现依据；相同 seed+params 视为同一候选 |
| `status` | enum | 是 | 候选状态 |
| `expires_at` | string | 否 | 未选中候选的保留截止时间 |
| `duration_ms` | integer | 视频必填 | ≤5000 |

### 6.2 规则

- **批量生成**：一次生成 N 张/条（图默认 4，视频默认 2），每个候选独立任务、独立 seed；失败不影响同批其他候选。
- **选片**：`selected_candidate_id` 指向一个候选；选中后进入时间轴，其余候选保持 `candidate`。
- **再次抽卡**：新增候选追加到列表，不覆盖历史；`params_hash` 相同的候选直接复用（幂等）。
- **保留期**：未选中候选默认保留 7 天（`draw.candidate_retention_days`），到期由清理任务标 `expired` 并删除媒体文件；已选中候选永久保留。
- **失败重抽**：失败候选不占保留期，可直接重抽。
- **切换选中**：换选另一个候选只改 `selected_candidate_id` 与 `status`，触发时间轴重建，不重新生成。

---

## 7. 状态机与增量

### 7.1 章节

```
draft ──写正文──► written ──生成脚本──► scripted ──全行配音──► voiced ──图/视频就绪──► visualized ──导出──► composed
  ▲                    │                    │                 │                    │
  └──── 上游设定变更（settings_revision 不等）→ stale（保留产物，标需重算）
```

| 状态 | 含义 | 进入条件 |
| --- | --- | --- |
| `draft` | 有标题无正文 | 新建章节 |
| `written` | 有正文，无有效脚本 | 正文非空 |
| `scripted` | 脚本存在且非 stale | 脚本 `status=ready` |
| `voiced` | 全部台词行 `tts.status=ready` | 配音完成 |
| `visualized` | 至少一个视觉元素已选中，且无 pending 的必需图 | 图像/视频完成 |
| `composed` | 有有效导出产物 | 导出成功 |
| `stale` | 上游变更 | `settings_revision` 或角色/音色变化 |

### 7.2 元素级状态

| 元素 | 状态 | 说明 |
| --- | --- | --- |
| 台词行 `tts.status` | `pending` `running` `ready` `failed` `stale` | `stale` 表示输入变了但还没重跑 |
| 音效 `asset.status` | `pending` `queued` `running` `ready` `failed` `skipped` | 失败可静音降级 |
| 画面 `status` | `pending` `queued` `generating` `candidates_ready` `selected` `failed` `stale` | 抽卡中为 `generating` |
| 动画 `status` | 同上 + `skipped` | 超预算或用户关闭 |
| 候选 `status` | `candidate` `selected` `rejected` `expired` `failed` | — |
| 镜头 `status` | `draft` `ready` | 镜头下全部必填元素就绪为 `ready` |

### 7.3 断点续跑

- 任务持久化在 `.ndw/queue.json`，进程退出时 `running` → `interrupted`；重启后自动回 `queued`。
- 每个产物按 `<实体>_r<revision>` 或 `<实体>_c<candidate>_r<revision>` 命名，重跑前先查产物是否存在且参数哈希一致。
- 抽卡是追加式：中断后重跑只补齐缺失候选，不重复已成功候选。
- 时间轴由 `source_hash` 判定；脚本或选中候选变化才重建。

---

## 8. GPU 串行调度

### 8.1 队列模型

七类任务共用一个队列，单消费者：

| 任务 kind | 工具 | 依赖 |
| --- | --- | --- |
| `writing` | 小说生成 | 设定库、前文摘要 |
| `script` | 导演脚本 | 章节正文、设定库 |
| `tts` | 配音 | 脚本台词行、音色 |
| `voice_clone` | 音色克隆 | 参考音频、授权确认 |
| `sfx` | 音效 | 脚本音效事件 |
| `image` | 图像 | 脚本画面、角色外貌 |
| `video` | 视频 | 脚本动画、选中图 |
| `mix` / `export` | 合成导出 | 时间轴、选中产物 |

### 8.2 串行与切换规则

1. **全局互斥**：任何 GPU 任务执行前必须持有 `/Volumes/other/gpu.lock`（与 ComfyUI 共用）。
2. **同一时刻只有一个工具在跑**：`max_concurrent = 1`，即使 omlx 支持 8 路并发也不使用。
3. **优先级**：用户交互触发的单条任务（重录一句、抽一张图）优先于批量任务；同优先级按创建时间。
4. **不抢占**：已开始的任务不被打断；ComfyUI 里已有任务时先等锁，不中断。
5. **模型切换**：工具之间切换不强制卸载模型；提供 `unload_on_switch` 策略（默认 `auto`）：下一个任务是 ComfyUI 类且 omlx 已加载模型时，先调 `/v1/models/{id}/unload` 再执行，避免 29GB 天花板 OOM。策略可配 `auto|never|always`。
6. **失败隔离**：单任务失败不影响同批其他任务；失败任务记录错误码、可重试标记与日志。
7. **进度可见**：队列状态实时反映「排队/执行/成功/失败/中断/取消」，页面上可暂停（执行完当前任务后停）。

### 8.3 与 omlx、TTS provider 的关系

- 文本任务走 omlx；omlx 不自动卸载（`idle_timeout=null`）。
- 文本任务结束后是否卸载由策略决定：`after_text_task`（文本批次结束且下一个不是文本任务）或 `never`。
- 卸载调用 `POST /v1/models/{model_id}/unload`，失败只记 warning，不影响任务结果。
- **TTS 按 provider 分别处理**：provider 声明自己的 `vram_estimate_gb`、`max_concurrent` 与 `unload.policy`。任务开始前调度器检查「当前已加载的大模型 + 本 provider 预估显存」是否超过内存/显存上限，超出则先卸载可卸载的模型（omlx 或上一个 TTS provider），再执行。
- **默认不卸载 TTS**：同一配音批次内连续合成同一 provider 时保持加载；切到其他 provider 或切到 ComfyUI 类任务时，按 `unload.policy` 决定是否卸载。
- 配音任务的服务级并发固定为 1（`min(全局 max_concurrent, provider.max_concurrent)`），单次合成按 §5.8 切块顺序执行；任务内不并行抢占显存。
- 对比试听（§5.12）涉及多个 provider，按 `items` 顺序逐个加载/合成，相邻 provider 不同时驻留。

---

## 9. UI 信息架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 工程：长夜将明    设定 v3    [设定工坊] [小说] [导演脚本] [配音] [图像] [视频] [合成] │
├───────────────┬──────────────────────────────────────┬───────────────────┤
│ 左：项目树     │ 中：当前工具主区                      │ 右：检查器/预览      │
│               │                                      │                   │
│ 设定          │ 小说：章节正文 + 续写面板              │ 角色卡/音色试听     │
│  世界观        │ 脚本：镜头列表 + 镜头详情              │ 候选图网格          │
│  大纲          │ 配音：台词表 + 波形/试听               │ 错误与警告          │
│  角色/音色     │ 图像：提示词 + 抽卡网格                │ 任务日志            │
│ 章节 1..N     │ 视频：动画列表 + 候选视频              │                   │
│ 渲染队列       │ 合成：时间轴预览 + 导出                │                   │
└───────────────┴──────────────────────────────────────┴───────────────────┘
```

### 9.1 各工具核心界面

| 工具 | 主区 | 核心操作 | 跳转 |
| --- | --- | --- | --- |
| 设定工坊 | 世界观 / 大纲 / 角色 / 音色 四个分页 | 编辑设定、导入文本、生成大纲、角色增删、音色试听与克隆 | → 小说、配音、图像 |
| 小说 | 章节正文编辑器（左正文右续写） | 续写（给思路/按大纲）、改写区间、生成章节摘要 | → 导演脚本 |
| 导演脚本 | 镜头卡片列表 + 单镜头详情 | 重新生成脚本、编辑镜头/台词/音效/画面/动画、定位原文 | → 配音、图像、视频 |
| 配音 | 台词表（按角色/情绪/状态筛选） + 变体试听 | 批量配音、单句重录、切换情绪变体、标记失败重试 | → 合成 |
| 图像 | 镜头图位 + 候选网格 | 生成 N 张、选片、重抽、改提示词、锁定画面 | → 视频、合成 |
| 视频 | 动画列表 + 候选播放 | 生成 N 条、选片、回退静态图、设置是否带音轨 | → 合成 |
| 合成导出 | 时间轴（语音/音效/图/视频轨） + 导出面板 | 试听、微调起止、混音、导出 MP4/MP3/SRT | → 产物预览 |
| 渲染队列（全局） | 任务表 | 查看状态、重试、取消、按工具筛选 | 全局 |

### 9.2 跨工具跳转

- 小说里选中一段 → 「看脚本」高亮对应镜头。
- 脚本里某行 → 「去配音」定位该行；某画面 → 「去抽卡」。
- 图像候选选中 → 「用于视频」直接作为 i2v 首帧。
- 任意失败任务 → 跳转到对应工具的失败项。

---

## 10. 里程碑重排（按七工具顺序）

> 原 M1~M4 作为「数据与队列地基」保留在 M0；v2 里程碑按工具推进，每档独立可验收。

| 里程碑 | 范围 | 怎么算完成 |
| --- | --- | --- |
| **M0 地基（已完成部分）** | 工程/章节/角色/台词数据、队列与锁、CLI、schema 校验、omlx 适配器 | 现有冒烟测试通过；omlx 自检「服务可达/模型存在/最小对话」通过 |
| **M1 设定工坊** | 设定库（世界观/大纲/角色/音色库）、导入与编辑、版本与快照 | 新建工程 → 录入世界观与 3 个角色 → 建 2 个音色（1 预设 1 克隆占位）→ 重启数据完整；导出设定快照 JSON；schema 校验通过 |
| **M2 小说生成** | 章节正文、续写会话、按大纲续写、章节摘要、设定注入 | 给定大纲与续写思路，生成 ≥1000 字且角色名/称谓与设定一致；生成 `synopsis`；中断后续写不重复计费；上下文超预算时有明确裁剪日志 |
| **M3 导演脚本** | 从正文生成镜头/台词/音效/画面/动画/转场、锚点、人工编辑、增量重算 | 一章生成 ≥3 个镜头，每行台词有说话人与锚点；改一句话只重算该行；删除一行时锚定音效降级并告警；脚本通过 schema 校验 |
| **M4 配音** | TTS provider 抽象与能力矩阵、至少 1 个真实 provider（Qwen3-TTS）、音色变体与情绪映射、切块合成、单句/批量/重录、实测时长回填、对比试听 | 整章台词全部 `ready` 且有实测时长；长行切成 15~30 秒块且拼接无明显断点；同一角色同一情绪始终用「同一 provider + 同一变体」；缺情绪变体时有降级 warning；换变体/换 provider 只重跑受影响行；单句重录只重跑变化块；授权未确认的克隆变体无法合成；至少 1 句能跨 2 个 provider 对比试听并记录 |
| **M5 图像** | 画面元素到 ComfyUI、角色外貌拼装、抽卡与选片、候选保留期 | 每个画面抽 4 张、可选中 1 张进入时间轴；提示词含角色外貌；候选过期清理生效；ComfyUI 不可达时报可操作错误 |
| **M6 视频** | 动画元素、i2v 首帧、5 秒上限、抽卡、回退静态图 | 至少 1 段 ≤5 秒视频进入时间轴；i2v 使用选中图；生成失败自动回退静态图；视频带音轨时按 `audio_policy` 处理 |
| **M7 合成导出** | 时间轴（含转场）、混音、MP4/MP3/SRT/manifest、试听 | 整章导出 MP4 音画同步；转场时长正确；字幕逐行时间码；重复导出结果一致；失败可只重跑混音 |

**依赖**：M2 依赖 M1；M3 依赖 M1/M2；M4/M5/M6 可并行（都依赖 M3）；M7 依赖 M4~M6 至少各有产物，但允许缺失时降级。

---

## 11. 开放问题（不预设答案）

1. **设定库粒度**：世界观、大纲、角色、音色是否拆成四个文件，还是合并？本文按「三个文件（`settings/bible.json` 含大纲 + `characters.json` + `voices.json`）、共用 `settings_revision`」设计，需确认。
2. **大纲结构**：只到「章」还是到「幕/卷/beat」？`outline_ref` 的路径规范需定。
3. **导演脚本编辑权限**：自动生成后允许改到什么程度？是否允许手工新增镜头与台词行（可能脱离正文锚点）？
4. **锚点稳定性**：正文大改（整段重写）时，是保留旧锚点等待人工重挂，还是直接全章重建脚本？默认策略待定。
5. **旁白音色**：旁白用独立音色、作者本人克隆，还是与主角同音色不同语速？
6. **切块长度与变速**：15~30 秒切块是否在所有语速下都稳定？atempo 超过多少开始明显失真（暂定 ±20% 需人工确认）？
7. **声音克隆合规**：授权声明的具体文案、是否必须留存凭证、是否允许克隆第三方声音用于商业发布。
8. **情绪变体覆盖面**：每个角色准备哪些情绪变体（默认 + 愤怒/悲伤/低语？），变体越多维护成本越高，需定最小集合。
9. **抽卡预算**：默认每画面 4 张、每动画 2 条是否合适？是否按章节/全书设总预算上限？
10. **候选保留期**：7 天是否合适？过期候选是删除文件还是只标 `expired`？
11. **图像风格一致性**：是否引入角色定妆图/LoRA/参考图来保证跨镜头样貌一致？首版仅靠提示词是否可接受？
12. **视频音轨**：H3 自带音轨与配音冲突时，默认 `mute` 还是 `duck`？
13. **时间轴编辑**：v2 是否仍只支持「自动排布 + 少量微调」，不做自由拖拽？
14. **设定与正文冲突**：续写时模型写出与设定矛盾的内容，是自动重写、标 warning，还是阻断？
15. **章节粒度与成片粒度**：一章一集还是多章一集？影响时间轴与导出单位。
16. **工具间并发**：用户是否可能同时打开两个工具并触发任务？队列已串行，但 UI 是否允许排队多工具任务待确认。
17. **旧数据迁移**：v1 的 `scenes/lines` 是否自动迁移为 v2 脚本，还是只支持新工程？
18. **Qwen3-TTS 部署形态**：本地 HTTP 服务、CLI 还是 Python 库？接口协议、端口与 `voice_clone_prompt` 缓存格式需确认后回填 provider 适配器。
19. **克隆质量阈值**：时长下限（3 秒是能力下限，5~15 秒是建议值）、SNR 下限、是否强制单人声检测。
20. **预设音色适用性**：Dylan（北京话）、Eric（四川话）用于普通话文本时是否需要限制；9 个预设的实际音色特征需试听后标注。
21. **切块拼接**：片段间静音长度（暂定 120~250ms）、响度归一化目标（暂定 -16 LUFS）是否合适。
22. **情绪强度字段**：`emotion_intensity` 没有引擎参数可映射，是否保留、改为变体分级，还是仅作人工提示。
23. **首批 provider 范围**：已定 Fun-CosyVoice3-0.5B（MLX 8bit）先跑通；Step-Audio-EditX、GLM-TTS 本轮只留接口与配置。Qwen3-TTS 1.7B 与 IndexTTS 2.5 的接入顺序、是否启用待定。
24. **默认 provider 与回落顺序**：工程默认用哪个？回落链按「质量优先」还是「速度优先」排序（默认 `provider_order` 由用户显式给出）？
25. **跨 provider 的参考音频复用**：同一份参考音频在不同克隆实现上效果差异大，是否需要为每个 provider 单独准备参考片段，还是共用一份并记录试听结论？
26. **能力缺失的处理**：自动路由已把「能力不满足」做成硬过滤；但锁定 provider 不支持情绪/副语言时，是降级并标注、阻断该行，还是要求先改绑定？
27. **对比试听的范围**：只支持「同一句多 provider」，还是也支持「同一 provider 多 seed / 多情绪变体」的 A/B？
28. **商用许可校验**：默认已按「仅许可干净模型」硬过滤；导出前是否仍需强制复核并阻断？
29. **路由权重默认值**：能力 30 / 质量 25 / 一致性 20 / 耗时 15 / 资源 10 是否合理，需要实践校准。
30. **一致性加分的衰减**：换 provider 后，同角色一致性分是否需要随时间/句数衰减，还是永久按当前 provider 计？
31. **决策缓存粒度**：`input_hash` 应包含哪些 capability 字段（全部还是仅参与判定的子集）？新增字段是否强制触发全量重路由？
32. **用途变更的影响**：`work.usage` 从 `personal` 改为 `commercial` 时，是否整体重路由并重算全部配音？
33. **许可凭证记录**：IndexTTS 等需申请授权的 provider，授权凭证存放位置与过期提醒如何设计？
34. **时长对齐精度**：静音填充 ±20ms、atempo 阈值（10%/20%）是否满足成片与对口型要求？

---

## 12. 与现有文档/代码的关系

| 现有内容 | v2 处理 |
| --- | --- |
| `docs/PRD.md` v1 流程 | 更新为七工具顺序，原「场景」表述改为「镜头」 |
| `docs/data-model.md` v1 实体 | 保留并扩展：新增设定库、导演脚本、音色库、候选；`scene` 逐步由 `shot` 承接 |
| `schema/scene.schema.json` | v1 兼容保留；新工程使用 `director-script.schema.json` |
| `schema/line.schema.json` | 字段沿用，新增 `anchor`、`voice`；v1 数据缺省视为无锚点 |
| `src/.../text/splitter.py` | M3 改为「生成导演脚本」的一部分，规则切分作为兜底 |
| `src/.../adapters/tts.py` | M4 实现 Qwen3-TTS 适配（本轮不动） |
| omlx 适配器 | 已实现真实 HTTP 调用，作为 M2 写作引擎 |
