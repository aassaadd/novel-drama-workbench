# 数据模型

本文件定义工作台的持久化数据。所有实体最终以 JSON 文件落盘，遵循同一套约定，并由 `schema/` 下的 JSON Schema 校验。

> **v2 说明（2026-11-09）**：v1 的「章节内嵌 场景 → 台词/音效/图/视频」保留可用，作为 M0 地基；v2 新增**设定库**、**导演脚本**、**音色变体库**、**抽卡候选**、**正文段落块与锚点**、**续写会话**、**静音事件** 七类结构。v2 的镜头（shot）承接 v1 的场景（scene），台词行（line）仍是配音与时间轴的最小单位。设计依据见 `docs/design-v2.md`。
>
> 兼容原则：本节所有 v2 字段均为**新增可选字段或状态枚举超集**，`schema_version` 仍为 1，v1 数据无需迁移即可继续校验通过；v1 的 `scenes[]` 与 v2 的 `scripts/*.json` 可并存。

## 1. 通用约定

| 项 | 约定 |
| --- | --- |
| 编码 | UTF-8，无 BOM；文件名使用 ASCII + 实体 ID |
| 字段命名 | `snake_case`（与 Python 侧字段同名，避免映射层） |
| ID | `前缀_ULID`，全局唯一且有序；前缀见下表 |
| 时间 | ISO 8601 UTC，形如 `2026-11-09T12:00:00Z` |
| 时长 | 整数毫秒（`*_ms`），禁止用浮点秒 |
| 音量 | `gain_db`，浮点 dB，0 为原始电平 |
| 版本 | 每个可变实体有 `revision`（整数，内容变更即 +1），是幂等与增量重跑的依据 |
| 枚举 | 全部小写下划线；未知值按「非法」处理并在校验时报错 |
| 缺失值 | 用 `null` 或不写该键，不写空字符串占位 |
| 向后兼容 | `schema_version` 为整数；升级时提供迁移脚本，不静默改字段含义 |

### 1.1 ID 前缀

| 前缀 | 实体 | 版本 |
| --- | --- | --- |
| `wrk_` | 作品（work） | v1 |
| `chp_` | 章节（chapter） | v1 |
| `scn_` | 场景（scene，v1 遗留） | v1 |
| `sht_` | 镜头（shot，v2） | v2 |
| `scr_` | 导演脚本（director script，文件级 ID） | v2 |
| `blk_` | 正文段落块（block） | v2 |
| `cnt_` | 续写会话（continuation） | v2 |
| `chr_` | 角色（character，含旁白角色） | v1 |
| `lin_` | 台词行（line） | v1 |
| `sfx_` | 音效事件（sfx event） | v1 |
| `sil_` | 静音/停顿事件（silence event） | v2 |
| `img_` | 关键图 / 画面元素（key image / visual） | v1 |
| `vid_` | 短视频段落 / 动画元素（video segment / animation） | v1 |
| `tml_` | 时间轴（timeline） | v1 |
| `exp_` | 导出产物（export artifact） | v1 |
| `job_` | 渲染任务（render job） | v1 |
| `voc_` | 音色（voice，一个角色的变体集合） | v2 |
| `vav_` | 音色变体（voice variant） | v2 |
| `cnd_` | 抽卡候选（candidate） | v2 |
| `act_` | 大纲幕（outline act） | v2 |
| `bea_` | 大纲节拍（outline beat） | v2 |
| `loc_` | 地点（location） | v2 |
| `thr_` | 未回收线索（open thread） | v2 |

`chr_narrator` 为保留 ID，代表旁白角色，不可删除；`chr_unknown` 表示脚本未能识别说话人。

## 2. 工程目录结构

一个作品 = 一个目录，可整体拷贝/备份，可进 git（媒体按 `.gitignore` 忽略）。

```
<projectRoot>/
├── project.json                     # 作品元数据 + 全局设置（含模型适配配置）
├── settings/
│   └── bible.json                   # v2 设定库：世界观 + 大纲 + 文风 + 连续性
├── characters.json                  # 角色表（跨章节复用，含音色绑定与状态）
├── voices.json                      # v2 音色库（音色 → 变体集合）
├── chapters/
│   └── <chapterId>.json             # 章节 + 正文块 + 续写会话（+ v1 内嵌 scenes，可选）
├── scripts/
│   └── <chapterId>.json             # v2 导演脚本：镜头/台词/音效/画面/动画/静音/转场
├── timelines/
│   └── <chapterId>.timeline.json    # 时间轴（可重建，属派生物）
├── assets/
│   ├── voice/<lineId>_r<rev>.wav            # 台词行最终音频（块拼接后）
│   ├── voice/chunks/<lineId>_k<idx>_r<rev>.wav  # v2 切块音频
│   ├── voices/ref/<variantId>.wav           # v2 克隆/自录参考音频
│   ├── voices/prompts/<variantId>.pt        # v2 voice_clone_prompt 缓存
│   ├── voices/preview/<variantId>.wav       # v2 试听音频
│   ├── voices/consent/<variantId>.txt       # v2 授权说明/凭证
│   ├── sfx/<sfxId>_r<rev>.wav
│   ├── image/<visualId>_c<cand>_r<rev>.png  # v2 带候选序号；v1 仍为 <imageId>_r<rev>.png
│   └── video/<animationId>_c<cand>_r<rev>.mp4
├── renders/<jobId>/                 # 任务中间产物与日志
├── exports/<exportId>/              # 成片、字幕、manifest
└── .ndw/
    ├── queue.json                   # 串行队列快照
    ├── locks/                       # 进程内/跨进程锁
    └── logs/                        # 任务日志
```

设计取舍：

- **章节内聚（v1 保留）**：v1 的场景、台词、音效、图、视频都内嵌在章节文件中，减少跨文件引用与读写竞争；单章文件预计 < 2MB。
- **v2 分文件**：设定库、导演脚本、音色库各自独立落盘。原因：设定库被四个工具读、被一个工具写；导演脚本会被频繁人工编辑与局部重算；音色库跨作品可复用。放同一文件会造成写竞争与整文件重写。
- **派生物分离**：时间轴与导出产物可随时重建，单独存放，不进 `chapters/`。
- **媒体按修订号命名**：`<实体>_r<rev>.wav`，改一行不会覆盖旧产物，便于对比与回滚，也天然支持幂等（同 rev 直接复用）。v2 抽卡再加候选号 `_c<cand>`，同一修订的多次生成不互相覆盖。

## 3. 实体总览

| 实体 | 文件 | 关键关系 | 主要必填字段 |
| --- | --- | --- | --- |
| 作品 work | `project.json` | 1 ↔ N 章节、1 ↔ N 角色 | `id` `title` `language` `schema_version` |
| 设定库 bible | `settings/bible.json` | 1 ↔ 1 作品；被章节/脚本/音色引用 | `schema_version` `settings_revision` `work_id` `premise` `world` `outline` `style` |
| 章节 chapter | `chapters/*.json` | N ↔ 1 作品；内嵌 1..N 正文块/续写会话（+ v1 场景） | `id` `work_id` `index` `title` `revision` |
| 正文块 block | 章节内嵌 | N ↔ 1 章节；锚点的定位基底 | `id` `index` `kind` `char_start` `char_end` `text_hash` |
| 续写会话 continuation | 章节内嵌 | N ↔ 1 章节 | `id` `mode` `status` `context` |
| 场景 scene（v1） | 章节内嵌 | N ↔ 1 章节；内嵌台词/音效/图/视频 | `id` `chapter_id` `index` |
| 导演脚本 script | `scripts/*.json` | 1 ↔ 1 章节；内嵌 1..N 镜头 | `id` `chapter_id` `work_id` `source` `status` `shots` |
| 镜头 shot | 脚本内嵌 | N ↔ 1 脚本；内嵌台词/音效/画面/动画/静音 | `id` `index` `scene_label` `anchor` `revision` |
| 角色 character | `characters.json` | 1 ↔ N 台词行（作为说话人） | `id` `name` `role` |
| 音色绑定 voice_binding | 角色内嵌 | 1 ↔ 1 角色；指向音色库 | `voice_library_id` `default_variant_id` |
| 音色 voice | `voices.json` | 1 ↔ N 变体；被角色/台词行引用 | `id` `engine` `default_variant_id` `variants` |
| 音色变体 variant | 音色内嵌 | N ↔ 1 音色 | `id` `source` `emotion_tags` `status` |
| 台词行 line | 场景/镜头内嵌 | N ↔ 1 镜头；N ↔ 1 角色 | `id` `kind` `speaker_id` `text` `anchor` `revision` |
| 旁白 narration | 台词行的一种 `kind` | 说话人为 `chr_narrator` | 同上 |
| 音效事件 sfx_event | 镜头内嵌 | N ↔ 1 镜头；位置可锚定台词行 | `id` `kind` `prompt` `duration_ms` `position` `anchor` |
| 静音事件 silence_event | 镜头内嵌 | N ↔ 1 镜头；锚定行/镜头边界 | `id` `duration_ms` `position` |
| 画面 visual / 关键图 | 镜头内嵌 | N ↔ 1 镜头；1 ↔ N 候选 | `id` `prompt` `anchor` `status` |
| 候选 candidate | 画面/动画内嵌 | N ↔ 1 画面或动画 | `id` `index` `seed` `params_hash` `status` |
| 动画 animation / 短视频段落 | 镜头内嵌 | N ↔ 1 镜头；可引用画面作首帧 | `id` `duration_ms` `mode` `anchor` |
| 时间轴 timeline | `timelines/*.json` | 1 ↔ 1 章节 | `id` `chapter_id` `revision` `tracks` |
| 渲染任务 render_job | `.ndw/queue.json` | 指向任意实体 | `id` `kind` `status` `idempotency_key` |
| 导出产物 export | `exports/*/manifest.json` | 1 ↔ 1 章节或作品 | `id` `kind` `path` `status` |

## 4. 作品（`project.json`）

```jsonc
{
  "schema_version": 1,
  "work": {
    "id": "wrk_01JBM7X3Q9K2R4T6V8W0Y2Z4A6",
    "title": "长夜将明",
    "author": "本地作者",
    "genre": ["悬疑", "都市"],
    "logline": "一句话简介",
    "language": "zh-CN",
    "usage": "personal",                  // v2：commercial|personal；决定许可硬过滤
    "style_guide": "第三人称限知，冷峻短句，少形容词",
    "target_duration_ms": 1800000,
    "created_at": "2026-11-09T12:00:00Z",
    "updated_at": "2026-11-09T12:00:00Z"
  },
  "settings": {
    "writing": {
      "adapter": "omlx",
      "protocol": "openai_compatible",     // omlx 实测：OpenAI 兼容本地 HTTP
      "endpoint": "http://127.0.0.1:9000", // 示例：无内置默认，用户在设置页填写
      "api_key": null,                     // 按 Authorization: Bearer <key> 传递；真实值只写在本地 config/config.yaml，不随工程落盘
      "model": "Qwen3.8-27B-oQ4e-mtp",     // 示例：运行时以 /v1/models 返回为准，由用户在设置页选择
      "available_models": [                 // 旧字段，仅兼容保留；页面下拉不再使用它
        "Qwen3.8-27B-oQ4e-mtp",            // 示例 ID
        "Qwen3.8-27B-Uncensored-OptiQ-4bit" // 示例 ID
      ],
      "context_window": null,              // 留空：由检测结果提供；填写才作为上限
      "memory_ceiling_gb": 29.48,          // 内存天花板，超出会 OOM
      "idle_timeout_s": null,              // null = 不自动卸载，必须显式卸载
      "max_concurrency": 8,                // 服务端能力上限；工作台仍串行使用
      "request_timeout_s": 300,
      "connect_timeout_s": 5,
      "max_retries": 2,
      "stop": [],
      "temperature": 0.8,
      "max_tokens": 2048,
      "enable_thinking": false,            // chat_template_kwargs.enable_thinking
      "stream": true,
      "unload": {
        "enabled": true,
        "path": "/v1/models/{model_id}/unload",
        "policy": "after_text_task"        // after_text_task|never|always
      }
    },
    "tts": {
      "adapter": "qwen3_tts",             // 兼容字段；v2 以 providers 为准
      "default_provider_id": "qwen3_tts_base",
      "provider_order": ["qwen3_tts_base"], // 回落链；只放 provider_id
      "providers": [                      // 完整定义在 config/config.yaml，工程只存引用
        { "provider_id": "qwen3_tts_base", "model": "Qwen3-TTS-1.7B-Base", "model_version": "1.7b-base-2026-10" }
      ],
      "routing": {                        // v2：自动路由
        "mode": "auto",                   // auto|manual
        "lock_provider_id": null,         // 工程级锁定；非空则跳过打分
        "weights": { "capability": 30, "quality": 25, "consistency": 20, "latency": 15, "resource": 10 },
        "policy_revision": 1,             // 权重或策略变更时 +1，触发重路由
        "retry_original_first": true,     // 降级后重跑先试原 provider
        "allow_noncommercial": false      // 非商用工程是否允许启用可选 provider
      },
      "default_voice_id": null,
      "voice_library_path": "voices.json",
      "max_chunk_ms": 30000,               // 单次合成建议上限
      "chunk_silence_ms": 180,             // 片段间静音
      "max_speed_delta": 0.2,              // atempo 超过 ±20% 需人工确认
      "comparison_keep": 3                 // 每句保留最近几次对比试听
    },
    "sfx": { "adapter": "placeholder" },
    "bible": { "path": "settings/bible.json", "revision": 3 },
    "tools": {
      "settings": "settings",  "novel": "chapters",
      "script": "scripts",     "voice": "assets/voice",
      "image": "assets/image", "video": "assets/video",
      "compose": "exports"
    },
    "comfy": {
      "base_url": "http://127.0.0.1:8188",
      "poll_interval_ms": 1000,
      "request_timeout_ms": 30000,
      "job_timeout_ms": 1800000,
      "templates": {
        "image": "qwen_image_2.1_3stage",
        "video_t2v": "minimax_h3_5s",
        "video_i2v": "ltx_2.3_i2v"
      }
    },
    "output": {
      "sample_rate": 48000,
      "channels": 2,
      "loudness_target_lufs": -16,
      "video_max_ms": 5000,
      "image_width": 1328,
      "image_height": 1328
    },
    "gpu": {
      "lock_path": "/Volumes/other/gpu.lock",
      "max_concurrent": 1,
      "unload_on_switch": "auto",          // auto|never|always
      "poll_interval_ms": 500
    }
  }
}
```

字段说明：

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `schema_version` | 是 | 工程结构版本，迁移依据 |
| `work.id` | 是 | 作品 ID |
| `work.title` | 是 | 作品名 |
| `work.author` / `logline` / `genre` | 否 | 展示与提示词拼装用 |
| `work.style_guide` | 否 | 全局文风约定，写作模型每次调用都会带上 |
| `work.target_duration_ms` | 否 | 目标成片时长，用于进度估算 |
| `settings.writing.protocol` | 是 | 实测为 `openai_compatible`；适配器据此选择 HTTP 客户端 |
| `settings.writing.endpoint` | 是 | omlx 地址（示例 `http://127.0.0.1:9000`，无内置默认） |
| `settings.writing.api_key` | 否 | 非空时按 `Authorization: Bearer <key>` 发送 |
| `settings.writing.model` | 是 | 请求体 `model` 字段；必须是 `/v1/models` 返回的 ID |
| `settings.writing.context_window` | 否 | 默认留空：由检测结果提供；填写才作为预算上限 |
| `settings.writing.memory_ceiling_gb` | 否 | 29.48；仅用于告警与调度提示，不参与计算 |
| `settings.writing.idle_timeout_s` | 是 | `null` 表示不会自动卸载，必须显式调用卸载 |
| `settings.writing.unload.*` | 是 | 卸载端点路径与策略；路径含 `{model_id}` 占位 |
| `settings.tts.adapter` | 否 | 兼容字段；v2 以 `providers[]` 为准 |
| `settings.tts.default_provider_id` | 是（用 TTS 时） | 工程默认 provider；行/角色未指定时使用 |
| `settings.tts.provider_order[]` | 是（用 TTS 时） | 回落链；只存 `provider_id`，取第一个健康且能力满足者 |
| `settings.tts.providers[]` | 是（用 TTS 时） | 工程内只存 `provider_id + model + model_version` 引用；完整配置（权重路径、密钥、能力）在外置 `config/config.yaml` |
| `settings.tts.max_chunk_ms` | 是 | 单次合成硬上限；长行按 15~30 秒切块 |
| `settings.bible.path` / `revision` | 是（用 v2 时） | 设定库路径与当前版本；工具据 `revision` 判定 stale |
| `settings.comfy.templates` | 是（用图/视频时） | 模板 ID → 工作流文件名的映射，模板本身不在工程内 |
| `settings.output.video_max_ms` | 是 | 短视频硬上限，固定 5000，UI 与校验双重限制 |
| `settings.gpu.lock_path` | 是 | 与 ComfyUI 互斥的锁文件路径 |
| `settings.gpu.max_concurrent` | 是 | 固定 1；文本与 ComfyUI 抢 GPU，串行使用 |
| `settings.gpu.unload_on_switch` | 是 | 工具切换时是否卸载 omlx 模型：`auto`（下一个是非文本 GPU 任务才卸载）/`never`/`always` |

> **不做模型管理**：工程里不存权重路径、不存模型清单、不存下载任务；写作能力完全交给 omlx，图像视频完全交给 ComfyUI。`available_models` 只是实测快照，不用于下载或校验权重是否存在。

## 5. 角色（`characters.json`）

```jsonc
{
  "schema_version": 1,
  "characters": [
    {
      "id": "chr_01JBM7X3Q9K2R4T6V8W0Y2Z4A6",
      "name": "沈砚",
      "aliases": ["小沈", "沈队"],
      "role": "protagonist",              // protagonist|antagonist|supporting|narrator|minor
      "gender": "male",
      "age_range": "30-40",
      "personality": ["克制", "偏执"],
      "background": "刑警，三年前办过错案",
      "speech_style": {
        "tempo": "偏慢",
        "pitch": "偏低",
        "accent": "普通话",
        "catchphrases": ["说重点"]
      },
      "voice_binding": {
        "adapter": "qwen3_tts",
        "voice_library_id": "voc_01JBM90008",     // v2：指向 voices.json 的音色
        "default_variant_id": "vav_01JBM90009",   // v2：默认变体
        "emotion_variant_map": {                  // v2：情绪 → 变体；未命中回落默认变体
          "angry": "vav_01JBM90010",
          "sad": "vav_01JBM90011"
        },
        "fallback_voice_id": null,                // v2：变体不可用时换用的音色
        "voice_id": null,                         // v1 兼容字段，v2 留空
        "reference_audio": null,                  // v1 遗留；v2 统一放 assets/voices/ref/
        "seed": 1234,
        "speed": 0.95                             // 后期 atempo 系数，不传给 TTS
      },
      "state": {                                  // v2：角色当前状态卡，供续写一致性使用
        "situation": "停职调查中，私下查旧案",
        "relationship_notes": { "chr_01JBM7X4A1": "对搭档保持隐瞒" },
        "knowledge": ["卷宗缺页", "三年前的错案"],
        "as_of_chapter_id": "chp_01JBM7X3Q9"
      },
      "appearance_prompt": "35 岁男性刑警，短寸头，深色风衣，冷光",
      "negative_prompt": "低质, 多余手指, 水印",
      "portrait_asset": null,
      "created_at": "2026-11-09T12:00:00Z",
      "updated_at": "2026-11-09T12:00:00Z"
    }
  ]
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `id` | 是 | 角色 ID；`chr_narrator` 固定为旁白 |
| `name` | 是 | 正式名，台词切分时用于识别说话人 |
| `aliases` | 否 | 别名/称呼，参与说话人识别 |
| `role` | 是 | 定位，`narrator` 仅旁白使用 |
| `personality` / `background` / `speech_style` | 否 | 写作与提示词拼装用 |
| `voice_binding.voice_library_id` / `default_variant_id` | 用 TTS 时必填（v2） | 角色默认音色与默认变体；缺失时该角色的行标记 `voice_missing` |
| `voice_binding.emotion_variant_map` | 否（v2） | 统一情绪枚举 → 音色变体 ID；未命中回落 `default_variant_id`（降级规则见 §16.5） |
| `voice_binding.fallback_voice_id` | 否（v2） | 变体不可用（缓存失败/授权未确认）时的备用音色 |
| `voice_binding.speed` | 否 | 后期 atempo 系数；Qwen3-TTS 无原生语速参数 |
| `state` | 否（v2） | 角色当前处境/关系/已知信息，章节完成后更新，供续写与一致性检查 |
| `appearance_prompt` | 用图时必填 | 角色外貌提示词，出图时拼进场景提示词 |
| `negative_prompt` | 否 | 负向提示词 |
| `portrait_asset` | 否 | 角色定妆图（后续角色一致性使用） |

## 6. 章节与场景

### 6.1 章节

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `id` | 是 | 章节 ID |
| `work_id` | 是 | 所属作品 |
| `index` | 是 | 章节序号，从 1 开始 |
| `title` | 是 | 章节名 |
| `outline` | 否 | 章节大纲（人工或写作模型生成） |
| `synopsis` | 否 | 章节摘要（长上下文压缩用，逐章滚动更新） |
| `content` | 否 | 章节原始正文（Markdown/纯文本） |
| `pov_character_id` | 否 | 视角角色 |
| `word_count` | 否 | 字数（去除空白，写入时计算） |
| `status` | 是 | v2：`draft` → `written` → `scripted` → `voiced` → `visualized` → `composed`，另有 `stale`；v1 的 `split` 保留 |
| `blocks` | 否（v2） | 正文段落块数组，锚点的定位基底；由正文派生，可重建 |
| `script_path` | 否（v2） | 导演脚本相对路径，默认 `scripts/<chapterId>.json`；为空表示尚未生成 |
| `continuations` | 否（v2） | 续写会话数组；每次续写一条记录，可追溯指令与产出区间 |
| `scenes` | 否 | v1 场景数组，拆分后写入（v2 新工程可留空，改用 `scripts/`） |
| `revision` | 是 | 内容版本，任何字段变更 +1 |
| `created_at` / `updated_at` | 是 | 时间戳 |

### 6.2 正文块（block，v2）

```jsonc
{
  "id": "blk_01JBM90000",
  "index": 0,
  "kind": "paragraph",            // paragraph|dialogue|heading
  "char_start": 0,                // 相对整章正文的偏移（Unicode 码点，0 基）
  "char_end": 120,                // 不含
  "text_hash": "sha256:..."
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `id` | 是 | 块 ID；正文未改动的段落上保持稳定（按 `text_hash + 顺序` 匹配） |
| `index` | 是 | 块顺序 |
| `kind` | 是 | 段落类型；`dialogue` 块通常以引号包裹 |
| `char_start` / `char_end` | 是 | 相对整章正文的区间，用于锚点定位 |
| `text_hash` | 是 | 规范化后文本哈希（去首尾空白、全半角统一） |

### 6.3 续写会话（continuation，v2）

```jsonc
{
  "id": "cnt_01JBM90007",
  "mode": "continue",             // continue|outline_driven|rewrite_range
  "instruction": "沈砚发现卷宗缺页后，先怀疑内鬼，不要立刻摊牌",
  "target_words": 1200,
  "outline_ref": "outline.chapter_01.beat_3",
  "range": null,                  // rewrite_range 时必填 {char_start, char_end}
  "context": {
    "include_bible": true,
    "include_character_cards": ["chr_01JBM7X3Q9"],
    "include_previous_summaries": 3,
    "include_recent_chars": 4000,
    "budget_tokens": 120000
  },
  "status": "done",               // pending|running|done|failed|canceled
  "produced": { "char_start": 5200, "char_end": 6800 },
  "model": "Qwen3.8-27B-oQ4e-mtp", // 示例：实际为用户在设置页所选模型
  "usage": { "prompt_tokens": 0, "completion_tokens": 0 },
  "created_at": "2026-11-09T12:00:00Z"
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `mode` | 是 | `continue` 按思路续写 / `outline_driven` 按大纲自动续写 / `rewrite_range` 改写指定区间 |
| `instruction` | 否 | 用户的续写思路；为空则纯按大纲续写 |
| `target_words` | 否 | 目标字数，默认 1000 |
| `outline_ref` | 否 | 大纲节点引用（`outline.acts[].chapters[].beats[].id`） |
| `range` | rewrite 时必填 | 待改写区间（字符偏移） |
| `context.*` | 是 | 上下文装配参数，决定输入预算与包含项 |
| `status` | 是 | 会话状态；`running` 中断重启后回 `pending` |
| `produced` | 否 | 实际产出的正文区间，用于定位插入位置与增量重算 |

### 6.4 场景（v1 遗留）

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `id` | 是 | 场景 ID |
| `chapter_id` | 是 | 所属章节 |
| `index` | 是 | 场景序号 |
| `title` | 否 | 场景名（默认「场景 N」） |
| `location` | 否 | 地点，影响环境音选择 |
| `time_of_day` | 否 | `dawn`/`morning`/`day`/`dusk`/`night`/`unknown` |
| `mood` | 否 | 情绪基调，影响音效与配乐 |
| `goal` | 否 | 本场戏剧目标（写作模型拆场用） |
| `summary` | 否 | 场景摘要（喂给后续场景的上下文） |
| `source_text` | 否 | 场景原始正文，供切分溯源与重新切台词 |
| `character_ids` | 否 | 出场角色 |
| `target_duration_ms` | 否 | 目标时长，用于配图/配乐规划 |
| `status` | 是 | `draft` → `ready` → `voiced` → `done` |
| `lines` | 否 | 台词行数组 |
| `sfx_events` | 否 | 音效事件数组 |
| `key_images` | 否 | 关键图数组 |
| `video_segments` | 否 | 短视频段落数组 |
| `revision` | 是 | 版本号 |

## 7. 台词行与旁白

台词行是时间轴的最小锚点，也是配音的最小单位。**旁白不是单独实体**，是 `kind = "narration"` 且 `speaker_id = "chr_narrator"` 的台词行，这样排版、时长估算、时间轴、重跑逻辑全部复用一套。

```jsonc
{
  "id": "lin_01JBM7XG01",
  "scene_id": "scn_01JBM7X3Q9",      // v1 内嵌场景时填写；v2 由所在镜头隐含，可省略
  "index": 3,
  "kind": "dialogue",                 // dialogue|narration|inner_monologue
  "speaker_id": "chr_01JBM7X3Q9",
  "voice": {                          // v2：行级音色覆盖
    "provider_id": null,              // 行级 provider；为空走角色 → 工程默认 → provider_order
    "voice_id": "voc_01JBM90008",     // 为空表示用角色默认绑定
    "variant_id": null,               // 直接指定变体；为空按情绪映射解析
    "speed": 0.95
  },
  "text": "说重点。",
  "emotion": "serious",               // neutral|joy|sad|angry|fear|surprise|tender|excited|whisper|serious
  "emotion_intensity": 0.7,           // 0~1；折算 0~3 四档，用于选同强度变体
  "paralinguistic": [],                // v2：laugh|sigh|breath|cry|whisper；路由硬条件
  "target_duration_ms": null,          // v2：期望时长；触发时长补偿策略
  "speed": 0.95,                      // 0.5~2.0；后期 atempo 变速，不传给 TTS
  "pause_before_ms": 200,
  "pause_after_ms": 400,
  "delivery_note": "压低声音",        // 仅人工参考，不传给 TTS
  "anchor": {                         // v2 必填
    "block_id": "blk_01JBM90000",
    "char_start": 120,
    "char_end": 125,
    "text_hash": "sha256:...",
    "state": "exact"                  // exact|shifted|orphan
  },
  "inferred": true,
  "confidence": 0.86,
  "tts": {
    "status": "ready",                // pending|running|ready|failed|stale
    "asset_id": "assets/voice/lin_01JBM7XG01_r2.wav",
    "duration_ms": 1430,
    "adapter": "qwen3_tts",
    "provider_id": "qwen3_tts_base",  // v2：实际使用的 provider
    "model_version": "1.7b-base-2026-10", // v2：实际模型版本，切换后重算依据
    "voice_id": "voc_01JBM90008",
    "variant_id": "vav_01JBM90009",   // v2：实际解析到的变体，供溯源
    "routing": {                      // v2：自动路由决策（可解释、可复现）
      "mode": "auto",                 // auto|locked_line|locked_character|locked_project|manual
      "policy_revision": 1,
      "input_hash": "sha256:...",
      "selected": { "provider_id": "qwen3_tts_base", "variant_id": "vav_01JBM90009", "score": 86, "reasons": ["情绪按变体匹配", "同角色历史一致", "许可允许商用"] },
      "rejected": [
        { "provider_id": "fish_s2_pro", "stage": "license", "reason": "LICENSE_BLOCKED", "detail": "权重为 proprietary 许可，商用需单独授权" }
      ],
      "degradations": [],             // emotion_intensity_downgrade|provider_fallback|nonverbal_to_text
      "decided_at": "2026-11-09T12:00:00Z"
    },
    "chunks": [                       // v2：切块合成记录
      {
        "index": 0,
        "text": "说重点。",
        "asset_id": "assets/voice/chunks/lin_01JBM7XG01_k0_r2.wav",
        "duration_ms": 1430,
        "provider_id": "qwen3_tts_base",
        "variant_id": "vav_01JBM90009",
        "status": "ready"
      }
    ],
    "synthesized_revision": 2,
    "actual_speed": null,             // 原生无变速；仅记录后期处理
    "error": null,
    "updated_at": "2026-11-09T12:10:00Z"
  },
  "revision": 2
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `id` | 是 | 台词行 ID |
| `scene_id` | v1 必填 | 所属场景（冗余，便于独立定位）；v2 由镜头隐含 |
| `index` | 是 | 所在镜头/场景内顺序 |
| `kind` | 是 | `dialogue` 对白 / `narration` 旁白 / `inner_monologue` 内心独白 |
| `speaker_id` | 是 | 说话角色；旁白固定 `chr_narrator`；未识别用 `chr_unknown` |
| `voice.voice_id` | 否（v2） | 行级音色覆盖，优先于角色绑定 |
| `voice.variant_id` | 否（v2） | 行级变体覆盖；为空按 §16.5 顺序解析 |
| `text` | 是 | 实际要念的文本（不含「他说」等提示语，提示语归入旁白） |
| `emotion` | 是 | 统一枚举（10 值）；克隆音色下用于**选变体**，不传给 TTS |
| `emotion_intensity` | 否 | 0~1，默认 0.5；仅人工提示，无引擎参数 |
| `speed` | 否 | 后期 atempo 系数，默认 1.0，0.5~2.0；**不是 TTS 参数** |
| `pause_before_ms` / `pause_after_ms` | 否 | 行内前后自然停顿，参与时间轴排版 |
| `delivery_note` | 否 | 自由文本表演提示；克隆模型不支持自然语言指令，仅人工参考 |
| `anchor` | v2 必填 | 回指正文段落块；定位算法见 §18.1 |
| `tts.status` | 是 | 合成状态；`synthesized_revision` 与行 `revision` 不等即视为过期 |
| `tts.duration_ms` | 否 | 实测时长；为空时用 `estimate_duration_ms(text, speed)` 估算 |
| `tts.variant_id` | 否（v2） | 本次实际使用的变体，音色漂移排查依据 |
| `tts.chunks[]` | 否（v2） | 切块记录；重录只重跑文本变化的块 |
| `revision` | 是 | 行内容版本（text/情绪/停顿/音色任一变化即 +1） |

**过期判定**：`tts.synthesized_revision != line.revision`、`tts.variant_id != 当前解析变体` 或 `tts.voice_id != 角色当前音色` → 需要重跑。

## 8. 音效事件

```jsonc
{
  "id": "sfx_01JBM7Y001",
  "scene_id": "scn_01JBM7X3Q9",
  "index": 1,
  "kind": "ambience",                 // ambience|foley|impact|transition|music_sting
  "prompt": "深夜警局走廊，日光灯电流声，远处键盘声",
  "prompt_en": "Late-night police station corridor, fluorescent hum, distant keyboard clicks",
  "duration_ms": 12000,
  "loop": true,
  "gain_db": -12.0,
  "fade_in_ms": 800,
  "fade_out_ms": 1200,
  "position": {
    "mode": "during_line",            // before_line|after_line|during_line|shot_start|shot_end|absolute
    "line_id": "lin_01JBM7XG01",
    "offset_ms": 0                    // during_line 时为进入该行的偏移；absolute 时作为绝对时间
  },
  "anchor": {                         // v2 必填
    "block_id": "blk_01JBM90000",
    "char_start": 120,
    "char_end": 125,
    "text_hash": "sha256:...",
    "state": "exact"
  },
  "asset": {
    "status": "ready",                // pending|running|ready|failed|skipped
    "asset_id": "assets/sfx/sfx_01JBM7Y001_r1.wav",
    "actual_duration_ms": 12000,
    "adapter": "placeholder",
    "cache_key": null,
    "error": null
  },
  "revision": 1
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `kind` | 是 | 音效类型，决定默认时长/音量模板 |
| `prompt` | 是 | 生成或检索用的描述 |
| `duration_ms` | 是 | 期望时长；循环音效为单段长度 |
| `loop` | 否 | 是否循环铺满锚定范围 |
| `gain_db` | 否 | 相对原始电平，默认 -12 |
| `position.mode` | 是 | 插入位置模式（相对台词行、镜头边界或绝对时间） |
| `position.line_id` | 相对模式必填 | 锚定台词行；该行被删除时事件降级为镜头开头并告警 |
| `position.offset_ms` | 否 | 相对锚点的偏移 |
| `anchor` | v2 必填 | 回指正文；用于改文后判断是否需要重排 |
| `asset.status` | 是 | 产物状态，失败不阻塞导出（该事件静音） |

## 9. 关键图

```jsonc
{
  "id": "img_01JBM7Z001",
  "scene_id": "scn_01JBM7X3Q9",      // v1；v2 由镜头隐含
  "index": 1,
  "anchor_line_id": "lin_01JBM7XG01", // v1 兼容；v2 用 anchor
  "anchor": {                         // v2 必填
    "block_id": "blk_01JBM90000",
    "char_start": 120,
    "char_end": 180,
    "text_hash": "sha256:...",
    "state": "exact"
  },
  "shot_type": "中景",               // v2：特写|中景|全景|空镜
  "composition": "人物在画面左侧，右侧是翻开的卷宗",
  "character_ids": ["chr_01JBM7X3Q9"],
  "action": "沈砚低头看卷宗，手指停在缺口处",
  "style": "写实电影感，冷调日光灯，浅景深",
  "anchor_desc": "沈砚第一次看到卷宗缺页",
  "prompt": "35 岁男性刑警站在深夜警局，翻开的卷宗缺页，冷调日光灯，电影感侧逆光",
  "negative_prompt": "低质, 多余手指, 水印",
  "prompt_source": "auto",             // auto|manual
  "style_ref": "写实电影感",
  "aspect_ratio": "3:4",
  "width": 1152,
  "height": 1536,
  "workflow_template_id": "qwen_image_2.1_3stage",
  "params": { "steps": 8, "cfg": 1.0 },
  "draw": {                             // v2：抽卡参数
    "batch_size": 4,
    "candidate_retention_days": 7,
    "selected_candidate_id": "cnd_01JBM90009"
  },
  "candidates": [ /* 见 §17，候选结构见 director-script.schema.json#/$defs/candidate */ ],
  "status": "selected",                 // pending|queued|generating|candidates_ready|selected|failed|stale
  "asset": {
    "status": "ready",                 // v1 兼容字段
    "asset_id": "assets/image/img_01JBM7Z001_c1_r1.png",
    "job_id": null,
    "error": null
  },
  "revision": 1
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `anchor_line_id` | v1 必填 | 所属段落锚点；图在时间轴上出现在该行附近 |
| `anchor` | v2 必填 | 回指正文段落块；改文后据此判断是否需要重画 |
| `anchor_desc` | 否 | 画面内容简述，人工可读 |
| `shot_type` / `composition` / `character_ids` / `action` / `style` | 否（v2） | 结构化画面描述，供人工校对与提示词重拼 |
| `prompt` | 是 | 正向提示词（角色外貌 + 场景 + 镜头语言拼装结果） |
| `prompt_source` | 是 | 是否由模型自动生成，便于批量重生成时区分 |
| `aspect_ratio` / `width` / `height` | 是 | 出图规格；竖屏/横屏由成片规格决定 |
| `workflow_template_id` | 是 | ComfyUI 模板 ID，仅引用不内嵌工作流 |
| `params` | 否 | 覆盖模板默认参数的少量键（步数、CFG 等） |
| `draw.batch_size` | 否（v2） | 本轮抽卡张数，默认 4 |
| `draw.candidate_retention_days` | 否（v2） | 未选中候选保留天数，默认 7 |
| `draw.selected_candidate_id` | 否（v2） | 选中候选；进入时间轴的是它 |
| `candidates[]` | 否（v2） | 候选列表，追加式，不覆盖历史 |
| `status` | 否（v2） | 画面状态；v1 数据缺省时按 `asset.status` 推断 |

## 10. 短视频段落

```jsonc
{
  "id": "vid_01JBM80001",
  "scene_id": "scn_01JBM7X3Q9",      // v1；v2 由镜头隐含
  "anchor_line_id": "lin_01JBM7XG01", // v1 兼容；v2 用 anchor
  "anchor": {                         // v2 必填
    "block_id": "blk_01JBM90000",
    "char_start": 180,
    "char_end": 240,
    "text_hash": "sha256:...",
    "state": "exact"
  },
  "start_offset_ms": 0,
  "duration_ms": 5000,
  "mode": "i2v",                     // t2v|i2v
  "source_image_id": "img_01JBM7Z001",
  "visual_id": "img_01JBM7Z001",     // v2：与 source_image_id 等价，新字段名
  "motion": "镜头缓慢推进，日光灯轻微闪烁，人物沉默转头",
  "camera_move": "slow_push_in",     // static|slow_push_in|pan_left|pan_right|handheld
  "prompt": "镜头缓慢推进，日光灯轻微闪烁，人物沉默转头",
  "negative_prompt": "画面抖动, 变形",
  "fps": 24,
  "workflow_template_id": "ltx_2.3_i2v",
  "params": {},
  "audio_policy": "mute",            // keep|mute|duck（与配音冲突时的处理）
  "fallback_image_id": "img_01JBM7Z001",
  "draw": {                           // v2：抽卡参数
    "batch_size": 2,
    "candidate_retention_days": 7,
    "selected_candidate_id": null
  },
  "candidates": [],
  "status": "pending",                // pending|queued|generating|candidates_ready|selected|failed|skipped|stale
  "asset": { "status": "pending", "asset_id": null, "job_id": null, "error": null },
  "revision": 1
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `duration_ms` | 是 | **硬上限 5000**，schema 用 `maximum: 5000` 约束，UI 不给超过的输入 |
| `mode` | 是 | `t2v` 文生视频，`i2v` 图生视频 |
| `source_image_id` / `visual_id` | i2v 必填 | 首帧关键图；不存在时任务失败并提示先生成图 |
| `anchor` | v2 必填 | 回指正文；用于改文后判断是否需重做 |
| `camera_move` | 否（v2） | 运镜方式，拼入提示词 |
| `audio_policy` | 是 | 视频自带音轨与配音的关系：保留/静音/压低 |
| `fallback_image_id` / `fallback_visual_id` | 否 | 视频失败时回退的静态图，避免成片缺画面 |
| `start_offset_ms` | 否 | 相对锚点行的起始偏移 |
| `draw` / `candidates[]` / `status` | 否（v2） | 抽卡参数与候选，语义同画面 |

## 11. 时间轴

时间轴是纯派生物，可随时由「台词行 + 音效 + 图 + 视频」重建；但仍落盘以便人工微调后复用。

```jsonc
{
  "schema_version": 1,
  "id": "tml_01JBM81001",
  "chapter_id": "chp_01JBM7X3Q9",
  "revision": 3,
  "duration_ms": 182340,
  "sample_rate": 48000,
  "channels": 2,
  "built_at": "2026-11-09T13:00:00Z",
  "source_hash": "sha256:...",
  "tracks": [
    {
      "id": "trk_voice",
      "kind": "voice",                // voice|sfx|music|image|video
      "name": "配音",
      "items": [
        {
          "source_kind": "line",      // line|sfx|key_image|video_segment
          "source_id": "lin_01JBM7XG01",
          "asset_id": "assets/voice/lin_01JBM7XG01_r2.wav",
          "start_ms": 12400,
          "end_ms": 13830,
          "gain_db": 0.0,
          "fade_in_ms": 20,
          "fade_out_ms": 40,
          "estimated": false
        }
      ]
    }
  ],
  "markers": [{ "at_ms": 0, "label": "第一章 开场" }],
  "warnings": ["lin_01JBM7XH02 使用估算时长（未合成）"]
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `revision` | 是 | 时间轴版本；人工微调后 +1 |
| `source_hash` | 是 | 生成时输入的哈希，用于判断是否需重建 |
| `tracks[].kind` | 是 | 五类轨道，顺序固定：voice → sfx → music → video → image |
| `items[].start_ms` / `end_ms` | 是 | 绝对时间；`end_ms > start_ms` |
| `items[].estimated` | 是 | 该片段时长是否为估算（未实测），导出时给警告 |
| `warnings` | 否 | 生成期发现的降级/缺失问题 |

## 12. 渲染任务

```jsonc
{
  "id": "job_01JBM82001",
  "kind": "tts",                     // writing|tts|sfx|image|video|mix|export
  "target": { "chapter_id": "chp_01JBM7X3Q9", "scene_id": "scn_01JBM7X3Q9", "line_id": "lin_01JBM7XG01" },
  "status": "queued",                // queued|running|succeeded|failed|canceled|interrupted|skipped
  "priority": 100,                   // 数值越小越先执行
  "idempotency_key": "sha256:...",
  "params_hash": "sha256:...",
  "attempts": 0,
  "max_attempts": 3,
  "depends_on": [],
  "adapter": "placeholder",
  "error": null,                     // { code, message, retryable, at }
  "artifacts": [],                   // [{ kind, path, meta }]
  "timings": { "queued_at": "...", "started_at": null, "finished_at": null, "duration_ms": null },
  "logs_path": ".ndw/logs/job_01JBM82001.log",
  "created_at": "2026-11-09T13:00:00Z"
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `kind` | 是 | 任务类型，决定用哪个适配器 |
| `target` | 是 | 作用对象，至少含一个实体 ID |
| `status` | 是 | 状态机见下 |
| `priority` | 是 | 排序键，同优先级按创建时间 |
| `idempotency_key` | 是 | 幂等键 = hash(kind + target + 输入 revision + adapter + params_hash) |
| `attempts` / `max_attempts` | 是 | 重试计数与上限（默认 3，指数退避） |
| `depends_on` | 否 | 前置任务 ID；前置失败则本任务 `skipped` |
| `error.code` | 否 | 统一错误码：`NOT_IMPLEMENTED` `UNAVAILABLE` `TIMEOUT` `RATE_LIMITED` `INVALID_INPUT` `UPSTREAM_ERROR` `CONTENT_BLOCKED` |
| `error.retryable` | 否 | 是否可自动重试，由适配器判定 |

## 13. 导出产物

```jsonc
{
  "id": "exp_01JBM83001",
  "scope": "chapter",                // chapter|work
  "chapter_id": "chp_01JBM7X3Q9",
  "kind": "mp4",                     // mp4|mp3|wav|srt|manifest
  "path": "exports/exp_01JBM83001/chapter-01.mp4",
  "status": "ready",                 // pending|running|ready|failed
  "duration_ms": 182340,
  "size_bytes": 48234120,
  "spec": {
    "sample_rate": 48000,
    "channels": 2,
    "loudness_lufs": -16,
    "video": { "width": 1080, "height": 1920, "fps": 24 }
  },
  "timeline_revision": 3,
  "created_at": "2026-11-09T13:10:00Z"
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `scope` | 是 | 章节级或整部作品 |
| `kind` | 是 | 产物类型；一次导出可产出多条（mp4 + srt + manifest） |
| `path` | 是 | 相对工程根目录 |
| `timeline_revision` | 是（音视频） | 对应时间轴版本，便于判断是否过期 |
| `spec` | 否 | 规格快照，保证可复现 |

## 14. 设定库（`settings/bible.json`，v2）

设定工坊唯一写入；小说、导演脚本、配音、图像、视频只读。`settings_revision` 任何变更 +1，下游据此判 `stale`。

```jsonc
{
  "schema_version": 1,
  "settings_revision": 3,
  "work_id": "wrk_01JBM7X3Q9K2R4T6V8W0Y2Z4A6",
  "premise": {
    "logline": "一名停职刑警追查卷宗缺页，牵出三年前的错案",
    "theme": "真相与代价",
    "core_conflict": "查下去会毁掉同事，也毁掉自己",
    "ending_intent": "开放式，主角选择公开真相",
    "target_length": "novel"
  },
  "world": {
    "era": "当代都市",
    "locations": [
      { "id": "loc_01JBM90001", "name": "警局走廊", "atmosphere": "深夜日光灯，回声长" }
    ],
    "rules": ["无超自然元素"],
    "factions": ["市局刑侦支队"],
    "glossary": [{ "term": "卷宗缺页", "meaning": "本案关键证据被抽走", "aliases": ["缺页"] }]
  },
  "outline": {
    "acts": [
      {
        "id": "act_01JBM90002",
        "title": "第一幕 发现",
        "chapters": [
          {
            "id": "chp_01JBM7X3Q9",
            "index": 1,
            "title": "缺页",
            "summary": "沈砚发现卷宗缺页",
            "beats": [
              { "id": "bea_01JBM90003", "summary": "翻卷宗发现缺页", "turning_point": true, "target_words": 1200 }
            ],
            "pov_character_id": "chr_01JBM7X3Q9"
          }
        ]
      }
    ]
  },
  "style": {
    "tone": "冷峻、克制",
    "pov": "third_limited",
    "tense": "past",
    "prose_style": "短句为主，少形容词",
    "forbidden": ["网络流行语"],
    "visual_style": "写实电影感，冷调，浅景深"
  },
  "continuity": {
    "timeline": [{ "at": "第 1 天 23:40", "event": "发现缺页", "chapter_id": "chp_01JBM7X3Q9" }],
    "open_threads": [{ "id": "thr_01JBM90004", "text": "谁抽走了缺页", "status": "open" }]
  },
  "characters_ref": "characters.json",
  "voices_ref": "voices.json",
  "created_at": "2026-11-09T12:00:00Z",
  "updated_at": "2026-11-09T12:00:00Z"
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `settings_revision` | 是 | 设定版本；所有工具记录依赖版本，不等即 stale |
| `premise.logline` | 是 | 一句话故事；其余 premise 字段为续写约束 |
| `world.era` | 是 | 时代背景；`locations[]` 供音效/画面提示词，`glossary` 供一致性检查 |
| `outline.acts[]` | 是 | 幕 → 章 → beat 三层；`beat.id` 被续写会话的 `outline_ref` 引用 |
| `style.tone` | 是 | 文风基调；`visual_style` 供全书画面统一 |
| `continuity` | 否 | 时间线与未回收线索，续写与校验用 |
| `characters_ref` / `voices_ref` | 否 | 角色表与音色库相对路径，默认根目录同名文件 |

## 15. 导演脚本（`scripts/<chapterId>.json`，v2）

导演脚本是「正文 → 可制作」的中间层，字段定义以 `docs/design-v2.md` §2 为准，此处只列落盘结构与引用关系。完整字段与必填性见 `schema/director-script.schema.json`。

```jsonc
{
  "schema_version": 1,
  "id": "scr_01JBM90001",
  "chapter_id": "chp_01JBM7X3Q9",
  "work_id": "wrk_01JBM7X3Q9K2R4T6V8W0Y2Z4A6",
  "source": { "chapter_revision": 12, "settings_revision": 3, "content_hash": "sha256:..." },
  "status": "draft",                 // draft|ready|stale
  "style": { "visual_style": "...", "narration_voice_id": "voc_narrator_01", "bgm_mood": "低沉弦乐" },
  "shots": [
    {
      "id": "sht_01JBM90002",
      "index": 1,
      "scene_label": "深夜·警局走廊",
      "anchor": { "block_id": "blk_01JBM90000", "char_start": 0, "char_end": 120, "text_hash": "sha256:...", "state": "exact" },
      "transition_in": { "type": "cut", "duration_ms": 0 },
      "transition_out": { "type": "fade", "duration_ms": 600 },
      "lines": [ /* 同 §7，anchor 必填 */ ],
      "sfx_events": [ /* 同 §8 */ ],
      "visuals": [ /* 同 §9 */ ],
      "animations": [ /* 同 §10 */ ],
      "silence_events": [ /* 见 §16.6 */ ],
      "revision": 1
    }
  ],
  "warnings": [],
  "created_at": "2026-11-09T12:00:00Z",
  "updated_at": "2026-11-09T12:00:00Z"
}
```

| 结构 | 必填 | 说明 |
| --- | --- | --- |
| `source` | 是 | 生成依赖快照；`chapter_revision` / `settings_revision` / `content_hash` 任一不等即 `stale` |
| `status` | 是 | `draft` 可编辑 → `ready` 可下游消费 → `stale` 需重算 |
| `shots[].anchor` | 是 | 镜头级锚点，定位到正文块区间 |
| `shots[].lines[].anchor` | 是 | 行级锚点；行是配音最小单位 |
| `silence_events[]` | 否 | 显式静音/停顿，替代 SSML |

## 16. 音色库、TTS Provider 与变体（v2）

**配音不绑死单一 TTS**：`voices.json` 只存共用资产（参考音频、变体、授权），provider 配置与能力在 `config/config.yaml`；台词行记录实际使用的 provider 与模型版本以便溯源与重算。

### 16.1 Provider 抽象与能力矩阵

统一接口（请求/结果/错误）见 `docs/design-v2.md` §5.1。落盘只保留「结果溯源」与「配置引用」，不存 provider 内部实现细节。

```jsonc
// config/config.yaml → tts.providers[]
{
  "provider_id": "qwen3_tts_base",
  "display_name": "Qwen3-TTS 1.7B Base（本地）",
  "model": "Qwen3-TTS-1.7B-Base",
  "model_version": "1.7b-base-2026-10",
  "backend": "http",                    // mlx|torch_mps|torch_cuda|http|cli
  "weights_path": "/Users/me/models/Qwen3-TTS-1.7B-Base",
  "endpoint": "http://127.0.0.1:9100",
  "api_key_env": "QWEN3_TTS_KEY",       // 密钥走环境变量；不落工程、不落源码
  "health_path": "/health",
  "vram_estimate_gb": 11,
  "max_concurrent": 1,
  "supports_batch": false,
  "unload": { "enabled": true, "path": "/v1/models/{model}/unload", "policy": "on_switch" },
  "capabilities": {
    "emotion_control": "reference",     // instruct|reference|none
    "emotion_instruction": false,
    "emotion_reference_variants": true,
    "emotion_intensity_levels": 4,      // 情绪强度分档数；0=不分档
    "voice_clone": true,
    "clone_min_ref_ms": 3000,
    "clone_recommended_ref_ms": 5000,
    "clone_prompt_cache": true,
    "max_variants_per_voice": 12,
    "max_text_chars": 120,
    "max_text_ms": 30000,
    "nonverbal": ["laugh", "sigh"],
    "nonverbal_control": "reference",   // native|reference|none
    "native_duration_control": false,
    "sample_rates": [24000, 48000],
    "output_formats": ["wav", "mp3"],
    "ssml": false,
    "pause_control": "silence_event",   // ssml|silence_event|none
    "streaming": true,
    "native_speed": false,
    "native_pitch": false,
    "languages": ["zh-CN", "en-US"],
    "license": "Apache-2.0",
    "license_gate": "none",             // none|approval_required|non_commercial
    "commercial_use": true,
    "license_note": "允许商用；克隆需声音所有者授权",
    "quality_tier": "standard",         // draft|standard|premium
    "latency_tier": "standard",         // fast|standard|slow
    "consistency": { "stability": "high", "pin_per_character": true },
    "allow_auto_select": true
  }
}
```

| 字段 | 必填 | 上层行为 |
| --- | --- | --- |
| `provider_id` | 是 | 全局唯一；台词行、角色、音色变体都引用它 |
| `model` / `model_version` | 是 | 写入产物溯源；版本变化即失效重算 |
| `backend` | 是 | 判断显存、卸载方式与是否走 GPU 锁 |
| `weights_path` / `endpoint` | 本地后端必填 | 只在本机配置；工程迁移后按 `provider_id` 重绑 |
| `api_key` / `api_key_env` | 否 | 二选一，禁止明文进工程与源码 |
| `vram_estimate_gb` / `max_concurrent` | 是 | 调度层判断能否与当前已加载模型共存 |
| `unload` | 否 | `policy: on_switch \| never \| always` |
| `capabilities.emotion_control` | 是 | `instruct` 写指令 / `reference` 选变体 / `none` 忽略并告警 |
| `capabilities.voice_clone` | 是 | 为 false 时克隆变体在该 provider 标不可用 |
| `capabilities.max_text_chars` / `max_text_ms` | 是 | 切块上限取候选 provider 最小值 |
| `capabilities.nonverbal` / `nonverbal_control` | 否 | 笑/叹/气声只能路由到声明支持者；`none` 时含副语言的行出局 |
| `capabilities.emotion_intensity_levels` | 否 | 强度需求高于档位数时向下就近取档并标降级 |
| `capabilities.native_duration_control` | 否 | 为 false 时时长靠切分 + 静音填充 + 微变速补偿 |
| `capabilities.license` / `license_gate` / `commercial_use` | 是 | 许可硬过滤：商用工程只允许 `commercial_use=true` 且 `license_gate=none` |
| `capabilities.quality_tier` / `latency_tier` | 是 | 自动路由打分维度 |
| `capabilities.consistency` | 否 | 同角色一致性加分；`pin_per_character=true` 优先固定同 provider |
| `capabilities.allow_auto_select` | 是 | 为 false 时自动路由跳过，只能手动锁定 |

以上配置结构由 `schema/tts-provider.schema.json` 校验；工程 `project.json` 内只保留引用，不复制能力字段。

### 16.2 选择粒度、锁定与自动路由

**锁定优先**（任一非空即跳过打分）：

| 粒度 | 字段 | 优先级 |
| --- | --- | --- |
| 台词行 | `line.voice.provider_id` | 1（最高） |
| 角色 | `character.voice_binding.provider_id` | 2 |
| 工程 | `project.settings.tts.routing.lock_provider_id` | 3 |

三级都为空时走**自动路由**（`project.settings.tts.routing.mode=auto`）：

1. **需求向量**：`emotion{label,intensity 0~3}`、`paralinguistic[]`、`clone_required`、`text_chars`、`language`、`duration_control`、`streaming_required`、`usage`。
2. **硬过滤**：`enabled` + `allow_auto_select` + `health` 通过 → 许可（商用要求 `commercial_use=true` 且 `license_gate=none`）→ 能力（情绪方式 / 副语言 / 克隆 / 长度 / 语言 / 时长 / 流式）→ 变体 `provider_availability=ready` → 资源余量。
3. **打分**：能力 30 / 质量 25 / 同角色一致性 20 / 耗时 15 / 资源 10；并列按 `provider_order` 索引、再按 `provider_id` 字典序。
4. **落盘**：`line.tts.routing`（selected / rejected / degradations / input_hash / policy_revision），UI 可展开。
5. **回落**：失败按 `provider_order` 降级，写 `degradations[]`，不覆盖 `voice.provider_id`。
6. **确定性**：同 `input_hash` 复用上次决策，不重新打分。

变体解析在选定 provider 后进行：行 `variant_id` → 角色 `emotion_variant_map[emotion]`（该 provider 可用）→ 角色 `default_variant_id` → 音色 `default_variant_id` → `voice_missing`。

### 16.3 已实测的能力边界（Qwen3-TTS 1.7B 参考实现）

| 结论 | 数据模型体现 |
| --- | --- |
| 9 个预设音色 | 变体 `source=preset` + `preset_name` 枚举 |
| 3 秒参考音频即可零样本克隆，无需微调 | `reference.duration_ms` 下限 2000，建议 5000~15000 |
| `voice_clone_prompt` 可预算缓存并复用 | `prompt_cache{status,path,hash,model}`，按变体一份 |
| Base 克隆模型不支持自然语言指令控情绪 | 情绪不写入 TTS 参数，只用于**选变体** |
| 无原生语速参数 | `speed` 定义为后期 atempo 系数 |
| 无 SSML / 停顿参数 | 显式 `silence_events` + 切块拼接静音 |
| 单次建议 ≤30~40 秒 | `tts.chunks[]` 切块记录，15~30 秒一块 |

### 16.4 结构（`voices.json`）

```jsonc
{
  "schema_version": 1,
  "voices": [
    {
      "id": "voc_01JBM90008",
      "name": "沈砚-低沉男声",
      "default_provider_id": "qwen3_tts_base",           // 可空；空则跟随工程默认 provider
      "language": "zh-CN",
      "gender": "male",
      "tags": ["低沉", "冷静"],
      "default_variant_id": "vav_01JBM90009",
      "variants": [
        {
          "id": "vav_01JBM90009",
          "name": "默认",
          "source": "cloned",                              // preset|cloned|recorded
          "emotion_tags": ["default", "neutral", "serious"], // default/neutral/joy/sad/angry/fear/surprise/tender/excited/whisper/serious
          "preset_name": null,                             // preset 必填，9 个枚举之一
          "reference": {
            "audio_path": "assets/voices/ref/vav_01JBM90009.wav",
            "duration_ms": 12000,
            "text": null,
            "snr_db": 32.5,
            "channels": 1,
            "sample_rate": 24000
          },
          "prompt_cache": {
            "status": "ready",                             // pending|ready|failed
            "path": "assets/voices/prompts/vav_01JBM90009.pt",
            "hash": "sha256:...",
            "model": "Qwen3-TTS-1.7B-Base",
            "created_at": "2026-11-09T12:00:00Z"
          },
          "consent": {
            "confirmed": true,                             // cloned/recorded 必须 true
            "note": "本人授权，仅用于本作品",
            "evidence_path": "assets/voices/consent/vav_01JBM90009.txt"
          },
          "quality": { "status": "pass", "notes": [], "checked_at": "2026-11-09T12:00:00Z" },
          "provider_availability": [                       // provider × 变体
            {
              "provider_id": "qwen3_tts_base",
              "status": "ready",                           // ready|pending|unusable
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
          "status": "ready",                               // ready|pending|unusable
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

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `default_variant_id` | 是 | 情绪未命中时的兜底变体 |
| `default_provider_id` | 否 | 该音色的建议 provider；空则跟随工程默认 |
| `variants[].source` | 是 | `preset`（9 个预设）/`cloned`（克隆）/`recorded`（自录参考） |
| `variants[].emotion_tags` | 是 | 该变体适用的情绪；`default` 表示兜底 |
| `variants[].preset_name` | preset 必填 | `Vivian` `Serena` `Uncle_Fu` `Dylan` `Eric` `Ryan` `Aiden` `Ono_Anna` `Sohee` |
| `variants[].reference` | cloned/recorded 必填 | 参考音频；`duration_ms` 2~20 秒 |
| `variants[].prompt_cache` | 建议 | 预计算的 `voice_clone_prompt`；缺失则合成前先生成 |
| `variants[].provider_availability` | 用克隆/自录时建议 | provider × 变体的可用性、是否支持克隆、试听产物与结论 |
| `variants[].consent.confirmed` | cloned/recorded 必填 | 必须 `true`，否则禁止合成（schema `const: true`） |
| `variants[].status` | 是 | `ready` 可合成 / `pending` 待试听或待缓存 / `unusable` 质检不过 |

> **provider 解耦**：`voices.json` 不绑定模型。参考音频与 prompt 缓存是共用资产；能否在某 provider 上使用由 `provider_availability` 决定。

### 16.5 情绪 → 变体解析与降级

解析顺序（命中即停）：

1. 行级 `voice.variant_id`。
2. 角色 `voice_binding.emotion_variant_map[emotion]`。
3. 角色 `voice_binding.default_variant_id`。
4. 音色库 `voices[].default_variant_id`。
5. 全部落空 → 行标 `voice_missing`，进入校对列表，不阻塞其他行。

降级规则：

- 情绪无对应变体 → 用默认变体合成，台词表标「情绪变体缺失」warning，**不自动生成新变体**。
- 变体 `status != ready` 或授权未确认 → 若角色有 `fallback_voice_id` 则换音色并告警；否则该行 `failed`。
- 同一角色同一情绪必须始终解析到同一变体（写入 `tts.variant_id` 供比对），否则音色漂移。
- 预设音色的情绪由预设本身风格体现，`emotion_variant_map` 可为空。

### 16.6 静音 / 停顿事件（silence_event）

```jsonc
{
  "id": "sil_01JBM90011",
  "index": 1,
  "duration_ms": 600,                // 50~30000
  "position": {
    "mode": "after_line",            // before_line|after_line|during_line|shot_start|shot_end
    "line_id": "lin_01JBM90003",
    "offset_ms": 0
  },
  "anchor": { "block_id": "blk_01JBM90000", "char_start": 120, "char_end": 125, "text_hash": "sha256:...", "state": "exact" },
  "reason": "对白之间的呼吸停顿"
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `duration_ms` | 是 | 静音时长；时间轴按实测语音轨插入空白 |
| `position.mode` | 是 | `shot_start` / `shot_end` 用于镜头边界留白 |
| `position.line_id` | 相对行模式必填 | 行被删除时降级到镜头结尾并告警 |
| `anchor` | 是 | 回指正文；与行内 `pause_*` 冲突时以静音事件为准并去重 |

**与 `pause_before_ms` / `pause_after_ms` 的分工**：行内字段是「这一行前后自然的呼吸」，由排版直接使用；`silence_event` 是「跨行、跨镜头或需单独标注的留白」，优先级更高。

### 16.7 切换 provider 的重算影响

合成产物的有效输入 = `provider_id + model_version + variant_id + text_hash + 切块参数 + 输出规格`。任一变化即失效。

| 变更 | 失效 | 不失效 |
| --- | --- | --- |
| 工程默认 provider | 未显式指定 provider 的行：块音频 → 行拼接音频 → 时间轴 → 字幕时间码 | 显式锁定 provider 的行；图像/视频/音效 |
| 角色级 provider | 该角色全部行（同上） | 其他角色；画面与动画 |
| 行级 provider | 仅该行块音频与拼接音频 | 同镜头其他行 |
| 同 provider 升模型版本 | 该 provider 下所有行 | 其他 provider 的行 |
| 改 `provider_order` | 仅原本走回落链的行 | 显式指定的行 |
| provider 能力变化 | 相关元素标 `stale`，人工确认后重跑 | 已完成产物 |

- 块级：只重跑变化块，同行其他块复用。
- 行级：块有变化才重拼，`duration_ms` 重算。
- 时间轴与字幕：`provider_id` / `model_version` / `variant_id` / `duration_ms` 纳入 `source_hash`，变化即重建。
- 图像、视频、音效不依赖 TTS，不受 provider 切换影响。

统一错误码：`PROVIDER_UNREACHABLE`、`PROVIDER_UNAUTHORIZED`、`PROVIDER_MODEL_MISSING`、`PROVIDER_OOM`、`PROVIDER_TIMEOUT`、`PROVIDER_UNSUPPORTED_VOICE`、`PROVIDER_UNSUPPORTED_EMOTION`、`PROVIDER_LICENSE_BLOCKED`、`PROVIDER_BAD_OUTPUT`；每个错误带 `retryable` 与可操作 `hint`。

### 16.8 对比试听（多 provider A/B）

落盘 `assets/voice/compare/<lineId>_<batchId>.json`，**不写台词行产物、不改 `tts` 状态、不进时间轴**：

```jsonc
{
  "id": "cmp_01JBM90012",
  "line_id": "lin_01JBM90003",
  "text": "说重点。",
  "emotion": "serious",
  "variant_id": "vav_01JBM90009",
  "batch_id": "cmpb_01JBM90013",
  "items": [
    { "provider_id": "qwen3_tts_base", "model_version": "1.7b-base-2026-10", "status": "ready",
      "asset_id": "assets/voice/compare/lin_01JBM90003_qwen3_tts_base.wav", "duration_ms": 1430,
      "applied": { "emotion": false } },
    { "provider_id": "other_tts", "model_version": "x.y", "status": "failed",
      "code": "PROVIDER_UNSUPPORTED_EMOTION", "message": "该 provider 不支持按变体表达情绪" }
  ],
  "winner_provider_id": null,
  "created_at": "2026-11-09T12:00:00Z"
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `items[]` | 是 | 每个 provider 一条；同文本、同情绪、同变体、同切块，只变 provider |
| `items[].status` | 是 | `ready|failed`；失败不影响其他项 |
| `items[].applied` | 否 | 实际生效的能力项（情绪/语速/SSML），用于解释差异 |
| `winner_provider_id` | 否 | 人工盲听结论；只作建议，不自动改绑定 |

对比任务按 `items` 顺序串行执行，相邻 provider 不同时驻留显存。记录结构由 `schema/voice-compare.schema.json` 校验。

## 17. 抽卡与候选（candidate，v2）

画面（`visual`）与动画（`animation`）都内嵌 `draw` + `candidates[]`。

```jsonc
{
  "id": "cnd_01JBM90009",
  "index": 1,
  "asset_id": "assets/image/img_01JBM90005_c1_r1.png",
  "job_id": "job_01JBM90010",
  "seed": 20261109,
  "params_hash": "sha256:...",
  "status": "candidate",             // candidate|selected|rejected|expired|failed
  "created_at": "2026-11-09T12:00:00Z",
  "expires_at": "2026-11-16T12:00:00Z",
  "width": 1152,
  "height": 1536,
  "duration_ms": null
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `index` | 是 | 批内序号；与文件名 `_c<cand>` 对应 |
| `asset_id` | 成功时必填 | 产物相对路径 |
| `seed` / `params_hash` | 是 | 复现依据；相同 seed + params 视为同一候选（追加时去重复用） |
| `status` | 是 | `candidate` 可选 / `selected` 已选中 / `rejected` 人工排除 / `expired` 过期 / `failed` 生成失败 |
| `expires_at` | 否 | 未选中候选的保留截止；默认 7 天，过期由清理任务标 `expired` 并删媒体 |
| `duration_ms` | 视频必填 | ≤5000 |

规则：

- 追加式：再次抽卡只追加，不覆盖历史；失败候选不占保留期，可直接重抽。
- 选中：`draw.selected_candidate_id` 指向一个候选，进入时间轴；换选只改指向与状态，不重新生成，触发时间轴重建。
- 已选中候选永久保留，不参与过期清理。

## 18. 锚点与增量重算（v2）

### 18.1 锚点（anchor）

所有脚本元素（镜头、台词行、音效、静音、画面、动画）共用同一结构：

```jsonc
{
  "block_id": "blk_01JBM90000",   // 必填：正文段落块
  "char_start": 120,              // 必填：块内起始偏移（Unicode 码点，0 基）
  "char_end": 125,                // 必填：块内结束偏移（不含）
  "text_hash": "sha256:...",      // 必填：该范围原文哈希（去首尾空白、全半角统一）
  "line_id": "lin_01JBM90003",    // 可选：稳定行 ID，优先定位
  "state": "exact"                // exact|shifted|orphan
}
```

定位算法（从强到弱，命中即停）：`line_id` 命中 → 块内偏移哈希命中 → `char_start ± 200` 邻域搜索 → 章节全文唯一匹配（更新 `block_id` 与偏移）→ 全部失败标 `orphan`，不参与下游生成。

### 18.2 增量重算口径

- 元素级 `input_revision` = 自身 `revision` + 依赖的 `settings_revision` + 依赖角色/音色 `revision`；任一变化标 `stale`。
- 只把 `stale` 元素入队；无变化直接复用产物（按文件存在 + 参数哈希一致判定）。

| 改动 | 重算 | 不重算 |
| --- | --- | --- |
| 改某行文本 | 该行 `revision+1` → 重切该行 → 该行 TTS → 必要时重建时间轴 | 同镜头其他行、图、视频 |
| 改说话人 / 情绪 / 语速 / 停顿 | 该行 TTS | 其他行 |
| 改角色音色或克隆变体 | 该角色全部台词行 TTS | 图像、视频、音效 |
| 改角色外貌设定 | 引用该角色的 `visuals` 与依赖的 `animations` 标 `stale` | 台词、音效 |
| 改镜头氛围/描述 | 该镜头 `visuals` 提示词重拼并重抽；依赖动画重跑 | 台词 TTS |
| 改音效提示词/时长 | 该音效事件重生成 | 其他元素 |
| 块内插入/删除句子 | 该块行重新切分 + 受影响行 TTS + 锚点邻域搜索 | 其他块 |
| 删除某行 | 锚定音效降级到镜头开头、画面锚点转孤儿或顺延、静音事件降级 | 其他行 TTS |
| 整章重写 | 重生成导演脚本，旧脚本归档 `history`，全量重算 | — |

### 18.3 改文后的最小重算流程

```
正文 diff → 重算 blocks（稳定 ID） → 逐元素 anchor 定位（exact/shifted/orphan）
→ 漂移元素更新锚点（不动内容） → 文本变化的行 revision+1
→ 依赖设定/角色变化的元素标 stale → 仅 stale 入队
→ 时间轴按 source_hash 判定是否重建
```

## 19. 状态机

**章节 `status`（v2）**：

```
draft ──写正文──► written ──生成脚本──► scripted ──全行配音──► voiced ──图/视频就绪──► visualized ──导出──► composed
  ▲                    │                    │                 │                    │
  └──── 上游设定变更（settings_revision 不等）→ stale（保留产物，标需重算） ◄────────┘
```

| 状态 | 进入条件 |
| --- | --- |
| `draft` | 有标题无正文 |
| `written` | 正文非空，无有效脚本 |
| `scripted` | 脚本 `status=ready` 且非 stale |
| `voiced` | 全部台词行 `tts.status=ready` |
| `visualized` | 视觉元素已选中且无 pending 的必需图 |
| `composed` | 有有效导出产物 |
| `stale` | `settings_revision`、角色或音色发生变化 |

**台词行 `tts.status`**：`pending` → `running` → `ready` / `failed`；输入变化或 provider 切换标 `stale`，重跑前不覆盖旧产物；provider 与音色都解析不到时标 `provider_missing` / `voice_missing`，进入校对列表。自动路由全部候选被硬过滤时同样标 `provider_missing`，并在 `tts.routing.rejected[]` 记录每个 provider 的淘汰阶段与原因；降级成功仍为 `ready`，但 `routing.degradations[]` 非空，UI 显示「已降级」。

**音效 / 画面 / 动画**：`pending` → `queued` → `running` → `ready`；抽卡类为 `generating` → `candidates_ready` → `selected`；视频额外允许 `skipped`（超预算或用户关闭）；失败为 `failed`，可重试。

**候选 `status`**：`candidate` → `selected` / `rejected` / `expired` / `failed`。

**渲染任务 `status`**：`queued` → `running` → `succeeded`；`running` 中断后重启标记 `interrupted`（可重跑）；失败 → `failed`（可重试时回 `queued`）；用户取消 → `canceled`；前置失败 → `skipped`。

## 20. 幂等、增量与校验

- **实体版本**：`revision` 变更即触发下游重算；产物文件名带 revision 与候选号，天然不覆盖。
- **幂等键**：`idempotency_key = sha256(kind | target_ids | input_revision | adapter | params_hash)`；队列中同键任务只保留一条，执行前再次比对，命中已完成则直接复用产物。
- **抽卡幂等**：同一 `visual/animation` 下 `seed + params_hash` 相同的候选直接复用，不重复生成。
- **时间轴 `source_hash`**：由参与行的 `id + revision + tts.duration_ms + tts.variant_id`、静音事件的 `id + revision`、音效/画面/动画的 `id + revision + 选中候选 ID` 规范化后哈希；未变化不重建。
- **schema 校验**：写入前用 `schema/*.schema.json` 校验，失败拒绝落盘并给出具体路径（如 `shots[0].lines[3].emotion`）。
- **迁移**：`schema_version` 变化时由 `store/migrations/` 脚本逐版本升级，迁移前自动备份到 `.ndw/backup/`。

## 21. 与 schema/ 的对应关系

| 实体 | Schema 文件 | 版本 |
| --- | --- | --- |
| 作品 + 全局设置 | `schema/project.schema.json` | v1 扩展 |
| 设定库（世界观/大纲/文风/连续性） | `schema/setting-bible.schema.json` | v2 |
| 角色表（含音色绑定与状态卡） | `schema/character.schema.json` | v1 扩展 |
| 音色库（音色 + 变体） | `schema/voice.schema.json` | v2 |
| TTS Provider 配置与能力矩阵（外置配置） | `schema/tts-provider.schema.json` | v2 |
| 配音对比试听记录 | `schema/voice-compare.schema.json` | v2 |
| 章节（含 blocks / continuations） | `schema/chapter.schema.json` | v1 扩展 |
| 导演脚本（镜头/台词/音效/画面/动画/静音/候选/锚点） | `schema/director-script.schema.json` | v2 |
| 场景（v1 遗留） | `schema/scene.schema.json` | v1 |
| 台词行（含旁白） | `schema/line.schema.json` | v1 扩展 |
| 音效事件 | `schema/sfx-event.schema.json` | v1 扩展 |
| 关键图 / 画面（含 draw / candidates） | `schema/key-image.schema.json` | v1 扩展 |
| 短视频段落 / 动画（含 draw / candidates） | `schema/video-segment.schema.json` | v1 扩展 |
| 时间轴 | `schema/timeline.schema.json` | v1 |
| 渲染任务 | `schema/render-job.schema.json` | v1 |
| 导出产物 | `schema/export-artifact.schema.json` | v1 |
