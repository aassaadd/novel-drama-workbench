# 音色与 BGM 内容决策前置（R18）设计

> 状态：设计完成（已对标定稿）＋ 已实现（2026-11-12，待验收）
> 日期：2026-11-12
> 关联：`docs/spec-r17-voice-design.md`（音色描述 → 抽卡）、`schema/character.schema.json`、`schema/voice.schema.json`、`schema/chapter.schema.json`、`schema/scene.schema.json`、`schema/director-script.schema.json`、`schema/bgm.schema.json`、`docs/data-model.md` §16
> 本轮范围：设计已定稿并进入实现；实现时避让并行开发中的文件（`schema/voice.schema.json`、`web/app.py`、`config.py`、`page-settings.js` 等）与运行中的任务。

---

## 0. 一句话方案

把「谁用什么声音」「这段戏该配什么音乐」两类**内容决策**全部压到前期：生成人物时就产出结构化**音色描述**并给旁白、常用龙套建实体；生成台本（导演标注）时就产出每个镜头的**BGM 描述**。后期配音/混音只做「按固定优先级查表 + 合成」，不再逐章临时决定。

---

## 1. 现状盘点（只读结论）

设计必须落在现有结构上，先把「现在是什么样」写清楚，避免方案落空。

| 项 | 现状 | 位置 | 对本设计的影响 |
| --- | --- | --- | --- |
| 角色音色绑定 | `character.voice_binding`：`voice_library_id` + `default_variant_id` + `emotion_variant_map` | `schema/character.schema.json#L78-L129` | 只存「引用」，**没有任何音色内容描述** |
| 实际解析 | `resolve_for_character()` 只读 `binding.voice_id` → `pick_variant(emotion)` | `src/novel_drama/store/voice_store.py#L97-L143` | 单一入口，适合收敛解析优先级；但当前**不读** `voice_library_id`、**不读** `line.voice.variant_id` |
| 逐行取值 | `_line_voice()` 调上面的函数，缺失时回落 `service.default_voice` | `src/novel_drama/tts/batch.py#L89-L114` | 回落链目前是「角色绑定 → 服务默认」，无章节覆盖、无龙套池 |
| 变体级优先级 | 行 `variant_id` > 角色情绪映射 > 角色默认变体 > 音色默认变体（文档约定） | `docs/data-model.md#L1129-L1137` | 文档已定，行级一层**尚未实现** |
| 旁白 | `chr_narrator` 是 `characters.json` 里的真实行（`new_narrator()`），`role=narrator` | `src/novel_drama/models/entities.py#L75-L94`、`src/novel_drama/store/project_store.py#L201` | 实体已存在；但 `voice_binding` 默认 `null`，且人物页**把它过滤掉**（`page-characters.js#L315-L317`），用户无法给它配音色 |
| 旁白展示 | 走固定中文名映射，不暴露 ID | `src/novel_drama/models/entities.py#L20-L45` | 旁白作为正式实体不会破坏展示层规则 |
| 龙套 | `role` 枚举有 `minor`，但只是标签；生成人物时无龙套池概念 | `schema/character.schema.json#L23`、`writing/service.py#L1898` | 可复用 `role=minor` 建实体，无需新实体类型 |
| 人物生成 | `generate_characters()` 只产 name/role/gender/age_range/personality/background/appearance/negative/relationships | `src/novel_drama/writing/service.py#L1790-L1923` | 要在此处扩出「音色描述 + 龙套池」 |
| 台本生成 | `suggest_script()` 产出 `@shot / !sfx / !visual / !anim / !silence` 标注 → `director-script` | `src/novel_drama/writing/service.py#L681-L836`、`director/markup.py` | **没有 BGM 语义**，BGM 目前全靠手工导入 |
| BGM | 章节级 `bgm/<chapter>.json`，cue = `asset_id + start_ms + gain + fade + loop` | `schema/bgm.schema.json`、`src/novel_drama/media/bgm.py` | 只有「音频放哪」，没有「为什么放这个、放什么风格」 |
| BGM 意图字段 | `script_style.narration_voice_id` / `bgm_mood` 已定义但**全代码库无引用** | `schema/director-script.schema.json#L33-L40` | 说明「意图层」是预留但未落地，可在本轮补实 |
| 场景情绪 | `scene.mood` 是自由文本（≤100 字），台词行有 10 类 `emotion` 枚举 | `schema/scene.schema.json#L15-L18`、`schema/line.schema.json#L16-L19` | BGM 描述可复用 `mood` + 行情绪，但要新增结构化字段 |
| 混音 | `plan_mix()` 跳过 `asset_id` 为空的 cue 且**不告警** | `src/novel_drama/media/mixer.py#L84-L106` | 若引入「只有描述、没有素材」的 cue，需要补告警 |

**结论**：现有结构是可扩展的（角色实体已有 role=minor/narrator，绑定和变体解析已有单一入口），缺的是「内容描述层」和「解析优先级的一处收敛」。不需要新实体类型，也不该另起一套音色库。

---

## 2. 数据模型：音色归属放在哪一层

### 2.1 三个候选与取舍

| 方案 | 做法 | 优点 | 致命问题 |
| --- | --- | --- | --- |
| A. 全塞进角色实体 | `character.voice_profile` + 现有 `voice_binding` | 1:1、无 join、schema 追加即可、迁移最轻；旁白/龙套本来就是角色行 | 跨角色策略（旁白切换点、龙套兜底）没地方放 |
| B. 独立 voice profile 表 | 新 `voice_profiles.json`，按 owner 索引 | 便于批量管理与复用 | 与 `characters.json` 1:1 双写，一致性与迁移成本翻倍；删除角色要级联；收益只在"多角色共用一份描述"（本项目不需要） |
| C. 章节级覆盖表 | `voice_overrides/<chapter>.json` | 天然支持逐章差异 | 正是用户要消灭的「逐章临时决定」；没有角色默认时每章都要写 |

**推荐：A 为主 + 少量工程级策略**。理由：

1. 音色描述是**角色的属性**（由人物档案推导），不是独立资产；独立表只会制造双写。
2. 旁白、龙套在本项目里已经是 `characters.json` 的行（`new_narrator()` 已存在），统一放角色实体可以**一个解析函数覆盖三类角色**。
3. 真正需要跨角色表达的是「全剧音色编排策略」，量很小（旁白切换点、龙套兜底音色、严格模式开关），放 `project.json` 的 `tts.cast` 即可，不值得单开文件。
4. 章节级覆盖**保留但严格限制**（见 §2.3），落在 `chapter.voice_overrides`，不新建文件。

### 2.2 三层结构

```
① 内容层（新增）   character.voice_profile      ← LLM 生成，存在人物阶段，跨章不变
② 绑定层（沿用）   character.voice_binding      ← 指向 voices.json 的真实参考音
③ 例外层（新增）   chapter.voice_overrides      ← 仅旁白/龙套，必须写理由
```

- ① 是「应该是什么声音」（可评审、可重新抽卡、可作为 voice design 输入）。
- ② 是「实际用哪份参考音」（由 R17 抽卡选中后写入，`voice_binding` 结构不变）。
- ③ 是「本剧确实需要的少数例外」（例如第二幕换女声旁白），不是常规手段。

### 2.3 解析优先级（唯一入口）

新增 `resolve_cast_voice(store, chapter, line) -> CastVoice`，**替换** `_line_voice()` 里的隐式逻辑，所有配音入口（批量、单行、试听、预检）统一走它：

| 级 | 来源 | 命中条件 | 是否允许静默 |
| --- | --- | --- | --- |
| 1 | `line.voice.variant_id` | 行级显式指定变体 | 否，必须在行上写 `voice.reason`，否则预检告警 |
| 2 | `line.voice.voice_id` | 行级显式指定音色 | 同上 |
| 3 | `chapter.voice_overrides[character_id]` | 章节显式覆盖 | 否，必须写 `reason`；仅 `role=narrator` 或 `stock` 角色可用 |
| 4 | `character.voice_binding` | 角色默认绑定 | 是（正常路径） |
| 5 | `tts.cast.stock_defaults[stock.slot]` | 龙套槽位默认音色 | 是，命中时打 `source=stock_pool` |
| 6 | `tts.cast.minor_fallback_voice_id` | 任意 `role=minor` 的兜底音色 | 是，打 `source=minor_fallback` |
| 7 | `tts.cast.narrator_fallback_voice_id` | 旁白兜底（仅在旁白未绑定时） | 是，打 `source=narrator_fallback` |
| 8 | `tts.cast.unknown_voice_id` | `speaker_id=chr_unknown`（台本里没建过的路人） | 是，打 `source=unknown_fallback`，进「待归类路人」清单并**告警** |
| 9 | `project.tts.default_voice_id` | 工程默认音色 | 是，打 `source=project_default` 并**告警** |
| 10 | 无 | 全部落空 | 报 `VOICE_MISSING`，进配音校对列表，不阻塞其他行 |

选定音色后，**变体级**仍沿用文档既定顺序：情绪变体映射 → 角色默认变体 → 音色默认变体（`voice_store.pick_variant()` 不动）。

`CastVoice` 返回结构（供行写回与校验共用）：

```json
{
  "voice_id": "voc_xxx",
  "variant_id": "vav_xxx",
  "ref_audio": "assets/voice/ref/vav_xxx.wav",
  "ref_text": null,
  "source": "character_binding|chapter_override|line_override|stock_pool|minor_fallback|narrator_fallback|project_default",
  "decided_by": {"level": 4, "evidence": "character.voice_binding.voice_library_id"},
  "warnings": []
}
```

### 2.4 什么情况下允许章节覆盖

硬约束（写入 schema 与校验器，不靠自觉）：

1. **只允许两类角色**：`role=narrator`，或 `stock` 标记为真的龙套；主角/反派/配角一律拒绝写入。
2. **必须写理由**：`reason` 非空（≤200 字），否则保存被拒。
3. **旁白覆盖必须来自白名单**：`tts.cast.narrator_switches[]` 里登记的切换点（见 §3.3），且覆盖章节必须落在该切换点之后、下一个切换点之前。防止"每章随机变旁白"。
4. **不允许改 provider/adapter**：覆盖只做 `voice_id` / `variant_id`，换引擎仍走 `voice_binding.provider_id` 或自动路由。
5. **覆盖是显式的**：`chapter.voice_overrides` 为空 = 不覆盖；不做任何"猜"。

---

## 3. 旁白

### 3.1 推荐方案：单实体 + 显式切换点（不做多条旁白实体）

- **唯一旁白实体**仍然是 `chr_narrator`（保留 ID 不变，`character.schema.json` 的 `allOf` 约束继续成立）。
- 需要「男声旁白 / 女声旁白」时，**不新建 `chr_narrator_2`**，而是登记「旁白切换点」：

```jsonc
// project.json → tts.cast
{
  "narrators": [                                 // 命名旁白人格（2~3 个足够），不是新实体
    {
      "id": "npr_primary",
      "label": "主旁白（男声）",
      "voice_profile": { /* §5，可缺省 */ },
      "voice_id": "voc_narr_primary"
    },
    {
      "id": "npr_female",
      "label": "第二幕女声旁白",
      "voice_profile": { /* §5 */ },
      "voice_id": "voc_narr_female"
    }
  ],
  "default_narrator_id": "npr_primary",          // 第 1 章起的旁白
  "narrator_switches": [
    {
      "at_chapter_id": "chp_01JBM9...",          // 从这一章开始生效（含）
      "persona_id": "npr_female",                // 切换目标人格
      "reason": "第二幕改为女主角第一人称旁白",
      "scope": "from_here"                       // from_here（直到下一个切换点）| this_chapter_only（番外/梦境）
    }
  ]
}
```

- 人格只解决"旁白可以有几种固定音色"，**不改变台词归属**：所有旁白行仍是 `speaker_id=chr_narrator`。
- 切换点引用 `persona_id` 而不是裸 `voice_id`：换旁白音色只需改人格，不必回改每条切换点；人格也能各自带 `voice_profile` 参与抽卡与校验。
- `from_here` 是默认，区间一直延续到下一个切换点；`this_chapter_only` 只允许番外、梦境、引用文书等显式场景，且必须写 `reason`。

理由：

1. 多旁白实体要改 `character.schema.json` 的 `id const` 约束，还要改所有"旁白就是 chr_narrator"的判断（`text/splitter.py`、`director/markup.py`、`media/references.py`、前端 `VIRTUAL_SPEAKER_LABELS`），成本高、回归面大。
2. 切换点结构天然保证「不会每章随机变」：一次登记、区间生效（`from_here` 直到下一个切换点）、下一章自动沿用；切换点若落在人物页之外的入口（如某章页）则保存被拒。
3. 台词归属不变（仍是 `speaker_id=chr_narrator`），时间轴、字幕、导出全部零改动。
4. 真要"两个旁白同时说话"的小说极少；确需时用行级 `line.voice.variant_id` 例外，而不是拆实体。

### 3.2 解析规则

```
旁白行 → line.voice（例外）
       → chapter.voice_overrides[chr_narrator]（必须命中白名单切换点）
       → narrator_switches 中 <= 当前章序号的最后一条 → cast.narrators[persona_id].voice_id
       → character(chr_narrator).voice_binding
       → cast.narrators[default_narrator_id].voice_id
       → cast.narrator_fallback_voice_id
       → VOICE_MISSING
```

切换点按「章节在 `chapters/` 里的 index 顺序」判定，不用创建时间，避免导入顺序影响结果。

### 3.3 前端约束

- 人物页把旁白作为**固定卡片**放在列表最上方：不可删除、不可改名，但可编辑音色描述、可执行「生成音色候选」（R17 入口）、可绑定音色。
- 旁白卡片的「跨章切换」入口收在 `设置 / 音色编排` 里，不在每章页提供"改旁白音色"按钮——从入口上就杜绝逐章临时改。

---

## 4. 龙套池

### 4.1 建模：还是角色实体，加两个标记

不引入新实体类型，`characters.json` 里新增可选字段：

```jsonc
{
  "id": "chr_01JBM9...",
  "name": "龙套·店小二",
  "role": "minor",
  "stock": {
    "slot": "waiter",              // 稳定英文槽位键（职业），全剧唯一含义
    "voice_slot": "male_mid",      // 音色池槽位：多个职业共用同一音色（见决策 11）
    "label": "店小二",              // 中文展示名（与 name 的「龙套·」前缀解耦）
    "archetype_tags": ["市井", "殷勤", "中年"],  // 供台本匹配的语义标签
    "reuse_policy": "reuse"        // reuse=全剧复用同一音色 | per_scene=允许按场景换
  },
  "voice_profile": { /* §5 */ },
  "voice_binding": { /* §2 */ }
}
```

- `slot` 是**稳定键**，一旦被章节台词引用就不再改名；改名只能改 `label` / `name`。
- 一个 slot 只对应一个角色实体（默认），保证"同一个龙套在不同章节是同一个声音"。
- `reuse_policy=per_scene` 是逃生口：确实要「同一类路人不同人演」时，允许多个角色共用同一 slot，此时一致性校验降级为 warning（见 §7）。

### 4.2 命名

- 显示名统一 `龙套·<职业>`（如 `龙套·衙役`、`龙套·路人甲`），`stock.label` 存不带前缀的职业名。
- `aliases` 收台词原文里的各种称呼（`店小二`、`小二`、`跑堂的`），供切分台词时精确匹配。
- 角色 ID 仍是普通 `chr_`，展示层继续走 `speaker_display_name()`，不暴露 ID。

### 4.3 生成时机与数量

在 `generate_characters()` 里**同批产出**：

- 主角/反派/配角：照旧。
- 附加 `stock_characters[]`：默认 8 个 `slot`（配置 `writing.characters.stock_slots`，可调）；每个带 `voice_profile`。
- 旁白不在这里生成（旁白是既有实体），但**人物阶段必须为旁白生成 `voice_profile`**（一次生成，之后跨章复用）。

LLM 工具 schema 追加（`emit_characters`）：

```
characters[]        照旧
stock_characters[]  { name, stock:{slot,label,archetype_tags}, gender, age_range,
                      personality, speech_style, voice_profile{...} }
narrator_profile    { voice_profile{...}, summary }
```

生成结果仍进**待保存列表**，用户确认后批量落盘（沿用现有 dry-run → 确认 → `character apply` 流程）。

### 4.4 台本出现"没建过的路人"怎么办

推荐策略：**归入虚拟未知角色 + 待归类清单，不自动建实体、不报错**。

| 情形 | 处理 | 理由 |
| --- | --- | --- |
| 说话人名与既有 `name`/`aliases` 精确命中 | 直接用该角色 | 最常见，零成本 |
| 未命中，但 LLM 切分时给出 `speaker_role_hint`（如 `waiter`）且池中有同 slot | 归到该 slot 角色，行上记 `speaker_match="stock_slot"` | 保证声音稳定 |
| 未命中，池中无同 slot 但有同性别/年龄段的龙套 | 归到该龙套，记 `speaker_match="stock_fuzzy"` 并告警 | 有声音比静音好，但必须有痕迹 |
| 仍无匹配 | `speaker_id=chr_unknown`（虚拟，不建实体），**默认用 `cast.unknown_voice_id` 出声**，进「待归类路人」清单并告警 | 避免每章生成垃圾角色；有声音比静音好，但必须留痕。用户可一键「归入某龙套」或「新建角色」 |
| 严格模式 `tts.cast.strict_minor=true` | 配音前把未归类路人列为 **error**，阻断批次 | 需要全员有声时用 |

「待归类路人」清单挂在人物页（按出现章节数排序），一键归类时**回写历史行的 speaker_id**，并复用同一音色。

---

## 5. 音色描述结构

### 5.1 字段清单（`character.voice_profile`）

在 R17 `docs/spec-r17-voice-design.md` §1.2 的 JSON 基础上补全为可复用结构（R17 的 `design_prompt` 是唯一喂模型的字段，这里保持不变）：

```jsonc
{
  "schema": "voice_profile/1",
  "summary": "三十岁出头、低沉克制的男声，语速偏慢，略带北方口音",
  "gender": "male",
  "gender_source": "character",              // character | manual | inferred
  "age_feel": "young_adult",                 // child|teen|young_adult|adult|middle_aged|elderly
  "age_range_text": "30-39",
  "pitch": "low",                            // very_low|low|mid_low|mid|mid_high|high|very_high
  "timbre": ["低沉", "略带沙哑", "厚实"],
  "pace": "slow",                            // very_slow|slow|normal|fast|very_fast
  "volume": "normal",                        // soft|normal|loud
  "energy": "calm",                          // calm|conversational|dramatic|authoritative（对标选角表的 Energy）
  "warmth": "neutral",                       // warm|neutral|cold|distant（对标选角表的 Warmth/Distance）
  "articulation": "clear",                   // clear|mumbled|clipped|drawl
  "breathiness": 0.2,                        // 0~1
  "raspiness": 0.35,                         // 0~1
  "temperament": ["沉稳", "压抑"],
  "accent": "北方口音",                       // 可为 null
  "region_hint": "华北",
  "language": "zh-CN",
  "role_distinctiveness": 0.8,               // 0~1，主角/反派更高，供抽卡时拉开辨识度
  "design_prompt": "A low-pitched, slightly raspy male voice ...",
  "negative_prompt": ["童声", "播音腔", "过度甜美"],
  "reference_text": null,                    // 可选：抽卡试听用固定台本（为空用全局默认台本）
  "confidence": 0.82,
  "evidence": ["gender=male", "speech_style.pitch=低沉", "personality=沉稳"],
  "source": {
    "kind": "llm",                           // llm | rule | manual
    "provider": "omlx",
    "model": "qwen3-...",
    "at": "2026-11-12T03:00:00Z",
    "from_hash": "sha256:...",               // 推导输入字段的哈希，用于判定 stale
    "override_fields": []                    // 用户手工改过的字段名
  },
  "status": "ready",                          // draft|ready|stale|missing
  "stale_reason": null
}
```

### 5.2 从人物设定的哪些字段推导

| 输出字段 | 输入 | 规则 | 权重 |
| --- | --- | --- | --- |
| `gender` | `character.gender` | 直接映射；`unknown/other` 交给 LLM 结合 `background` 推断并标 `inferred` | 硬约束 |
| `age_feel` / `age_range_text` | `character.age_range` | 解析「18-24」「中年」等文本 → 枚举 | 高 |
| `pitch` | `speech_style.pitch` | 中文音区词 → 枚举（低沉→low，尖细→high） | 高 |
| `pace` | `speech_style.tempo` | 慢/缓→slow，急/快→fast | 高 |
| `accent` | `speech_style.accent` | 保留原词；为空则 null（**不臆造方言**） | 中 |
| `timbre[]` | `personality[]` + `background` | 气质词→音色质感映射表（沉稳→厚实、阴郁→暗哑、活泼→明亮） | 高 |
| `temperament[]` | `personality[]` | 直接取 2~4 个 | 高 |
| `breathiness` / `raspiness` | `background`（病弱/年迈/粗豪）+ `appearance_prompt` 的体型/年龄词 | 仅作弱提示 | 低 |
| `volume` / `articulation` | `background`（职业：说书人/军汉/书生） | 职业发音习惯 | 中 |
| `role_distinctiveness` | `role` | 主角 0.9 / 反派 0.85 / 配角 0.6 / 龙套 0.35 | 中 |
| `design_prompt` | 以上全部 | 拼接为英文设计提示 | — |

规则：

1. **外观只取体型/年龄线索，不取颜值词**（避免"漂亮=甜妹音"的刻板映射）——沿用 R17 §1.1 设计原则。
2. 缺失字段不阻断：`age_range` 为空 → `age_feel` 由 `background` 推断；`personality` 为空 → 只给生理与职业线索，`confidence` 相应降低。
3. **推导输入哈希**：`source.from_hash` = sha256(`gender|age_range|personality|background|speech_style|appearance_prompt 的年龄体型片段`)；字段一变即判 `stale`，校验器提示"音色描述已过期，请重新生成"（不需要 LLM 也能检测）。
4. LLM 不可用 → 规则模板回退（性别+年龄+音区+语速拼句），`source.kind="rule"`、`confidence` 上限 0.5，页面显示"规则生成，建议重新生成"。
5. 结果仍进待保存列表，确认后写盘；覆盖已有 `voice_profile` 需二次确认（与 R17 一致）。

### 5.3 与 R17 抽卡的关系

`voice_profile.design_prompt` 是 R17 抽卡弹层的**预填值**；抽卡选中候选后写 `voices.json` + `character.voice_binding`，并把 `voice_profile.status` 置 `ready`、记录 `source` 中的 `batch_id`（放 `source.batch_id`，可选字段）。**音色描述不因抽卡而被替换**——描述是决策依据，binding 是产物。

---

## 6. BGM 描述

### 6.1 落点：镜头（场景）级为主，章节 cue 只承载音频

| 位置 | 存什么 | 生命周期 |
| --- | --- | --- |
| `director-script.shots[].bgm`（主） | 结构化 BGM 描述（风格/情绪/强度/时长/循环/多段） | 台本阶段生成，用户可编辑 |
| `scene.bgm`（兜底） | 同结构；给不走导演脚本的 v1 工程用 | 同上 |
| `bgm/<chapter>.json#cues[]`（派生） | 真实音频 + `start_ms` + 增益/淡入淡出/循环 | 选好素材后排布，可随时重建 |
| `timeline`（不存） | 只在混音时读 cue | 派生，重建即可 |

**为什么不是章节级或时间轴 cue 级**：

1. 章节级太粗：一章多个情绪段落无法表达，最后还是要拆。
2. 时间轴 cue 级太细且不稳定：时间轴由台词实测时长派生，一句台词改时长 cue 就漂移；描述应该锚在**叙事单位（镜头/场景）**上，由构建期换算成毫秒。
3. 镜头级正好是现有 `suggest_script()` 的生产粒度（`@shot`），也是 `materialize_media()` 的落地粒度，改动面最小。

读取顺序：`shot.bgm` > `scene.bgm` > 无（视为未规划，告警）。

### 6.2 字段清单

```jsonc
// director-script.schema.json → $defs.bgm_plan
{
  "id": "bgp_01JBM9...",
  "scope": "shot",                       // chapter|shot（shot 写在镜头，chapter 仅在整章统一音乐时用）
  "name": "夜雨独行",
  "narrative_function": "铺垫危机临近，压住对白空隙",  // 叙事功能：为什么这里需要音乐（cue sheet 标准字段）
  "mood_family": "tense",                // calm|tense|sad|romantic|mysterious|action|comedic|epic|horror
  "mood_text": "压抑、悬而未决",           // 自由描述，给人工看
  "energy": 0.35,                        // 0~1
  "intensity": 0.5,                      // 0~1
  "style": "低音弦乐 + 稀疏钢琴单音，无人声",
  "instrumentation": ["大提琴", "钢琴", "低频鼓点"],
  "tempo_bpm": 62,
  "duration_hint_ms": 45000,             // 期望时长（可为空，由 cue 决定）
  "loop_required": true,                 // 是否必须无缝循环
  "loop_point_hint": "整曲可循环",         // 可选，供选曲/生成时参考
  "gain_db_hint": -20,                   // 对白下建议音量
  "fade_in_ms": 1500,
  "fade_out_ms": 2000,
  "duck_under_dialogue": true,           // 预留：对白自动压低（本轮只存不自研实现）
  "source": "llm",                       // llm|manual
  "status": "planned",                   // planned|asset_ready|rendered|missing
  "segments": [                          // 一个场景多个情绪段落
    {
      "index": 1,
      "anchor_line_id": "lin_01JBM9...",  // 段落起点锚在该行
      "mood_family": "mysterious",
      "energy": 0.2,
      "style": "环境低频铺底",
      "duration_hint_ms": 12000,
      "transition_in": "fade",           // cut|fade|dissolve|none
      "transition_out": "crossfade",
      "note": "推门之前保持悬疑"
    }
  ],
  "revision": 1
}
```

规则：

- `segments` 为空 = 整镜一条；非空 = 按段拆 cue，段内共享 `name`/`instrumentation`，允许各自覆盖 `mood_family/energy/style`。
- 段落起点必须有 `anchor_line_id`（镜头内已存在的行），保证时间轴重建后可重新定位；行被删则整段降级到镜头起点并告警。
- `loop_required=true` 的段必须在 cue 里写 `loop=true`，否则校验器报 `BGM_LOOP_MISMATCH`。

### 6.3 与现有 cue 机制对接

```jsonc
// bgm.schema.json → cues[] 追加可选字段（asset_id 改为可选）
{
  "id": "bgm_01JBM9...",
  "name": "夜雨独行",
  "asset_id": "assets/audio/bgm/night_rain_loop.wav",   // 可为空：未选曲
  "start_ms": 12800,
  "gain_db": -20,
  "fade_in_ms": 1500,
  "fade_out_ms": 2000,
  "loop": true,
  "loop_until_ms": 58000,
  "plan_ref": {"shot_id": "sht_...", "segment_index": 1, "plan_id": "bgp_..."},
  "mood_family": "mysterious",       // 冗余一份，便于按情绪筛选素材
  "status": "planned"                // planned|ready|missing
}
```

- **cue 生成（派生动作）**：`materialize` 阶段按镜头顺序把 `bgm_plan` 展开成 cue；`start_ms` 由 `timeline_builder` 的同一套行时间计算得出（构建时间轴时顺带写 cue，保证与语音轨一致）。
- **未选曲**：`asset_id` 为空、`status=planned`，`plan_mix()` 需要把这些 cue 报为 warning（现在是无提示跳过），媒体页显示"待选曲"。
- **重建安全**：cue 由 plan 派生，plan 改了就重建对应镜头范围的 cue；手工导入的 cue（无 `plan_ref`）不动。
- **选曲/生成**：本轮只做描述与排布；素材来源可以是手工导入（现有 `import_asset`）、未来本地音乐生成，或按 `mood_family + instrumentation` 检索本地素材库。`source` 字段留口。

### 6.4 台本生成时怎么产出

在 `suggest_script()` 的标注语法里增加一行 `!bgm`，与 `!sfx` 同级：

```
@shot 1 验尸房夜询 location=验尸房 time=night mood=压抑
旁白：夜色渐深。
沈砚秋|calm：执照可以收，尸体不能白躺。
!bgm tense 0.35 "低音弦乐 + 稀疏钢琴，无人声" loop
!bgm-seg 1 after-line=3 mysterious 0.2 "环境低频铺底" 12000
!sfx ambience 8000 after-line=1 雨声敲窗
```

- `!bgm <mood_family> <energy> "<style>" [loop]`：镜头主描述。
- `!bgm-seg <index> after-line=N <mood_family> <energy> "<style>" <duration_ms>`：多情绪段落。
- 解析器（`director/markup.py`）新增对应规则，落到 `shot.bgm`；缺 `!bgm` 时 **不编造**，`status=missing`，校验器告警（而不是让 LLM 无中生有）。
- 提示词里加入"每个镜头必须给一条 BGM 判断；宁缺毋滥，不用音乐就写 `!bgm none`"。
- 离线适配器（`adapters/offline_writing.py`）按场景 `mood` 给确定性映射，保证无 LLM 也能跑通。

---

## 7. 一致性校验

### 7.1 设计目标

可自动化、可解释、可在多处复用；**不调用 TTS 模型**，只做静态解析（读 `characters.json` / `voices.json` / `chapters/*.json` / `scripts/*.json`）。

新增模块 `src/novel_drama/voice_cast.py`（或 `store/cast_store.py`，实现时定），唯一入口：

```python
check_cast(store, chapter_id=None) -> {
  "level": "ok|warn|error",
  "findings": [...],
  "stats": {...},
  "input_hash": "sha256:..."
}
```

### 7.2 检查项与级别

| 代码 | 检查 | 级别 | 修复建议 |
| --- | --- | --- | --- |
| `VOICE_PROFILE_MISSING` | 非 unknown 角色缺 `voice_profile` | narrator/主角 error；其余 warn | 生成音色描述 |
| `VOICE_PROFILE_STALE` | `from_hash` 与当前人物字段不符 | warn | 重新生成描述 |
| `VOICE_BINDING_UNRESOLVED` | 有描述但解析不到可用变体 | error（该角色有台词时） | 抽卡/绑定音色 |
| `VOICE_VARIANT_NOT_READY` | 解析到变体但 `status != ready` 或授权未确认 | error | 修复变体 |
| `CHARACTER_VOICE_DRIFT` | 同一角色跨章解析出的 `variant_id` 不唯一（排除显式覆盖） | error | 删掉异常章节覆盖或统一绑定 |
| `NARRATOR_DRIFT` | `chr_narrator` 跨章解析不唯一 | error | 收敛到切换点白名单 |
| `NARRATOR_SWITCH_ILLEGAL` | 章节覆盖旁白但不在 `narrator_switches` 区间内 | error | 登记切换点或删除覆盖 |
| `CHAPTER_OVERRIDE_FORBIDDEN` | 主角/反派/配角出现章节覆盖，或缺 `reason` | error | 删除覆盖 |
| `STOCK_SLOT_CONFLICT` | 同一 `slot` 下多个角色绑了不同音色（`reuse_policy=reuse`） | error | 统一音色或改 `per_scene` |
| `STOCK_UNCLASSIFIED` | 章节里有 `chr_unknown` 且同名台词 ≥ 2 次 | warn（严格模式 error） | 归入龙套池 |
| `BGM_PLAN_MISSING` | 有台词的镜头没有 `bgm` | warn | 生成 BGM 描述 |
| `BGM_SEGMENT_ANCHOR_ORPHAN` | 段起点行不存在 | warn | 重新锚定 |
| `BGM_LOOP_MISMATCH` | `loop_required=true` 但 cue `loop=false` | warn | 改 cue |
| `BGM_ASSET_MISSING` | `status=planned` 且已导出/混音 | warn | 选曲 |

### 7.3 跑在什么时机

| 时机 | 范围 | 阻断行为 |
| --- | --- | --- |
| 保存人物后（单个） | 该角色 + 当前章 | 不阻断保存；卡片上出现黄色标记；error 时 toast 中文提示 |
| 切章时（前端） | 当前章 | 不阻断；页面顶部黄条「本章有 N 项音色/BGM 待处理」，可点开明细 |
| 场景/台词切分后（CLI） | 全工程 | 不阻断切分；切分结果照常落盘并输出到 stdout，问题清单以中文提示打到 stderr（`--json` 时 JSON 仍保持纯净） |
| **配音前** | 全工程 | `error` 阻断批次（返回中文清单，逐条给 `chapter_id/line_id/角色名`）；`warn` 只提示 |
| 导出前 | 全工程 | 不阻断；导出结果里附 `warnings`（避免"因为 BGM 没选曲就不能导出"） |
| CLI `ndw cast check` | 按参数 | 退出码：0 无 error，4 有 error（与 `PreconditionError` 统一口径） |

### 7.4 性能与缓存

- 单章：读 1 个章节 + 1 份人物表 + 1 份音色表，O(行数)，实测预计 < 30ms。
- 全工程：O(角色数 × 章节数) 的纯内存解析，不读音频、不加载模型；100 章量级预计 < 300ms。
- 缓存：`.ndw/cast_check.json` 记录 `input_hash`（人物表/音色表/章节 revision 集合的哈希），未变化直接复用结果；切章校验走内存缓存，不写盘。

---

## 8. 兼容与迁移

### 8.1 总体原则

**只做追加，不改既有语义**：所有新字段可选；老工程不迁移也能继续配音、导出，行为与现在一致。

### 8.2 字段缺省时的回落

| 缺失项 | 回落 | 用户可见提示 |
| --- | --- | --- |
| `character.voice_profile` | 跳过描述层，直接走 `voice_binding` | 人物卡片「音色描述未生成」 |
| `voice_binding`（旁白） | `cast.narrators[default_narrator_id].voice_id` → `cast.narrator_fallback_voice_id` → `project.tts.default_voice_id` | 配音页提醒「旁白使用兜底/工程默认音色」 |
| `voice_binding`（龙套） | `cast.stock_defaults[stock.voice_slot]` → `cast.minor_fallback_voice_id` → 工程默认 | 同上去重提示 |
| `chr_unknown`（未建路人） | `cast.unknown_voice_id` → `cast.minor_fallback_voice_id` → 工程默认 | 人物页「待归类路人」清单 |
| `chapter.voice_overrides` | 不覆盖 | 无 |
| `tts.cast` | 全部按"未配置"，只走角色绑定与工程默认 | 设置页显示默认值 |
| `shot.bgm` / `scene.bgm` | 视为未规划 | 标注页「本镜未规划 BGM」 |
| `cue.plan_ref` | 视为手工 cue，不参与重建 | 无 |

### 8.3 存量工程升级步骤

1. **零改动可用**：升级后老工程照常配音（解析优先级第 4 级就是现有 `voice_binding`）。
2. **补旁白实体**：`load_characters()` 已有兜底（无 `chr_narrator` 时补一行），只需确保 `role=narrator` 的卡片在页面上可见。
3. **回填音色描述**：新增 `ndw cast backfill --profiles [--all] [--dry-run]`
   - 默认 dry-run，输出待生成清单与预估 token；
   - 已手工改过（`source.override_fields` 非空）的角色默认跳过，需 `--force`；
   - 只写 `voice_profile`，不动 `voice_binding`、不动台词。
4. **回填龙套标记**：`ndw cast backfill --stock` 把已有 `role=minor` 且被 ≥2 章引用的角色标记 `stock.slot`（用 LLM 或按姓名生成 slug），生成 `cast.stock_defaults` 建议值（dry-run 供确认）。
5. **回填 BGM 描述**：`ndw script bgm plan --chapter <id>` 对指定章生成 `shot.bgm`（可批量 `--all`），不创建 cue、不动音频。
6. **数据版本**：`schema_version` 保持 1；不需要整库重写。回填命令写过的文件会自然带上新字段。

### 8.4 风险点与缓解

| 风险 | 缓解 |
| --- | --- |
| 开始读取 `line.voice.variant_id` 后，老工程里"写了但一直没生效"的行突然变声 | 配音前报告列出"N 行存在行级覆盖，将生效"；`config.voice.cast.honor_line_override=false` 可全局关掉（默认 true） |
| 旁白突然从"工程默认音色"变成"narrator_voice_id" | 回填命令不写 `narrator_voice_id`；只有用户显式设置才生效 |
| 龙套 `slot` 改名导致历史章节指向错乱 | `slot` 落盘后禁止直接改，改名走 `cast rename-slot`（同时回写引用） |
| BGM cue 重建覆盖手工调整 | 只重建带 `plan_ref` 的 cue；`gain_db` 等混音参数以现有 cue 为准，plan 只提供建议值 |
| 校验过严阻塞配音 | 只有 `error` 级阻断；严格模式（`strict_minor`、`strict_bgm`）默认关闭 |

---

## 9. 改动面评估

规模：S ≈ 小（<100 行）、M ≈ 中（100~300 行）、L ≈ 大（300+ 行）；不含测试与文档。

| 模块 | 具体文件 | 改动性质 | 规模 |
| --- | --- | --- | --- |
| 角色 schema | `schema/character.schema.json` | **新增** `voice_profile`、`stock` 可选字段；`role` 枚举不变 | S |
| 工程 schema | `schema/project.schema.json` | **新增** `tts.cast {narrators[], default_narrator_id, narrator_switches[], stock_defaults{}, voice_pool[], unknown_voice_id, minor_fallback_voice_id, narrator_fallback_voice_id, strict_minor, strict_bgm}` | M |
| 章节 schema | `schema/chapter.schema.json` | **新增** `voice_overrides` 可选对象 | S |
| 场景 schema | `schema/scene.schema.json` | **新增** `bgm`（引用 `bgm_plan` $defs） | S |
| 导演脚本 schema | `schema/director-script.schema.json` | **新增** `$defs.bgm_plan`；`shot.bgm` 可选 | M |
| BGM schema | `schema/bgm.schema.json` | `asset_id` 改可选；cue **新增** `plan_ref`/`mood_family`/`status` | S |
| 音色 schema | `schema/voice.schema.json` | R17 已规划的 `designed`/`generation`/`sha256`；本轮不额外改 | — |
| 人物生成 | `writing/service.py`（`generate_characters`、`_CHARACTER_TOOL_SCHEMA`、`_clean_character_entry`） | **改逻辑 + 扩 schema**：产出 `voice_profile`、`stock_characters`、`narrator_profile`；清洗与校验新字段 | L |
| 人物落盘 | `cli_character.py`（`append_characters`、`cmd_character_update`） | **改逻辑**：写入 `voice_profile`/`stock`；`update` 支持 `--voice-profile-file`、`--stock-slot`；允许更新旁白（仅描述/绑定字段） | M |
| 解析收敛 | 新增 `voice_cast.py` + 改 `store/voice_store.py`、`tts/batch.py#_line_voice` | **新增函数 + 改调用**：`resolve_cast_voice()` 统一 10 级优先级；`_line_voice` 改为薄封装 | L |
| 一致性校验 | 新增 `voice_cast_check.py`（或并入上文件）+ `cli.py` 新命令 | **新增** 14 条检查 + 缓存 + 退出码 | L |
| 台本生成 | `writing/service.py#suggest_script`（提示词）、`director/markup.py`（`!bgm`/`!bgm-seg` 解析）、`director/materialize.py`（写 `shot.bgm`）、`adapters/offline_writing.py` | **改逻辑**：新增 BGM 标注语法、解析、物化 | L |
| BGM 派生 | `media/bgm.py`（新增 `sync_cues_from_plans`）、`media/timeline_builder.py`（输出行起始时间给 cue 用）、`media/mixer.py`（空素材告警） | **新增函数 + 小改** | M |
| 配音入口门禁 | `cli_media.py` / `cli_voice.py` / `tts/batch.py` | **改逻辑**：配音前跑 check，error 时中文清单阻断 | M |
| 导出告警 | `media/export.py`、`media/jianying.py` | **小改**：把 cast check 的 warn 并入导出结果 | S |
| CLI | `cli.py`、`cli_character.py`、`cli_voice.py`、`cli_media.py` | **新增** `cast check` / `cast backfill` / `cast rename-slot` / `script bgm plan` | M |
| Web 人物页 | `web/static/page-characters.js`、`index.html` | **改逻辑**：旁白固定卡片可见可配；角色卡增加「音色描述」区与「生成描述/生成音色候选」按钮；龙套分组 | L |
| Web 音色库 | `web/static/page-voices.js` | **小改**：龙套池分组展示、`stock_defaults` 配置入口 | M |
| Web 标注页 | `web/static/page-script.js`、`timeline.js` | **改逻辑**：镜头 BGM 卡片（风格/情绪/强度/时长/循环/多段），可手改可重生成 | L |
| Web 配音页 | `web/static/page-voice*.js`、`app.js` | **小改**：配音前预检结果条 | M |
| Web 接口 | `web/app.py` | **新增** 6 个接口（见 §10） | M |
| 设置页 | `web/static/page-settings.js` | **小改**：`tts.cast` 配置组 + 音色编排（旁白切换点） | M |

**合计**：schema 6 个文件追加字段；新增 2 个模块；改 8 个既有模块；前端 5 个页面。整体属于「一轮中等改造」，但可按 §11 分期，每期都可独立验收。

---

## 10. 接口与入口（实现时按此对接）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/ui/characters/voice-profile` | 生成/重生成单个或批量音色描述（dry-run 返回预览，不落盘） |
| POST | `/api/ui/characters/stock` | 生成龙套池（复用人物待保存列表流程） |
| PATCH | `/api/ui/characters` | 扩展现有接口：`voice_profile`、`stock.slot`（旁白也可更新） |
| POST | `/api/ui/script/bgm/plan` | 按章生成 BGM 描述（dry-run 预览 `shot.bgm`） |
| GET | `/api/ui/cast/check?path&chapter` | 一致性校验结果（含 `level`/`findings`/`stats`） |
| POST | `/api/ui/cast/backfill` | 存量回填（profiles/stock/bgm 三类，默认 dry-run） |

CLI：

```bash
ndw cast check --project P [--chapter chp_x] --json
ndw cast backfill --profiles --all --dry-run
ndw cast backfill --stock --dry-run
ndw script bgm plan --chapter chp_x --dry-run
ndw voice profile generate --id chr_x   # 单角色（也可 --all）
```

前端入口（不新增顶级页签）：

- 人物页：旁白固定卡片 + 每张角色卡的「音色描述」折叠区 + 生成按钮。
- 人物页 → 音色库 subtab：龙套池分组 + 槽位兜底音色。
- 标注页：镜头卡片下方 BGM 区（可折叠），显示多段与时长提示。
- 配音页：顶部预检条（error 红 / warn 黄），点击定位。
- 设置页 → 音色编排：旁白切换点、严格模式开关。

---

## 11. 分期落地与验收

| 期 | 内容 | 验收标准 |
| --- | --- | --- |
| P1 数据与解析 | 6 个 schema 追加；`resolve_cast_voice()` 收敛；`cast check` CLI + 缓存；旁白卡片可见 | 老工程不改任何数据，配音产物与升级前逐行一致；构造"同一角色两章不同绑定"的工程，`cast check` 报 `CHARACTER_VOICE_DRIFT`；旁白可在人物页看到并绑定音色 |
| P2 人物阶段生成 | `emit_characters` 扩字段；纯龙套池；待保存列表展示音色描述；回填命令 | 生成 1 批人物 + 8 个龙套，确认后落盘，`voice_profile` 字段完整且证据可读；同一人物改 `age_range` 后 `cast check` 报 `VOICE_PROFILE_STALE`；无 LLM 时规则回退可用 |
| P3 BGM 前置 | `!bgm` 标注语法 + 解析 + `shot.bgm` + `sync_cues_from_plans` + 混音空素材告警 | 生成一章台本后每个镜头都有 `bgm`（或显式 `none`）；手工导入素材后 cue 按段生成且 `start_ms` 与时间轴一致（误差 ≤ 50ms）；未选曲时混音给出中文 warning 且不中断 |
| P4 门禁与前端收口 | 配音前阻断、导出告警、标注页/配音页 UI、设置页音色编排 | 存在 `VOICE_BINDING_UNRESOLVED` 时配音返回中文清单并退出码 4；修好后可正常批次；页面切换章节出现黄条并可定位 |

每期结束都要求：`python -m py_compile` + `node --check`，schema 校验通过，临时工程清理。

---

## 12. 关键决策定稿

> 已按 §13 的对标结果定稿，不再逐条确认。"变化"列标注与原推荐的差异与依据。

| # | 决策点 | 定稿 | 变化与依据 |
| --- | --- | --- | --- |
| 1 | 音色描述存哪 | **角色实体新增 `voice_profile` + 工程级 `tts.cast`**；`tts.cast` 内含命名旁白人格 `narrators[]`（各带自己的 `voice_profile`） | 细化。依据 §13.1：ElevenLabs 以"角色"为单位做 casting，改一次全书该角色台词同步更新 |
| 2 | 旁白唯一还是多条 | **单实体 `chr_narrator` + 命名人格 + 显式切换点（区间锁定）**，不拆多旁白实体 | 细化：切换点引用 `persona_id`。依据 §13.3：双播/POV 是格式性决策，音色一旦分配就锁到全书 |
| 3 | 章节覆盖范围 | **仅旁白/龙套可覆盖、必须写 `reason`、旁白还须命中切换点**；主角色不得章节级覆盖，个别台词走行级 `line.voice` | 保持。依据 §13.3："不要因为后面章节感觉不同就中途换" |
| 4 | 龙套建模 | **`role=minor` + `stock.slot` 职业稳定键 + `stock.voice_slot` 音色池槽位**，默认 8 个职业槽位 / 6 个音色槽位，命名 `龙套·<职业>` | 细化：职业槽位与音色槽位分离。依据 §13.2：一本典型有声书 4~8 个清晰对白音色 |
| 5 | 未建过的路人 | **归 `chr_unknown`，默认用 `cast.unknown_voice_id` 出声，并进「待归类路人」清单告警**；不自动建实体、不因缺失直接报错，`strict_minor=true` 时才 error | **改**：原为"缺音色→VOICE_MISSING"。依据 §13.1：官方承认角色检测会漏，检测结果只是起点 |
| 6 | BGM 描述落点 | **镜头级 `shot.bgm`（场景兜底），cue 只承载音频，支持 `segments[]` 多情绪段**；补 `narrative_function` | 细化。依据 §13.4：spotting 阶段的 cue 表包含叙事功能与时长 |
| 7 | BGM 无素材时 | **cue 允许空 `asset_id`，混音告警不中断** | 保持。依据 §13.4：cue 本来就先于成品音乐存在 |
| 8 | 校验时机 | **保存人物 warn、切章 warn、配音前 error 阻断、导出 warn**；另提供「选角确认」全剧一次性检查（对轨冲突 + 覆盖率） | 细化。依据 §13.3：选角表与三轮对轨都在录制前完成 |
| 9 | 行级 `line.voice.variant_id` | **立即生效并纳入优先级第 1 级，配音前报告受影响行数** | 保持。依据 §13.1：平台支持段落级覆盖，但必须留痕 |
| 10 | 存量工程 | **零改动可用 + 显式回填命令（默认 dry-run）** | 保持 |
| 11 | 龙套音色来源 | **共享全剧 6 个龙套音色池（按性别×年龄），`stock.voice_slot` 映射；同场景两个龙套不得同音色**；音色仍由 voice design 抽卡产生，产物进池共享 | **改**：原为"每个 slot 独立抽卡、不复用参考音"。依据 §13.2：同场景 4~6 个音色、单句角色轻微区分即可 |

**结论**：11 项中 **2 项实质变更（5、11）**、5 项细节收紧（1、2、4、6、8）、4 项保持（3、7、9、10）。

---

## 13. 对标调研（2026-11-12）

> 来源标注：**官方文档** = 厂商产品文档/帮助中心；**社区实践** = 从业者博客、开源工具、教程；**行业流程** = 配音/有声书/影视配乐既有工序。只摘录可核对的事实，二手教程仅用于佐证常识性做法。

### 13.1 平台侧：音色决策发生在哪一步、旁白怎么处理

| 产品 | 事实 | 对本设计的含义 |
| --- | --- | --- |
| **ElevenLabs Audiobooks**（官方文档） | 上传书稿后先做 Character Casting：AI 检测说话角色，并**为主旁白、每个检测到的角色**各分配音色；官方明确"角色检测由 AI 生成，应视为**起点**，可能漏掉次要角色、未命名角色"；更改角色音色会**同步更新全书该角色所有台词**；也支持在编辑器里为特定段落"覆盖设置"，不影响整个项目 | ① 音色决策在**生成音频之前**、以角色为单位一次定完（支撑 §2 三层模型）；② 段落级覆盖是例外而非主路径（支撑 §2.3 第 1~2 级）；③ 漏检角色是常态（支撑 §4.4 用 `chr_unknown`，而不是报错） |
| **ElevenLabs Voice Design**（官方文档） | 提示词维度中 **Age / Accent(Nationality) / Gender 为高重要性**，Tone 为"可选/非必需"；建议用具体词（"30 岁出头""thick Australian"），并明确列出各维度的推荐描述词 | 决定 §5.1 字段优先级：`gender/age_feel/accent` 必填且硬约束，`timbre/temperament` 允许自由发挥；`design_prompt` 用具体词 |
| **Fish Audio（Drama 3 / S1）**（官方博客 + 社区教程） | 单次推理支持最多 16 个 speaker token（`<|speaker:N|>`），音色与 speaker 索引在**脚本内**绑定；克隆时"参考音频的情绪会被复制"，建议选与目标情绪匹配的参考片段 | ① 音色绑定属于脚本/角色层，不是音频后处理（支撑 §2.1 选 A）；② 参考音自带情绪色彩 → 同一角色跨情绪应走**变体**，而不是换音色（与 §2.3 变体级顺序一致） |
| **Descript**（官方帮助） | 多说话人靠**逐段 @ 指定 Speaker**；Speaker 是项目级实体，改 Speaker 即改该段声音 | 段级指定是通用交互，但工具本身不保证跨章一致 → 本设计的自动校验（§7）是增量价值 |
| **魔音工坊**（官方帮助） | 明确"小说类适合多个配音师，**每个角色对应一个配音师**"，提供多人配音与逐句调音 | 角色↔音色 1:1 是中文有声书的默认心智，支撑 `character.voice_binding` 为主路径 |
| **讯飞（配音/智作）**（官方文章 + 教程） | 角色音色按**性别、年龄、性格**三维差异化；旁白建议用**低频、稍慢**的音色并与角色**分轨**，避免"旁白抢戏" | ① 支撑 §5.2 的推导维度；② 旁白需要独立混音策略（本设计只存 `profile`，混音沿用现有轨道） |
| **剪映 / CapCut**（第三方教程） | "文本朗读"按**字幕段落**选音色，不同段落可用不同音色 | 段级换音色是最容易产生跨章不一致的入口 → 本设计把入口收到人物页/设置页（§3.3），段级只作例外 |

### 13.2 开源与社区：多角色有声书怎么管音色

| 来源 | 事实 | 含义 |
| --- | --- | --- |
| **GPT-SoVITS 及其中文有声书教程**（社区） | 每个角色准备**一份固定参考音**，工程里维护"角色 → 参考音"列表，批量生成时按角色查表；说话人识别只作**初始分配**，之后人工修正 | 与本设计"角色实体 + `voice_binding` + 解析查表"同构；也印证识别结果需人工确认（§4.4 待归类清单） |
| **PyVideoTrans / Pandrator / mizuoto**（开源工具） | 流程是"识别说话人 → 分配角色音色 → 批量生成 → 改 cast 后**只重生成受影响片段** → 前后对比" | 支撑 §7 校验 + 增量重跑；"改音色影响面可计算"必须做成能力 |
| **CosyVoice（阿里云百炼）**（官方文档） | 预置音色按**有声书/年龄/性别**打标（如"25~30 岁、抑扬顿挫女"），支持注册/删除自定义音色 | 音色库应保留结构化标签，与 `voice_profile` 对齐；"注册音色"= 本项目的 `voices.json` 条目 |
| **Vois / Sounds and Such 等社区**（社区实践） | 一本典型有声书 **4~8 个清晰对白音色 + 1 个旁白**就够；**同场景限制 4~6 个**；只有一句台词的角色"轻微区分即可"；角色过多反而听不清 | **直接改写决策 11**：龙套不该"每个 slot 独立抽卡"，而应共享小音色池 + 同场景避让 |
| **VoiceOverMaker / SpeechGeneration 等工作流指南**（社区实践） | 先建 character sheet（角色、原型、音高/音色/能量、默认情绪），再**每个主要角色试 3 个以上音色**，最后把选定结果**锁定成表**（Character → Voice ID → Tier） | 支撑"人物阶段产出 profile → 抽卡 → 写回 binding 锁定"的链路（§5.3） |

### 13.3 行业流程：选角表、旁白人数、群众角色复用

| 事实 | 来源类型 | 含义 |
| --- | --- | --- |
| 有声书制作先出 **casting sheet / 选角表**：角色、功能定位、年龄带、口音、能量、语速区间、音色质感、**排除项（disqualifiers）**、发音表 | 行业流程（出版/配音经纪指南） | §5.1 补 `energy`、`warmth`；`negative_prompt` 已有，对应 disqualifiers；选角必须在录制前定稿 |
| 中文有声书选角做**三轮筛选**：每个角色先选 ≥3 位候选、≥3 位旁白；再把所有角色与旁白**打乱对轨**筛"声线搭配"；最后定最匹配组合 | 行业流程（媒体访谈） | 校验不能只看"单角色是否绑定"，还要看**全剧音色是否撞车** → §7 增加"全剧对轨冲突"检查 |
| 广播剧/有声书 cast 表里**群杂角色单独成组**，由少量 CV 兼任；旁白通常 1 人，多旁白只出现在 POV/双播/中途换人 | 行业流程（剧组名单） | 支撑 `stock` 分组 + 单旁白实体；多旁白是"格式"，不是逐章自由选 |
| 多播有声书有五种常见形态：**POV 分割、角色分割、双人对话、选集、中途换人**；一旦给 POV 分配音色就**锁死到全书**，"不要因为后面章节感觉不同就中途换" | 行业流程（配音团队/工具指南） | 支撑决策 2/3：切换点按 POV/幕登记、区间锁定、禁止逐章漂移 |
| 单人旁白可覆盖几十个角色，靠**音区/能量/节奏**区分，而非每个角色换一套声线 | 行业流程（配音从业者） | 支撑"音色池 + 轻度区分"，说明"每个龙套独立音色"不是最优 |
| 同一演员兼多个小角色时，约束是**同场次不撞音**，不是全剧唯一 | 行业流程（配音实践） | 支撑决策 11 的"同场景避让"校验 |

### 13.4 BGM：影视/游戏配乐的 cue sheet

| 事实 | 来源类型 | 含义 |
| --- | --- | --- |
| 配乐在 **spotting session**（读本/看片阶段）先出 **music cue sheet**：每条 cue 记录**入点、时长、叙事功能（为什么这里要音乐）、情绪/配器描述**，此时**还没有成品音乐** | 行业流程（影视配乐教材/从业者） | ① BGM 决策应发生在**台本阶段**（§6.1 镜头级）；② "只有描述、没有素材"的 cue 是正常状态 → 支撑决策 7（`asset_id` 可空） |
| cue 表标准字段：cue 编号、入点时间码、时长、叙事功能、情绪、配器、与对白关系（是否压低） | 行业流程 | §6.2 补 `narrative_function`；已有 `duck_under_dialogue`/`gain_db_hint` 与行业一致 |
| AI 配乐实践建议先 spotting，把提示精确到"0:15 起势、0:22 落下"，而不是笼统写"紧张" | 社区实践 | 支撑 `segments[]` + `anchor_line_id`：情绪变化必须锚到具体行，而不是整章一个形容词 |

### 13.5 三个核心问题的结论

1. **旁白是否允许换？** 允许，但必须是**格式性决策**（POV/双播/中途换人），在前期登记、区间锁定；不允许"这章感觉不同就换"。→ 单实体 + 命名人格 + 切换点白名单（决策 2）。
2. **龙套怎么复用而不撞音？** 不是"每个龙套独立音色"，而是**小音色池 + 职业槽位映射 + 同场景不撞音**；单句角色只需轻微区分。→ 6 音色池 × 8 职业槽位 + 场景内冲突检查（决策 4、11）。
3. **音色决策放在流程哪一步？** 全部在**录制/合成之前**：平台是 casting 步骤，行业是选角表 + 三轮对轨，开源工具是"角色→参考音"列表。→ 人物阶段定 `voice_profile` + 绑定，台本阶段定 BGM，后期只查表（决策 1、6、8）。

### 13.6 不采纳的做法与原因

| 做法 | 为什么不采纳 |
| --- | --- |
| 多旁白实体（`chr_narrator_m` / `chr_narrator_f`） | ElevenLabs 的旁白仍是**单一 narrator 角色**；双播行业靠 POV 分段而非拆角色。拆实体要改 ID 约束与 5 处判断，收益低、回归面大 |
| 未识别角色自动建实体 | 官方明确检测会漏、只作起点；自动建会产出大量一次性垃圾角色，反而破坏一致性 |
| 每个龙套一个独立音色 | 与"4~8 个清晰音色"的听感上限冲突，且增加抽卡成本；共享池 + 同场景避让更接近行业做法 |
| BGM 直接写时间轴 cue | cue 依赖实测时长，重跑即漂移；spotting 阶段的描述必须锚在叙事单位上 |
| 章节级 BGM（一章一首） | 无法表达一章内多情绪段落，最后仍要拆 |

### 13.7 来源清单

**官方文档**

- [有声书 | ElevenLabs Documentation](https://elevenlabs.io/docs/zh/eleven-creative/products/audiobooks)（英文版：[Audiobooks](https://elevenlabs.io/docs/eleven-creative/products/audiobooks)）
- [Create studio-quality audiobooks with ElevenLabs](https://elevenlabs.io/use-cases/audiobooks)
- [声音设计 | ElevenLabs Documentation](https://elevenlabs.io/docs/zh/eleven-creative/voices/voice-design)、[Voice Design v3](https://elevenlabs.io/blog/voice-design-v3)
- [Fish Audio Drama 3](https://fish.audio/zh-CN/blog/fish-audio-drama-3/)
- [Generate text-to-speech audio – Descript](https://help.descript.com/ai-speech/tts)、[AI Speakers overview](https://help.descript.com/hc/en-us/articles/17372069745293-AI-Speakers-overview)
- [魔音工坊使用帮助](https://www.moyin.com/help?id=1)
- [讯飞有声书配音：角色扮演与情感注入](https://peiyin.xunfei.cn/seospread/1246.html)
- [CosyVoice 系统预置音色列表（阿里云百炼）](https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list)
- [GPT-SoVITS 官网](https://gpt-sovits.com/)

**社区实践 / 开源工具**

- [Creating Distinct Character Voices for Audiobooks – Vois](https://pre-release.vois.so/blog/character-voices-audiobooks)
- [Character Voices in Audiobooks: Pro Differentiation Hacks](https://soundsandsuch.com/howtoaudiobook/character-voices-in-audiobooks-pro-differentiation-hacks)
- [How to Create a Multi-Character Audiobook With AI Voice Casting](https://ondevtra.com/blog/multi-character-audiobook-ai-voice-casting.html)
- [Multi-Voice TTS Workflow](https://speechgeneration.ai/multi-voice-text-to-speech-workflow)
- [How to Make a Duet-Narration Romance Audiobook](https://www.audioproducer.ai/blog/how-to-make-a-duet-narration-romance-audiobook)
- [How to Manage Audiobook Narrators](https://www.authorvoices.ai/blog/manage-audiobook-projects-with-multiple-narrators)
- [Best Practices for Editing Multiple Voice Actors in a Single Audiobook](https://www.audioscene.org/article/best-practices-for-editing-multiple-voice-actors-in-a-single-audiobook/)
- [How to Create a Multi-Cast AI Audiobook（含 casting sheet 的 Vocal Contrast Match 列）](https://blog.fanfun.ai/multi-cast-ai-audiobook-guide/)
- [How to Find and Brief an Audiobook Narrator](https://publishing.co.uk/guides/audiobook-narrator-finding/)
- [Writing a Voice Casting Brief for Dubbing](https://octavia.lunartech.ai/blog/voice-casting-brief-for-dubbing)
- [Fish Audio Drama 3 in ComfyUI（本地部署实践）](https://freakyjolly.com/fish-audio-drama-3-local-comfyui/)
- [GPT-SoVITS 多角色有声书教程（B 站）](https://www.bilibili.com/opus/1004900258543566857)、[pyVideoTrans 多角色配音](https://en.pyvideotrans.com/multi-role-dubbing)
- [Pandrator](https://pypi.org/project/pandrator/)、[mizuoto](https://pypi.org/project/mizuoto/0.2.20/)
- [剪映字幕自动配音方法（第三方教程）](https://m.duote.com/tech/202510/880038.html)

**行业流程**

- [有声阅读：用专业提高质量，让"制作"打动人心（环球网）](https://lx.huanqiu.com/article/4D8C9BJJuHP)（三轮选角与对轨）
- [有声小说制作：从文本到音频的批量流程（腾讯云社区）](https://cloud.tencent.cn/developer/article/2771694)
- [广播剧制作流程篇（配音号）](https://www.peiyinhao.com/1983.html)
- [广播剧《小蘑菇》配音组名单（729 声工场）](http://www.729voice.com/newsinfo/1382541.html)（群杂角色单列）
- [Composing for Film, Part 1: Before You Score（B&H）](https://www.bhphotovideo.com/explora/pro-audio/tips-and-solutions/composing-film-part-1-you-score)
- [Spotting Sessions and Music Cue Sheets（Fiveable）](https://library.fiveable.me/sound-design/unit-12/spotting-sessions-music-cue-sheets/study-guide/m1X2Hkre78fOEUIq)
- [Scoring for Media: cue 信息项（Stevens）](https://personal.stevens.edu/~abrick/scoringformedia/html/HMU495_notes_03.html)
